//! The iOS Now Playing star: `MPRemoteCommandCenter`'s like command.
//!
//! ## Why this exists
//!
//! On iOS the Now Playing module — Control Center, the lock screen, CarPlay,
//! the Watch's now-playing app — draws a star for the track the system believes
//! is playing (issue #53: "the star button on IOS builds"). That star IS
//! `MPRemoteCommandCenter.likeCommand`, and the web cannot reach it: the Media
//! Session API the webview already uses (see the `navigator.mediaSession`
//! effect in `web/src/components/PlayerBar.tsx`) covers play, pause, previous,
//! next and seek and NOTHING else — there is no like action to register there.
//! So the star is the one piece of this feature that must be native, and this
//! module is that piece. Everything else stays where it was: one like store on
//! the server, three (now one) hearts in the UI, and `web/src/lib/iosFavs.ts`
//! as the bridge.
//!
//! ## What it does
//!
//! * [`register`] runs once, at setup, and attaches ONE handler to
//!   `likeCommand`. Pressing the star tells the webview (a Tauri event) and
//!   answers the OS with success. The shell never writes a like itself: the
//!   app's own store is the single writer, and the pushed state comes back
//!   through [`set_liked`].
//! * [`set_liked`] mirrors the app's answer onto the star. `active` on an
//!   `MPFeedbackCommand` means "the user already likes this item" (MediaPlayer's
//!   own `MPFeedbackCommand.h`: *"An example of when a feedback command would
//!   be active is if the user already 'liked' a particular content item"*), and
//!   that is what makes the OS draw the star filled instead of hollow. The web
//!   UI calls it whenever the current track or its liked state changes, so the
//!   star and the app's hearts cannot disagree.
//! * `dislikeCommand` is pinned inactive. This app has no dislike concept, and
//!   an active dislike button would offer a state nothing could store.
//! * `enabled` is set explicitly, in the words of Apple's own header rather
//!   than relying on a default: the command has to be pressable BEFORE the web
//!   has said anything about the current track, or the very first press — the
//!   one that likes an unliked track — would have nothing to hit.
//!
//! ## What it deliberately does not do
//!
//! * It does not touch `MPNowPlayingInfoCenter`. The webview's own Media
//!   Session metadata is what the OS reads for title/artist/album/artwork, and
//!   the star is only on screen WHILE that session owns the now-playing module
//!   (a paused-then-abandoned session can lose the module to another app, and
//!   the star goes with it — the OS decides that, not us).
//! * It does not set `localizedTitle`/`localizedShortTitle`. The OS's own
//!   "Like" wording is right for every one of this app's six UI languages;
//!   hard-coding English here would be a translation regression, and these
//!   strings are only extra context the command does not need to appear.
//!
//! None of this compiles off iOS: `lib.rs` declares the module behind
//! `#[cfg(target_os = "ios")]`, Android drives its now-playing notification
//! from the webview's Media Session alone, and the desktop targets have no
//! `MPRemoteCommandCenter` to talk to. `lib.rs` still registers the Tauri
//! command on every target (empty body elsewhere) so the web UI can call it
//! unconditionally.

use std::ptr::NonNull;

use block2::{DynBlock, RcBlock};
use objc2::msg_send;
use objc2::rc::Retained;
use objc2::runtime::{AnyClass, AnyObject};
use tauri::{AppHandle, Emitter};

/// The event the shell sends when the OS star is pressed. MUST match
/// `LIKE_EVENT` in `web/src/lib/iosFavs.ts` — the two are one wire, and the
/// web side ignores an event name it does not know.
pub const LIKE_EVENT: &str = "mlo-ios-like";

/// `MPRemoteCommandHandlerStatusSuccess`, the value the OS wants back once the
/// press has been acted on (MediaPlayer's `MPRemoteCommand.h`; the failure
/// values are 100/110/120/200). Anything else makes the system treat the press
/// as unanswered and can make the UI look broken, so the handler always
/// answers success: handing the toggle to the web UI IS handling it.
const HANDLED: isize = 0;

/// MediaPlayer is where `MPRemoteCommandCenter` lives, and linking it is not
/// ceremony: `objc_getClass` only finds a class whose framework is LOADED, so
/// without this the dynamic lookup below would come back empty (the framework
/// is otherwise only pulled in by the webview's own playback).
#[link(name = "MediaPlayer", kind = "framework")]
extern "C" {}

/// The process-wide `MPRemoteCommandCenter`, or `None` when this process has no
/// MediaPlayer command centre at all (a class that did not load, a stripped
/// runtime): the callers log or return rather than taking the app down over the
/// OS's own furniture.
///
/// A dynamic lookup rather than a cached `class!`: a missing class must be a
/// quiet "no star here", never a panic on the way up.
fn command_center() -> Option<Retained<AnyObject>> {
    let class = AnyClass::get(c"MPRemoteCommandCenter")?;
    // SAFETY: the class was just looked up and `sharedCommandCenter` is the
    // singleton accessor with no arguments.
    unsafe { msg_send![class, sharedCommandCenter] }
}

/// The live `MPFeedbackCommand` the Now Playing star is drawn from. `active` on
/// it means "the user already likes this item" — see the module docs.
fn like_command() -> Option<Retained<AnyObject>> {
    let center = command_center()?;
    // SAFETY: a message to the live centre; `likeCommand` is a read-only
    // property returning the command the centre owns.
    unsafe { msg_send![&*center, likeCommand] }
}

/// The sibling feedback command, which this app never activates.
fn dislike_command() -> Option<Retained<AnyObject>> {
    let center = command_center()?;
    // SAFETY: as `like_command`, for `dislikeCommand`.
    unsafe { msg_send![&*center, dislikeCommand] }
}

/// Mirror the app's answer for the CURRENT track onto the OS star: `true` draws
/// it filled (the track is favourited), `false` hollow. Called from the
/// `set_now_playing_liked` Tauri command, i.e. whenever the web UI's own like
/// state for the track changes — see `web/src/lib/iosFavs.ts`.
pub fn set_liked(liked: bool) {
    let Some(like) = like_command() else {
        return;
    };
    // SAFETY: `like` is a live `MPFeedbackCommand` (a `Retained`, so non-nil),
    // and `setActive:` takes the BOOL the feedback state is expressed with.
    unsafe {
        // `enabled` is re-asserted with every push rather than only at setup:
        // the same bit is written by the system's now-playing plumbing (WebKit
        // publishes the webview's now-playing info and refreshes the command
        // set that goes with it), and a star a state push cannot turn back on
        // is a star that vanishes mid-album.
        let _: () = msg_send![&*like, setEnabled: true];
        let _: () = msg_send![&*like, setActive: liked];
    }
}

/// Re-assert that the star is pressable, without touching its state. Called by
/// the audio-session module at the transitions that rebuild the system's
/// now-playing furniture — becoming active again, and the media server
/// restarting (`src/ios_audio.rs`).
pub fn refresh() {
    let Some(like) = like_command() else {
        return;
    };
    // SAFETY: as `set_liked`.
    unsafe {
        let _: () = msg_send![&*like, setEnabled: true];
    }
}

/// Wire the star up once, at app setup: enable the like command, leave the
/// dislike command out of the UI, and attach the handler that hands every press
/// to the web UI.
pub fn register(app: &AppHandle) {
    let Some(like) = like_command() else {
        eprintln!(
            "[mlo-desktop] MediaPlayer's command centre is unavailable — \
             the iOS Now Playing star stays hidden"
        );
        return;
    };

    let app = app.clone();
    // SAFETY: every call below is a message to a live object with the
    // arguments its declaration takes; the handler block's signature is
    // `MPRemoteCommandHandlerStatus (^)(MPRemoteCommandEvent *)` — one object
    // pointer in (NSInteger out), which is what the closure is declared with.
    unsafe {
        // Pressable before the first state push (see the module docs): the
        // "already liked" bit below is the STATE, not the presence.
        let _: () = msg_send![&*like, setEnabled: true];
        // Nothing is known about the current track yet, so the star starts
        // hollow; the web UI pushes the real state as soon as it knows.
        let _: () = msg_send![&*like, setActive: false];

        // The app has no dislike concept: the command is left in the state
        // that keeps it off the screen.
        if let Some(dislike) = dislike_command() {
            let _: () = msg_send![&*dislike, setActive: false];
        }

        let handler = RcBlock::new(move |_event: NonNull<AnyObject>| -> isize {
            // The web UI owns the like store, so the press is handed over as
            // an event and the answer comes back through `set_liked` — one
            // writer, and the star's fill can never drift from the app's
            // hearts. A webview that cannot be reached is not a reason to
            // fail the press: the same audio session is what the user is
            // looking at, and the OS has no useful "failed" rendering here.
            if let Err(e) = app.emit(LIKE_EVENT, ()) {
                eprintln!("[mlo-desktop] could not tell the UI about the iOS star press: {e}");
            }
            HANDLED
        });
        // The `&DynBlock<...>` binding is the shape `msg_send!` accepts for a
        // block argument (the same one `block2`'s own docs and objc2-foundation
        // use for `initWithBytesNoCopy:…deallocator:`).
        let handler: &DynBlock<dyn Fn(NonNull<AnyObject>) -> isize + 'static> = &handler;
        // `addTargetWithHandler:` copies the block into the command (that copy
        // is what the returned token stands for), so our own reference can go
        // out of scope here — exactly how the ecosystem's other block-taking
        // calls are written. The token is only needed to `removeTarget:`, and
        // the star lives as long as the app does.
        let _token: Option<Retained<AnyObject>> = msg_send![&*like, addTargetWithHandler: handler];
    }
}
