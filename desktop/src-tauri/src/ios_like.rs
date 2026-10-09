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
//!   `likeCommand`. Pressing the star tells the webview (a Tauri event,
//!   carrying the press's own NUMBER — see the next bullet) and answers the OS
//!   with success. The shell never writes a like itself: the app's own store is
//!   the single writer, and the pushed state comes back through [`set_liked`].
//! * [`set_liked`] mirrors the app's answer onto the star, and REMEMBERS it.
//!   `active` on an `MPFeedbackCommand` means "the user already likes this item"
//!   (MediaPlayer's own `MPFeedbackCommand.h`: *"An example of when a feedback
//!   command would be active is if the user already 'liked' a particular content
//!   item"*), and that is what makes the OS draw the star filled instead of
//!   hollow. The web UI calls it whenever the current track or its liked state
//!   changes, so the star and the app's hearts cannot disagree — which needs the
//!   memory, not just the write: the system rebuilds the command set behind this
//!   app's back (WebKit republishes it from the web content process), so the
//!   fill is replayed from the web's own last word at every re-assert
//!   (`assert_like_command`). Without the replay a liked track's star went
//!   hollow the moment the app was backgrounded and returned.
//! * `dislikeCommand` is pinned inactive. This app has no dislike concept, and
//!   an active dislike button would offer a state nothing could store.
//! * `enabled` is set explicitly, in the words of Apple's own header rather
//!   than relying on a default: the command has to be pressable BEFORE the web
//!   has said anything about the current track, or the very first press — the
//!   one that likes an unliked track — would have nothing to hit.
//! * A press the webview was not awake to receive is REMEMBERED and re-sent
//!   when the app is next active (`refresh`), and forgotten the moment the web
//!   pushes a state of its own. The star's whole point is that it is used from
//!   the lock screen, which is exactly when a parked webview drops a Tauri
//!   event on the floor; without this the press is answered at the OS level and
//!   then silently lost. A parked webview is not always a DEAF one, though — it
//!   may run the queued event the moment it wakes — so every press carries a
//!   number (`PENDING_PRESS`) and the page drops a copy of a press it has
//!   already handled: one press, one like, however many times it is delivered.
//! * What the OS currently has is `ios_like::command_state_rows` in the app's
//!   own state page (R289): `now_playing_like_enabled` (is the star pressable),
//!   `now_playing_like_active` (is it drawn filled — MediaPlayer's own `active`
//!   bit, read through the `isActive` getter its header declares),
//!   `now_playing_like_web_state` (what the web last pushed) and
//!   `now_playing_like_press_pending` (a press still owed to the webview).
//!   "The like button does not work" is answerable from those four rows without
//!   a device to hand.
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
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicU8, Ordering};
use std::sync::OnceLock;

use block2::{DynBlock, RcBlock};
use objc2::msg_send;
use objc2::rc::Retained;
use objc2::runtime::{AnyClass, AnyObject};
use tauri::{AppHandle, Emitter};

/// The event the shell sends when the OS star is pressed. MUST match
/// `LIKE_EVENT` in `web/src/lib/iosFavs.ts` — the two are one wire, and the
/// web side ignores an event name it does not know.
///
/// The payload is the press's NUMBER (see `PENDING_PRESS`), not just a ping:
/// an unanswered press is delivered again, and the number is how the page tells
/// a second delivery of one press from a second press.
pub const LIKE_EVENT: &str = "mlo-ios-like";

/// The app handle, kept from `register` so the star can be re-asserted from the
/// notification handlers in `src/ios_audio.rs`, which have no handle of their
/// own. Written once, at setup, before any press can happen.
static APP: OnceLock<AppHandle> = OnceLock::new();

/// The press the web UI has not answered yet, as its press NUMBER (0 = no press
/// waiting), and the counter every press is stamped with.
///
/// The star is pressed while the phone is LOCKED, which is precisely when the
/// webview's JavaScript may be parked: the event is handed to the webview the
/// moment it arrives, and if nothing was running to receive it the press is
/// gone — the OS got its "handled", and the user's like never happened. So the
/// press is remembered here and re-sent when the app is next active (see
/// `refresh`), and the memory is cleared by the APP answering: every state push
/// from the web (`set_liked`, i.e. the `set_now_playing_liked` command) means
/// the webview is alive and has the like of its own.
///
/// The number is not bookkeeping, it is the press's IDENTITY, and the web side
/// needs it. A parked webview is not always a DEAF one: WKWebView queues the
/// JavaScript it is handed while the web content process is suspended and runs
/// it the moment the process wakes, so a press can arrive twice — once from the
/// original hand-over and once from `refresh`'s re-delivery. Two bare events
/// would toggle the like twice and land exactly where the user started, which
/// is the one outcome indistinguishable from "the star does nothing"; the
/// number lets `web/src/lib/iosFavs.ts` drop the copy it has already handled,
/// so a press lands once however many times it is delivered.
static PENDING_PRESS: AtomicU64 = AtomicU64::new(0);
static PRESS_SEQ: AtomicU64 = AtomicU64::new(0);

/// What the WEB last said about the current track, as `LAST_LIKED`'s three
/// values. Deliberately not the OS's own bit: see `assert_like_command`.
static LAST_LIKED: AtomicU8 = AtomicU8::new(LIKED_UNKNOWN);
const LIKED_UNKNOWN: u8 = 0;
const LIKED_YES: u8 = 1;
const LIKED_NO: u8 = 2;

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
///
/// The answer is REMEMBERED (`LAST_LIKED`), not just written: it is the web's
/// newest truth about the track, and the re-asserts below have to be able to
/// put it back on a star the system rebuilt behind this app's back. The web
/// stays the only writer — what is replayed is its own last word.
pub fn set_liked(liked: bool) {
    // The web UI is alive and has spoken: whatever press was still waiting for
    // it has been answered, and re-sending it would toggle the like twice.
    PENDING_PRESS.store(0, Ordering::SeqCst);
    LAST_LIKED.store(if liked { LIKED_YES } else { LIKED_NO }, Ordering::SeqCst);
    assert_like_command();
    assert_again_soon();
}

/// Re-assert the star — and hand the webview any press it was never awake to
/// receive.
///
/// Called by the audio-session module at the transitions that rebuild the
/// system's now-playing furniture — becoming active again, and the media server
/// restarting (`src/ios_audio.rs`) — which is also the first moment a parked
/// webview can be expected to be running again: see `PENDING_PRESS`.
pub fn refresh() {
    let pending = PENDING_PRESS.swap(0, Ordering::SeqCst);
    if pending != 0 {
        if let Some(app) = APP.get() {
            if let Err(e) = app.emit(LIKE_EVENT, pending) {
                // Still not deliverable: keep it for the next transition rather
                // than losing the user's press. `fetch_max` and not a plain
                // store, because a press that arrived while this one was in
                // flight is NEWER (the counter only rises) and must win.
                PENDING_PRESS.fetch_max(pending, Ordering::SeqCst);
                eprintln!("[mlo-desktop] could not re-deliver the iOS star press: {e}");
            }
        }
    }
    assert_like_command();
    assert_again_soon();
}

/// One place that writes the star: `enabled`, and `active` as the web last
/// pushed it — or a quiet return when this process has no command centre to
/// write.
///
/// `enabled` is what makes the star pressable; it is re-asserted on every state
/// push and at every transition because the same bit is written by the system's
/// now-playing plumbing (WebKit republishes this app's remote-command set from
/// the web content process whenever the media session changes, and that set is
/// transport-only) — a star a state push cannot turn back on is a star that
/// vanishes mid-album.
///
/// `active` — the star's FILL — is replayed from `LAST_LIKED`, and that is a
/// correction rather than a second opinion. It used to be written ONLY by the
/// push above, on the theory that a re-assert replaying a snapshot would fight
/// the newest push; but the re-assert happens exactly where the system rebuilds
/// the command set, so the fill was the thing that got lost: like a track, come
/// back to the app, and the star is hollow again while the app's own hearts say
/// liked — the owner's "the like button doesn't work" with a handler that had
/// fired correctly. What is written here is the web's OWN last word (updated in
/// the same main-thread breath as the write), so it cannot be out of date, and a
/// push racing it lands after it and wins.
fn assert_like_command() {
    let Some(like) = like_command() else {
        return;
    };
    let state = LAST_LIKED.load(Ordering::SeqCst);
    // SAFETY: as `set_liked`.
    unsafe {
        let _: () = msg_send![&*like, setEnabled: true];
        if state != LIKED_UNKNOWN {
            let _: () = msg_send![&*like, setActive: state == LIKED_YES];
        }
    }
}

/// The star is asserted at the transition AND again a moment later — the second
/// look is what this file's `enabled` comment is about.
///
/// The command centre has two owners writing it in the same breath: this module
/// (the star exists for the APP to offer) and the webview's now-playing
/// plumbing (WebKit rewrites the command set whenever the media session
/// changes, which is asynchronous to the web event that caused it). An
/// `enabled` written at the transition can therefore be cleared by WebKit's own
/// write landing a few milliseconds later — the owner's "there is no star
/// button" with a command centre that says otherwise — and the star stays
/// hidden until the next push. The delayed pass closes that window, replaying
/// the web's own newest state (see `assert_like_command`).
///
/// One timer at a time: `set_liked` fires on every track change and every like
/// press, and a timer per call would be work for nothing. The class is looked
/// up rather than assumed, exactly like the heartbeat in `ios_audio.rs`: a
/// runtime without `NSTimer` loses the second look, never the app. The timer
/// runs on the calling thread's run loop, which is the same constraint the
/// keep-alive carries — every caller here is the main thread (`set_liked` rides
/// a Tauri command the mobile runtime delivers there, `refresh` a UIKit
/// notification) — and an off-main call would only cost the second look.
fn assert_again_soon() {
    static PENDING: AtomicBool = AtomicBool::new(false);
    if PENDING.swap(true, Ordering::SeqCst) {
        return;
    }
    let Some(class) = AnyClass::get(c"NSTimer") else {
        PENDING.store(false, Ordering::SeqCst);
        return;
    };
    let block = RcBlock::new(|_timer: NonNull<AnyObject>| {
        PENDING.store(false, Ordering::SeqCst);
        assert_like_command();
    });
    // The `&DynBlock<…>` binding is the shape `msg_send!` accepts for a block
    // argument, and the run loop retains the scheduled timer (see the
    // heartbeat's timer in `ios_audio.rs`) — so the token is dropped on purpose.
    let block: &DynBlock<dyn Fn(NonNull<AnyObject>) + 'static> = &block;
    // SAFETY: a live NSTimer class and the documented factory selector; one
    // shot, half a second out, with the block above.
    let timer: Option<Retained<AnyObject>> = unsafe {
        msg_send![class, scheduledTimerWithTimeInterval: 0.5f64, repeats: false, block: block]
    };
    // A refused timer must not leave `PENDING` set: the `swap` above is the
    // only thing that lets a second look be scheduled at all, so a `true` left
    // behind here would disable the delayed re-assert for the life of the app —
    // the star would then flap for exactly the reason this function exists.
    if timer.is_none() {
        PENDING.store(false, Ordering::SeqCst);
    }
}

/// Is this command pressable right now, as the system has it? (`"unavailable"`
/// when this process has no such command at all.)
fn command_enabled(command: Option<Retained<AnyObject>>) -> String {
    let Some(command) = command else {
        return "unavailable".into();
    };
    // SAFETY: a live `MPRemoteCommand` (or `MPFeedbackCommand`, which is one);
    // `isEnabled` is its own read-only getter.
    let on: bool = unsafe { msg_send![&*command, isEnabled] };
    (if on { "yes" } else { "no" }).into()
}

/// Is the star FILLED right now, as the system has it? That is `active` on the
/// `MPFeedbackCommand` ("the user already likes this item"), and it is the one
/// half of the like button a device report cannot otherwise settle: the app
/// pushes a state and the OS draws it, and those two are different processes.
///
/// The read is MediaPlayer's own declaration, not a guess: `MPRemoteCommand.h`
/// carries it on the base class — `@property (nonatomic, assign, getter =
/// isActive) BOOL active;` — and `MPRemoteCommandCenter.h` types `likeCommand`
/// as an `MPFeedbackCommand *`, so the object this is sent to is always one that
/// answers it.
fn command_active(command: Option<Retained<AnyObject>>) -> String {
    let Some(command) = command else {
        return "unavailable".into();
    };
    // SAFETY: a live `MPFeedbackCommand` (see above); `isActive` is the getter
    // the header declares for `active`.
    let on: bool = unsafe { msg_send![&*command, isActive] };
    (if on { "yes" } else { "no" }).into()
}

/// The readout rows for the in-app state page (`ios_audio::state`): which of the
/// Now Playing commands this process has, whether the system currently has them
/// ENABLED, what the star's FILL is, what the web last pushed, and whether a
/// press is still held for the webview.
///
/// The owner's reports about this furniture — "there is no star button", the
/// lock screen's ⟲10 / 10⟳ pair instead of a track step, "the like button needs
/// to actually work" — are about what the OS drew from bits that two owners
/// write (see `assert_again_soon`), and neither can be seen from a development
/// box. These rows are the measurement: a `now_playing_like_enabled` of "no"
/// while the phone shows no star is the webview's write having won;
/// `now_playing_like_active` beside `now_playing_like_web_state` is the star's
/// fill against the app's own answer, so "the app thinks it is liked and the OS
/// draws it hollow" is a row pair rather than an argument;
/// `now_playing_like_press_pending` is a press the shell is still holding for a
/// webview that has not answered (the memory `refresh` exists to empty); and a
/// `skip_*` of "yes" is where the ±10 s buttons come from (the web player
/// declares both skip actions unsupported — see the `navigator.mediaSession`
/// effect in `web/src/components/PlayerBar.tsx` — so the same question is
/// answerable one layer up).
pub fn command_state_rows() -> Vec<(String, String)> {
    let Some(center) = command_center() else {
        return vec![
            ("now_playing_like_enabled".into(), "unavailable".into()),
            ("now_playing_like_active".into(), "unavailable".into()),
            ("now_playing_like_web_state".into(), web_state()),
            ("now_playing_like_press_pending".into(), press_pending()),
            ("now_playing_skip_back_enabled".into(), "unavailable".into()),
            ("now_playing_skip_forward_enabled".into(), "unavailable".into()),
        ];
    };
    // SAFETY: messages to the live centre; each is a read-only property
    // returning the command the centre owns.
    let skip_back: Option<Retained<AnyObject>> = unsafe { msg_send![&*center, skipBackwardCommand] };
    let skip_forward: Option<Retained<AnyObject>> = unsafe { msg_send![&*center, skipForwardCommand] };
    vec![
        ("now_playing_like_enabled".into(), command_enabled(like_command())),
        ("now_playing_like_active".into(), command_active(like_command())),
        ("now_playing_like_web_state".into(), web_state()),
        ("now_playing_like_press_pending".into(), press_pending()),
        ("now_playing_skip_back_enabled".into(), command_enabled(skip_back)),
        ("now_playing_skip_forward_enabled".into(), command_enabled(skip_forward)),
    ]
}

/// What the web UI last said about the CURRENT track, in the row's own words —
/// the app's side of the star, against `now_playing_like_active`'s OS side.
fn web_state() -> String {
    match LAST_LIKED.load(Ordering::SeqCst) {
        LIKED_YES => "liked".into(),
        LIKED_NO => "not liked".into(),
        _ => "unknown (nothing pushed yet)".into(),
    }
}

/// The press the shell is still holding for the webview, if any: `none`, or the
/// press number `refresh` will re-deliver.
fn press_pending() -> String {
    match PENDING_PRESS.load(Ordering::SeqCst) {
        0 => "none".into(),
        press => format!("waiting ({press})"),
    }
}

/// Wire the star up once, at app setup: enable the like command, leave the
/// dislike command out of the UI, and attach the handler that hands every press
/// to the web UI.
pub fn register(app: &AppHandle) {
    // Kept for `refresh`: the notification handlers have no handle, and a press
    // that arrived while the webview was parked has to be re-sent from one.
    let _ = APP.set(app.clone());
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
            //
            // Remembered BEFORE the send, because the send is exactly what
            // fails when the phone is locked and the webview is parked: the
            // press is re-delivered the next time the app is active (see
            // `refresh`), and dropped the moment the web answers with a state
            // of its own. The number stamped here travels with BOTH copies of
            // the press, which is what lets the page drop the one it has
            // already handled instead of toggling the like twice (see
            // `PENDING_PRESS`). `fetch_max` rather than a plain store for the
            // same reason as there: the counter only rises, so the newest press
            // must win whatever order these two lines run in.
            let press = PRESS_SEQ.fetch_add(1, Ordering::SeqCst) + 1;
            PENDING_PRESS.fetch_max(press, Ordering::SeqCst);
            if let Err(e) = app.emit(LIKE_EVENT, press) {
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
