//! The iOS audio session: music that outlives the app being in front.
//!
//! ## Why this exists
//!
//! A WKWebView plays through whatever `AVAudioSession` category the app has
//! configured, and until this module existed the app never configured one. The
//! default category is `soloAmbient`, which iOS treats as incidental UI sound:
//! it is muted the moment the app stops being the frontmost app, and by the
//! ringer switch. That is the owner's 4.0.0 report, verbatim — "audio is muted
//! when app is unfocused".
//!
//! `UIBackgroundModes: [audio]` in `Info.plist` is the app's *permission* to
//! play in the background; the session's category is what makes it true. Apple
//! states the pair in its own guide — "With this category, your app can also
//! play background audio if you're using the Audio, AirPlay, and Picture in
//! Picture background mode" (Configuring Audio Settings for iOS and tvOS) —
//! which is why the plist key alone changed nothing about the muting.
//!
//! ## Why a category was not enough (4.0.2's own report)
//!
//! 4.0.2 set the category once at setup and called `setActive:YES` with it. The
//! owner's next report: "audio still stops playing from the app when it tabs
//! out… if I pause / play again the audio works, then doesn't work after
//! entering and exiting the app again."
//!
//! That is the WKWebView's *web content process* being suspended, not the
//! category: WebKit keeps that process alive for a web page only while it can
//! justify it, and a page that is merely playing audio is a case iOS has to be
//! told about on every transition. The same three facts are in the field
//! reports for every other WKWebView host (Cordova's `WKCWebView` background
//! audio threads, WebKit bug reports, and Tauri's own iOS audio issue): the
//! webview stops when the app is backgrounded, and a fresh `play()` in the
//! foreground is what starts the clock again — exactly the owner's pause/play
//! observation. Two things fix it, and both are here or in the app's config:
//!
//! * `backgroundThrottling: "disabled"` on the window (`tauri.conf.json`) —
//!   wry sets WebKit's `WKPreferences.inactiveSchedulingPolicy` to `.none`
//!   (public API, iOS 17+/macOS 14+), so the web content process is not
//!   suspended for being invisible. Long-running audio in a backgrounded
//!   hybrid app is the documented reason that setting exists.
//! * This module's session lifecycle below: the session is ACTIVATED when
//!   playback starts and DEACTIVATED when it stops, and re-asserted on the
//!   transitions where iOS takes a backgrounded app's session away
//!   (backgrounding, becoming active again, an interruption ending, the media
//!   server restarting).
//!
//! ## What it does
//!
//! * [`configure`] runs once at setup: category `AVAudioSessionCategoryPlayback`,
//!   mode `AVAudioSessionModeDefault`, no options. It does NOT activate:
//!   Apple's own guidance is to activate when playback begins, "to ensure that
//!   you won't prematurely interrupt any other background audio that may be
//!   playing" — and a launch-time activation is exactly that, on every launch.
//! * [`set_playing`] is called by the web UI (through the
//!   `set_playback_active` Tauri command) as the player starts and stops:
//!   activate on the way in, deactivate on the way out with
//!   `NotifyOthersOnDeactivation` so whatever the user was listening to before
//!   comes back.
//! * [`register`] attaches the OS notifications that re-assert the session at
//!   the four moments it is taken away: `UIApplicationDidEnterBackground` and
//!   `UIApplicationDidBecomeActive` (re-assert while playing, in both
//!   directions), `AVAudioSessionInterruption` (an interruption is iOS taking
//!   the session; when it ends with `ShouldResume` the session is re-asserted,
//!   and when it does not, the wish to play is dropped rather than stolen back
//!   from whatever took it), and `AVAudioSessionMediaServicesWereReset` (the
//!   audio server restarted: everything must be set again from scratch).
//! * The category comes from the framework's own exported constant rather than
//!   a copied string, and AVFAudio is linked explicitly: `objc_getClass` only
//!   finds a class whose framework is LOADED.
//! * No category options, deliberately: `MixWithOthers` would let the music play
//!   over whatever else the phone is doing, and a player is not a sound effect.
//! * A session that will not take the category — or will not activate — is
//!   logged, never fatal. The same rule as the star: nothing about the OS's
//!   audio furniture is worth the app, which still plays while it is in front.
//! * Every notification name and user-info key comes from the framework's own
//!   exported symbol, for the same reason as the category: the string's value
//!   is an implementation detail, the symbol is what Apple documents.
//!
//! ## What it deliberately does not do
//!
//! It owns no playback state of its own beyond "the web player says it is
//! playing", no now-playing metadata and no remote commands: the webview's
//! Media Session publishes title/artist/artwork and answers the card's
//! play/pause/next buttons, and WebKit activates the session for its own
//! playback as well. This module's job is that the app's session is the one a
//! music client needs, at the moments iOS asks the question.
//!
//! None of this compiles off iOS: `lib.rs` declares the module behind
//! `#[cfg(target_os = "ios")]`, Android plays through its own audio path, and
//! the desktop targets have no `AVAudioSession` to talk to. `lib.rs` still
//! registers the `set_playback_active` command on every target (empty body
//! elsewhere) so the web UI can call it unconditionally.

use std::sync::atomic::{AtomicBool, Ordering};
use std::ptr::NonNull;

use block2::{DynBlock, RcBlock};
use objc2::msg_send;
use objc2::rc::Retained;
use objc2::runtime::{AnyClass, AnyObject};
use objc2_foundation::NSString;

/// AVFAudio is where `AVAudioSession` lives (it was AVFoundation before iOS 14;
/// this app's floor is 14.0 — `bundle.iOS.minimumSystemVersion`). Linking it is
/// not ceremony: the dynamic class lookup below finds nothing until the
/// framework is loaded.
#[link(name = "AVFAudio", kind = "framework")]
extern "C" {
    /// `NSString *const AVAudioSessionCategoryPlayback` — read, not copied: the
    /// string's value is an implementation detail of AVFAudio, and this symbol
    /// is what Apple's documentation names.
    static AVAudioSessionCategoryPlayback: &'static NSString;
    /// `NSString *const AVAudioSessionModeDefault`
    static AVAudioSessionModeDefault: &'static NSString;
    /// `NSNotificationName const AVAudioSessionInterruptionNotification`
    static AVAudioSessionInterruptionNotification: &'static NSString;
    /// `NSNotificationName const AVAudioSessionMediaServicesWereResetNotification`
    static AVAudioSessionMediaServicesWereResetNotification: &'static NSString;
    /// `NSString *const AVAudioSessionInterruptionTypeKey` — the user-info key
    /// whose value is the `AVAudioSessionInterruptionType`.
    static AVAudioSessionInterruptionTypeKey: &'static NSString;
    /// `NSString *const AVAudioSessionInterruptionOptionKey` — the user-info
    /// key whose value carries the `AVAudioSessionInterruptionOptions` bits.
    static AVAudioSessionInterruptionOptionKey: &'static NSString;
}

/// UIKit posts these for a scene-based app too, which is what Tauri's iOS shell
/// is; observing the app-level pair is the whole lifecycle this module needs.
#[link(name = "UIKit", kind = "framework")]
extern "C" {
    /// `NSNotificationName const UIApplicationDidEnterBackgroundNotification`
    static UIApplicationDidEnterBackgroundNotification: &'static NSString;
    /// `NSNotificationName const UIApplicationDidBecomeActiveNotification`
    static UIApplicationDidBecomeActiveNotification: &'static NSString;
}

/// `AVAudioSessionInterruptionTypeBegan` — an interruption STARTED (a call, an
/// alarm, another app taking the session). Its sibling `…Ended` is 0, which is
/// why the comparison below is written as "not began" rather than "is ended".
const INTERRUPTION_BEGAN: usize = 1;

/// `AVAudioSessionInterruptionOptionShouldResume` — iOS is telling us it is
/// polite to resume. Without it the interruption was somebody else's turn and
/// this app must not take the session back.
const SHOULD_RESUME: usize = 1;

/// `AVAudioSessionSetActiveOptionNotifyOthersOnDeactivation` — on the way out,
/// tell any app this one interrupted that it may start again. Deactivating
/// silently is what leaves another player paused for no reason.
const NOTIFY_OTHERS_ON_DEACTIVATION: usize = 1;

/// What the web player last said about itself. Plain atomics rather than a
/// mutex: the writers are the main thread (the Tauri command, the notification
/// handlers) and a torn read is impossible for a bool.
static PLAYING: AtomicBool = AtomicBool::new(false);

/// Whether the session is active because THIS module activated it, so a stop
/// only deactivates a session we are the ones holding — WebKit activates the
/// session for its own playback, and taking that away is not ours to do.
static ACTIVE_BY_US: AtomicBool = AtomicBool::new(false);

/// The process-wide `AVAudioSession`, or `None` when this process has none at
/// all (a class that did not load, a stripped runtime): the caller logs rather
/// than taking the app down over the OS's own furniture.
fn session() -> Option<Retained<AnyObject>> {
    let class = AnyClass::get(c"AVAudioSession")?;
    // SAFETY: the class was just looked up and `sharedInstance` is the
    // singleton accessor with no arguments.
    unsafe { msg_send![class, sharedInstance] }
}

/// Ask for the playback category. `true` when the session took it.
fn take_category(session: &AnyObject) -> bool {
    // SAFETY: both statics are AVFAudio's own exported constants (linked above,
    // so the framework is loaded and they are non-null), the receiver is the
    // live session, and `options` is the `AVAudioSessionCategoryOptions` bitset
    // — none of them, since this app mixes with nothing. `error:` is an
    // out-parameter nothing here reads: a refusal is the BOOL.
    let categorised: bool = unsafe {
        msg_send![
            session,
            setCategory: AVAudioSessionCategoryPlayback,
            mode: AVAudioSessionModeDefault,
            options: 0usize,
            error: None::<&mut AnyObject>
        ]
    };
    if !categorised {
        eprintln!(
            "[mlo-desktop] the iOS audio session would not take the playback \
             category — playback will stop when the app leaves the foreground"
        );
    }
    categorised
}

/// Activate or deactivate. Deactivating carries `NotifyOthersOnDeactivation`,
/// so an app this one interrupted is free to resume.
fn set_active(session: &AnyObject, active: bool) -> bool {
    let options = if active { 0usize } else { NOTIFY_OTHERS_ON_DEACTIVATION };
    // SAFETY: as `take_category`; `setActive:withOptions:error:` is the
    // documented activation call, `active` is the BOOL it takes and `options`
    // is an `AVAudioSessionSetActiveOptions` bitset.
    unsafe {
        msg_send![
            session,
            setActive: active,
            withOptions: options,
            error: None::<&mut AnyObject>
        ]
    }
}

/// Put the session back the way a playing app needs it, if the app is playing.
///
/// Called at every transition where iOS takes a backgrounded app's session away
/// (app state changes, an interruption ending, the audio server restarting).
/// A no-op while nothing is playing: the category is still set, but a session
/// is not activated for a player that is sitting still — that would interrupt
/// whatever the user is actually listening to.
fn reassert() {
    let Some(session) = session() else {
        return;
    };
    if !take_category(&session) {
        return;
    }
    if !PLAYING.load(Ordering::SeqCst) {
        return;
    }
    if set_active(&session, true) {
        ACTIVE_BY_US.store(true, Ordering::SeqCst);
    } else {
        eprintln!("[mlo-desktop] the iOS audio session would not activate");
    }
}

/// Put this app's audio in the playback category — once, at setup. See the
/// module docs for why activation itself is NOT here.
pub fn configure() {
    let Some(session) = session() else {
        eprintln!(
            "[mlo-desktop] AVAudioSession is unavailable — playback will stop \
             when the app leaves the foreground"
        );
        return;
    };
    take_category(&session);
}

/// The web player's play/pause, as the shell's own record of it: activate the
/// session when playback starts (Apple's guidance: activate when playback
/// begins, never before, so other audio is not interrupted by a launch), and
/// hand it back — with `NotifyOthersOnDeactivation` — when playback stops.
///
/// Called from the `set_playback_active` Tauri command, i.e. by
/// `web/src/lib/iosAudio.ts` as the player's `playing` state changes.
pub fn set_playing(playing: bool) {
    PLAYING.store(playing, Ordering::SeqCst);
    let Some(session) = session() else {
        return;
    };
    if playing {
        if !take_category(&session) {
            return;
        }
        if set_active(&session, true) {
            ACTIVE_BY_US.store(true, Ordering::SeqCst);
        } else {
            eprintln!("[mlo-desktop] the iOS audio session would not activate");
        }
    } else if ACTIVE_BY_US.swap(false, Ordering::SeqCst) {
        // Only a session this module activated: WebKit's own activation for
        // its playback is not ours to take away.
        set_active(&session, false);
    }
}

/// The value at `key` in a notification's `userInfo`, when it is a number.
///
/// `userInfo` and `objectForKey:` both return BORROWED references (`NSDictionary`
/// follows the ownership conventions, not the copy ones), so nothing here is
/// wrapped in a `Retained`: the value is read inside the call and dropped with
/// the notification, exactly as the framework intends.
fn user_info_usize(notification: &AnyObject, key: &NSString) -> Option<usize> {
    // SAFETY: the receiver is the live NSNotification a notification centre
    // just handed us; both messages are the documented accessors and neither
    // transfers ownership. A `null` result (no user info, no such key) is
    // checked before the value is used.
    unsafe {
        let info: *mut AnyObject = msg_send![notification, userInfo];
        if info.is_null() {
            return None;
        }
        let value: *mut AnyObject = msg_send![info, objectForKey: key];
        if value.is_null() {
            return None;
        }
        Some(msg_send![value, unsignedIntegerValue])
    }
}

/// An interruption: iOS taking the session (a call, an alarm, another app).
///
/// `Began` means the sound has already stopped — the app only forgets that it
/// was the one holding the session. `Ended` re-asserts only when iOS says
/// `ShouldResume`: without that option the session belongs to whatever took it,
/// and the wish to play is dropped instead of fought over.
fn on_interruption(notification: &AnyObject) {
    let began = user_info_usize(notification, unsafe { AVAudioSessionInterruptionTypeKey })
        .is_some_and(|kind| kind == INTERRUPTION_BEGAN);
    if began {
        ACTIVE_BY_US.store(false, Ordering::SeqCst);
        return;
    }
    let may_resume =
        user_info_usize(notification, unsafe { AVAudioSessionInterruptionOptionKey })
            .is_some_and(|options| options & SHOULD_RESUME != 0);
    if may_resume {
        reassert();
    } else {
        PLAYING.store(false, Ordering::SeqCst);
    }
}

/// The audio server restarted. Apple's rule is to reconfigure everything from
/// scratch, and (when the app wants to keep playing) to start the audio again —
/// the session object survives, its configuration does not. The star's command
/// is re-asserted in the same breath for the same reason: what the system knew
/// about this app's now-playing furniture is gone.
fn on_media_services_reset() {
    reassert();
    crate::ios_like::refresh();
}

/// Going to the background while playing is THE transition this module exists
/// for: without an active playback session at that moment, iOS is entitled to
/// treat the app as idle and stop its audio.
fn on_background() {
    reassert();
}

/// Coming back: the session was suspended or handed out while the app was away,
/// so the moment the app is active again it is put back. (`DidBecomeActive`
/// rather than `WillEnterForeground`: it fires once the app really is frontmost,
/// which is also when the star's command must be pressable again.)
fn on_active() {
    reassert();
    crate::ios_like::refresh();
}

/// Observe one notification by name. The centre copies the block (its contract
/// for `addObserverForName:object:queue:usingBlock:`), so the block's lifetime is
/// the observer's, and the returned token is deliberately leaked: the
/// observers live exactly as long as the app does, and nothing here ever
/// removes one. Passing `nil` for both `object` and `queue` means "every
/// sender, on the posting thread" — which for all three of these is the main
/// thread.
fn observe(name: &'static NSString, handler: fn(&AnyObject)) {
    let Some(center_class) = AnyClass::get(c"NSNotificationCenter") else {
        eprintln!("[mlo-desktop] NSNotificationCenter is unavailable — the iOS audio session will not survive interruptions");
        return;
    };
    // SAFETY: the class was just looked up and `defaultCenter` is the singleton
    // accessor with no arguments.
    let center: Option<Retained<AnyObject>> = unsafe { msg_send![center_class, defaultCenter] };
    let Some(center) = center else {
        return;
    };
    let block = RcBlock::new(move |notification: NonNull<AnyObject>| {
        // SAFETY: the parameter is the NSNotification the centre passes to the
        // block, live for the duration of the call, and the handler only reads
        // it (`userInfo`) or ignores it.
        handler(unsafe { notification.as_ref() });
    });
    // The `&DynBlock<…>` binding is the shape `msg_send!` accepts for a block
    // argument (the same one `src/ios_like.rs` uses for the star's handler).
    let block: &DynBlock<dyn Fn(NonNull<AnyObject>) + 'static> = &block;
    // SAFETY: the receiver is the live centre; `name` is a framework constant;
    // `object`/`queue` are nil (any sender, posting thread); the block matches
    // the `void (^)(NSNotification *)` the method takes.
    let token: Option<Retained<AnyObject>> = unsafe {
        msg_send![
            &center,
            addObserverForName: name,
            object: None::<&AnyObject>,
            queue: None::<&AnyObject>,
            usingBlock: block
        ]
    };
    if let Some(token) = token {
        // Deliberate: the observer outlives this call by design, and the token
        // is only needed to remove one.
        core::mem::forget(token);
    }
}

/// Wire the session's lifecycle up once, at app setup: the app-state pair, the
/// interruption pair, and a media-server restart. See the module docs for what
/// each one is for.
pub fn register() {
    observe(unsafe { UIApplicationDidEnterBackgroundNotification }, |_: &AnyObject| {
        on_background()
    });
    observe(unsafe { UIApplicationDidBecomeActiveNotification }, |_: &AnyObject| on_active());
    observe(unsafe { AVAudioSessionInterruptionNotification }, on_interruption);
    observe(
        unsafe { AVAudioSessionMediaServicesWereResetNotification },
        |_: &AnyObject| on_media_services_reset(),
    );
}
