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
//! reports for every other WKWebView host (Cordova's
//! `cordova-plugin-ionic-webview` background-audio threads, WebKit bug
//! reports, and Tauri's own iOS audio issue): the
//! webview stops when the app is backgrounded, and a fresh `play()` in the
//! foreground is what starts the clock again — exactly the owner's pause/play
//! observation. Two things fix it, and both are here or in the app's config:
//!
//! * `backgroundThrottling: "disabled"` on the window (`tauri.conf.json`) —
//!   wry sets WebKit's `WKPreferences.inactiveSchedulingPolicy` to `.none`
//!   (public API, iOS 17+/macOS 14+), so the web content process is not
//!   suspended for being invisible. Long-running audio in a backgrounded
//!   hybrid app is the documented reason that setting exists.
//! * This module's session lifecycle below.
//!
//! ## 4.0.4: the lifecycle itself was the bug
//!
//! 4.0.3 gave the session a lifecycle — the category re-applied and the session
//! activated on every start of playback, deactivated with
//! `NotifyOthersOnDeactivation` on every stop. The owner's next report was
//! blunter than the ones before it: *"Audio isn't playing in the app (pressing
//! play on tracks just makes them pause immediately)."*
//!
//! The two calls were the bug. `setCategory:mode:options:` on a session that is
//! already ACTIVE is Apple's documented "may interrupt audio playback", and the
//! app is always in exactly that state when the call arrives — the web player
//! writes `playing` the moment a row is pressed, so the shell's call lands while
//! the webview's own element is starting (or already playing) into that same
//! session. The interruption stops the element, the element fires `pause`, and
//! the player reads that as "the track ended by itself" — press play, get a
//! pause. The deactivate half is the same fault from the other side: a pause
//! that arrives in the same second as a start (a track change, a refused load,
//! the OS pausing the element) handed the session back mid-startup, and the
//! play that followed began in a session that had just been taken away.
//!
//! What replaced it, in one sentence: **the category is taken once (at setup,
//! while the session is inactive) and the session is activated when playback
//! begins and never handed back while the app lives.**
//!
//! ## 4.1.0's own report: the app process renders nothing of its own
//!
//! The owner, on the build that shipped the lifecycle above: *"Audio just cuts
//! out after tabbing out of the app"* — and the like button on iOS's own Now
//! Playing surfaces still behaving as if nothing had been wired to it. Both
//! reports have one subject, and it is this process.
//!
//! iOS's audio background mode is a promise about the APP: "With this category,
//! your app can also play background audio if you're using the Audio, AirPlay,
//! and Picture in Picture background mode" (Apple, *Configuring Audio Settings
//! for iOS and tvOS*) — and what the runtime keeps alive is an app that IS
//! producing audio. This app's audio is decoded by WebKit's web content
//! process: the session configured here belongs to this process, which renders
//! silence into it and nothing else. So at the one moment iOS asks "is this app
//! playing?", the only truthful answer this process can give is no — the audio
//! that justifies the grant lives in another process, and a backgrounded app
//! with no playback of its own is suspended like any other. The music stops
//! with it, and a suspended app also stops answering the Now Playing module's
//! remote commands, which is why the star reads as unwired rather than as
//! broken (R251, R288).
//!
//! So this module renders something of its OWN while it matters:
//!
//! * [`start_keep_alive`] plays half a second of 16-bit silence — generated in
//!   `silence_wav`, not shipped as an asset — through an `AVAudioPlayer`
//!   looping it at unity volume, on the same playback session the music uses.
//!   Inaudible by construction (the samples are zeros) and real output as far
//!   as the session is concerned, which is the whole point.
//! * [`sync_keep_alive`] is the condition, and it is the narrowest one that
//!   works: **the web player says it is playing AND the app is in the
//!   background**. Started at `DidEnterBackground` (the last moment before iOS
//!   asks the question) and when playback starts while already backgrounded
//!   (the lock screen's play button has no app-state notification to ride on),
//!   stopped the moment either half goes away — coming back to the app, or the
//!   music stopping. Nothing renders in the foreground, so the cost is bounded
//!   to the time the app is out of sight and the user is listening.
//! * It is still not a now-playing writer. `MPNowPlayingInfoCenter` is left
//!   alone: the webview's Media Session publishes title, artist, album and
//!   artwork, and it stays the one source of what the lock screen shows.
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
//!   **activate on the way in, do nothing on the way out**. Activating an
//!   already-active session is a no-op, so a play that arrives while the
//!   session is up cannot interrupt anything; the OS ends the session when the
//!   app does. The audible cost is the documented one — another player paused
//!   by this app does not resume by itself when this app pauses — and it is the
//!   price of a session that cannot be yanked out from under a starting track.
//! * [`register`] attaches the OS notifications that re-assert the session at
//!   the moments it is taken away: `UIApplicationDidEnterBackground` and
//!   `UIApplicationDidBecomeActive` (activate again while playing, in both
//!   directions — a session that is already active is untouched), and
//!   `AVAudioSessionInterruption` (when an interruption ends with
//!   `ShouldResume` the session is re-asserted; when it does not, the next press
//!   of play is what takes it back). `AVAudioSessionMediaServicesWereReset` is
//!   the one transition that also re-takes the CATEGORY — the audio server
//!   restarted, everything must be configured from scratch, and nothing is
//!   playing into the session at that moment (the reset is what stopped it).
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
//! playback as well. The one thing this process does render is the keep-alive
//! above, and only while the app is in the background — silence, for a grant
//! that is otherwise this process's to lose. This module's job is that the
//! app's session is the one a music client needs, at the moments iOS asks the
//! question.
//!
//! None of this compiles off iOS: `lib.rs` declares the module behind
//! `#[cfg(target_os = "ios")]`, Android plays through its own audio path, and
//! the desktop targets have no `AVAudioSession` to talk to. `lib.rs` still
//! registers the `set_playback_active` command on every target (empty body
//! elsewhere) so the web UI can call it unconditionally.

use std::cell::RefCell;
use std::ptr::NonNull;
use std::sync::atomic::{AtomicBool, Ordering};

use block2::{DynBlock, RcBlock};
use objc2::msg_send;
use objc2::rc::Retained;
use objc2::runtime::{AnyClass, AnyObject};
use objc2_foundation::{NSData, NSString};

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
/// tell any app this one interrupted that it may start again. The session is
/// NEVER deactivated while this app lives (see the module docs: handing it back
/// on every pause is what made the session flap under the webview's own
/// playback), so the option is documented here and deliberately unused.
const _NOTIFY_OTHERS_ON_DEACTIVATION: usize = 1;

/// What the web player last said about itself. Plain atomics rather than a
/// mutex: the writers are the main thread (the Tauri command, the notification
/// handlers) and a torn read is impossible for a bool.
static PLAYING: AtomicBool = AtomicBool::new(false);

/// Whether the app is in the background. Written by the two app-state
/// notifications, read by the keep-alive below.
static IN_BACKGROUND: AtomicBool = AtomicBool::new(false);

/// Frames of silence the keep-alive loops: 0.5 s of 16-bit mono at 44.1 kHz.
/// The player loops it (`numberOfLoops = -1`), so the buffer only has to be
/// long enough not to be re-created constantly.
const KEEP_ALIVE_FRAMES: usize = 22_050;

thread_local! {
    /// The keep-alive player while it is running — `AVAudioPlayer` over one
    /// looping half-second of silence, through the same playback session the
    /// webview's music uses. `None` whenever there is nothing to keep alive.
    ///
    /// A thread-local rather than a static: `Retained<AnyObject>` is neither
    /// `Send` nor `Sync` (objc2 says so on purpose — thread affinity is a
    /// property of the concrete class), and every writer here is the main
    /// thread anyway: setup, the two app-state notifications, and the
    /// `set_playback_active` Tauri command, which the mobile runtime delivers
    /// on the main thread like every other sync command.
    static KEEP_ALIVE: RefCell<Option<Retained<AnyObject>>> = const { RefCell::new(None) };
}

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

/// Activate the session. There is deliberately no deactivate: see the module
/// docs on why the session is held for the life of the app.
fn activate(session: &AnyObject) -> bool {
    // SAFETY: as `take_category`; `setActive:withOptions:error:` is the
    // documented activation call, `active` is the BOOL it takes and `options`
    // is an `AVAudioSessionSetActiveOptions` bitset — none of them, since this
    // app never hands the session back.
    unsafe {
        msg_send![
            session,
            setActive: true,
            withOptions: 0usize,
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
///
/// The category is deliberately NOT re-applied here. `setCategory:mode:options:`
/// on a session that is already ACTIVE — which is exactly this app's state
/// while the webview is playing — is Apple's documented "this may interrupt
/// audio playback": measured against the owner's report, the interruption is
/// the webview's own element pausing the moment the category call lands, which
/// is "pressing play just pauses it immediately". Only the media-server reset
/// re-takes the category, because there the whole session is gone and Apple's
/// own recovery rule is to configure it from scratch.
fn reassert() {
    let Some(session) = session() else {
        return;
    };
    if !PLAYING.load(Ordering::SeqCst) {
        return;
    }
    if !activate(&session) {
        eprintln!("[mlo-desktop] the iOS audio session would not activate");
    }
}

/// Put this app's audio in the playback category — once, at setup, while the
/// session is INACTIVE (the one moment a category change is free). See the
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

/// A 0.5 s WAV of 16-bit silence, mono at 44.1 kHz — the keep-alive player's
/// whole input. Built by hand rather than shipped as an asset: a 44-byte
/// canonical header plus zeroed samples is the entire file, and an app whose
/// audio is streamed from a server has no business carrying an audio file in
/// its bundle.
fn silence_wav(frames: usize) -> Vec<u8> {
    const RATE: u32 = 44_100;
    const CHANNELS: u16 = 1;
    const BITS: u16 = 16;
    let block_align = CHANNELS * BITS / 8;
    let data_len = frames as u32 * u32::from(block_align);
    let mut wav = Vec::with_capacity(44 + data_len as usize);
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&(36 + data_len).to_le_bytes());
    wav.extend_from_slice(b"WAVEfmt ");
    wav.extend_from_slice(&16u32.to_le_bytes()); // PCM header size
    wav.extend_from_slice(&1u16.to_le_bytes()); // format 1 = PCM
    wav.extend_from_slice(&CHANNELS.to_le_bytes());
    wav.extend_from_slice(&RATE.to_le_bytes());
    wav.extend_from_slice(&(RATE * u32::from(block_align)).to_le_bytes());
    wav.extend_from_slice(&block_align.to_le_bytes());
    wav.extend_from_slice(&BITS.to_le_bytes());
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&data_len.to_le_bytes());
    wav.resize(44 + data_len as usize, 0); // the samples: silence
    wav
}

/// Start rendering the keep-alive, if it is not already running.
///
/// Never fatal and never noisy: a device with no `AVAudioPlayer` class, a
/// refused `play` — the app keeps playing while it is in front, which is what
/// it did before this existed. Nothing here touches the session's category: it
/// is already `playback` and already active (see `configure` and `reassert`),
/// and re-taking it mid-playback is the documented interruption this module
/// exists to avoid (R268).
fn start_keep_alive() {
    KEEP_ALIVE.with(|slot| {
        let mut slot = slot.borrow_mut();
        if slot.is_some() {
            return;
        }
        let Some(class) = AnyClass::get(c"AVAudioPlayer") else {
            eprintln!(
                "[mlo-desktop] AVAudioPlayer is unavailable — this app process \
                 renders nothing of its own, so iOS may suspend it (and the \
                 playback with it) while it is in the background"
            );
            return;
        };
        let data = NSData::with_bytes(&silence_wav(KEEP_ALIVE_FRAMES));
        // SAFETY: `alloc` then the designated initializer, in that order, with
        // the NSData above and a null error out-parameter (nothing here reads
        // the reason a player could not be built; a refusal is the `None`).
        //
        // Both sends are declared with a RAW pointer return rather than
        // `Retained`: objc2's `msg_send!` only accepts `Retained`/`Option<…>`
        // for a return it can map to an object of a known class, and this
        // class is looked up by name (there is no objc2 binding crate for
        // AVFAudio here). The +1 the initializer hands back is taken over by
        // `Retained::from_raw` below, which is what makes the ownership the
        // same as if it had been typed.
        let player: Option<Retained<AnyObject>> = unsafe {
            let allocated: *mut AnyObject = msg_send![class, alloc];
            if allocated.is_null() {
                None
            } else {
                let made: *mut AnyObject = msg_send![
                    &*allocated,
                    initWithData: &*data,
                    error: None::<&mut AnyObject>
                ];
                if made.is_null() {
                    // A refused initializer: the allocation is nobody's, so it
                    // is given to a Retained and dropped (its +1 released).
                    drop(Retained::from_raw(allocated));
                    None
                } else {
                    Retained::from_raw(made)
                }
            }
        };
        let Some(player) = player else {
            eprintln!("[mlo-desktop] the iOS keep-alive player would not start");
            return;
        };
        // SAFETY: a live AVAudioPlayer, and each call is the documented setter
        // for one of its properties; `numberOfLoops` is an NSInteger (−1 =
        // loop for ever), `volume` a float at unity so the render is real
        // output rather than a muted one, and both `prepareToPlay` and `play`
        // return the BOOL they are declared with.
        let playing: bool = unsafe {
            let _: () = msg_send![&*player, setNumberOfLoops: -1isize];
            let _: () = msg_send![&*player, setVolume: 1.0f32];
            let _: bool = msg_send![&*player, prepareToPlay];
            msg_send![&*player, play]
        };
        if !playing {
            eprintln!("[mlo-desktop] the iOS keep-alive player would not play");
            return;
        }
        *slot = Some(player);
    });
}

/// Stop the keep-alive. Idempotent, and the only place the player is released —
/// dropping the last reference stops it, and `stop` is called first so the
/// stop is explicit rather than a side effect of the release.
fn stop_keep_alive() {
    KEEP_ALIVE.with(|slot| {
        if let Some(player) = slot.borrow_mut().take() {
            // SAFETY: a live AVAudioPlayer; `stop` is the documented call.
            unsafe {
                let _: () = msg_send![&*player, stop];
            }
        }
    });
}

/// Render silence of this process's own exactly while both halves of the
/// condition hold: the WEB PLAYER IS PLAYING and THE APP IS IN THE BACKGROUND.
///
/// The foreground case is deliberately excluded — the webview's own playback is
/// the only sound the user should hear while they are looking at the app — so
/// the keep-alive costs nothing during normal use, and starts at the last
/// moment iOS can still be told (see the module docs on why the app process has
/// to say it at all).
fn sync_keep_alive() {
    if PLAYING.load(Ordering::SeqCst) && IN_BACKGROUND.load(Ordering::SeqCst) {
        start_keep_alive();
    } else {
        stop_keep_alive();
    }
}

/// The web player's play/pause, as the shell's own record of it.
///
/// Playback STARTING is what activates the session (Apple's guidance: never
/// before, so other audio is not interrupted by a launch). Playback stopping
/// does NOT deactivate it, and that is the 4.0.4 correction: the web says
/// "playing" the moment a row is pressed — before the element has really
/// started — so a pause that follows within the same second (a track change,
/// a refused load, the OS pausing the element) used to hand the session back
/// mid-startup, and the play that came right after it started into a session
/// that had just been taken away. Activating an already-active session is a
/// no-op, so holding it costs nothing; the OS ends it when the app does.
///
/// Called from the `set_playback_active` Tauri command, i.e. by
/// `web/src/lib/iosAudio.ts` as the player's `playing` state changes.
///
/// It also drives the keep-alive (`sync_keep_alive`), which is the half of
/// background playback this process can supply itself — a playback that starts
/// while the app is ALREADY in the background (the lock screen's play button)
/// has no app-state notification to ride on.
///
/// And it re-arms the OS star: a playback that begins is exactly the moment
/// WebKit publishes its own remote-command set for the now-playing module from
/// the web content process (`RemoteCommandListenerCocoa::updateSupportedCommands`
/// → `MRMediaRemoteSetSupportedCommands`, whose list is transport-only), so the
/// like command is re-asserted on the way in as well as on every state push
/// (R288).
pub fn set_playing(playing: bool) {
    PLAYING.store(playing, Ordering::SeqCst);
    if playing {
        if let Some(session) = session() {
            if !activate(&session) {
                eprintln!("[mlo-desktop] the iOS audio session would not activate");
            }
        }
        crate::ios_like::refresh();
    }
    sync_keep_alive();
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
/// `Began` needs nothing done — iOS has already stopped the sound, and the
/// webview's own element fires `pause`, which is what clears the player's state
/// through the store (and, over the bridge, this module's `PLAYING`). That is
/// deliberately not duplicated here: the shell's record of what the WEB wanted
/// is only ever written by the web.
///
/// `Ended` re-asserts only when iOS says `ShouldResume`. Without that option
/// the session belongs to whatever took it, and the next press of play is what
/// takes it back (see `set_playing`).
fn on_interruption(notification: &AnyObject) {
    let began = user_info_usize(notification, unsafe { AVAudioSessionInterruptionTypeKey })
        .is_some_and(|kind| kind == INTERRUPTION_BEGAN);
    if began {
        return;
    }
    let may_resume = user_info_usize(notification, unsafe { AVAudioSessionInterruptionOptionKey })
        .is_some_and(|options| options & SHOULD_RESUME != 0);
    if may_resume {
        reassert();
    }
}

/// The audio server restarted. Apple's rule is to reconfigure everything from
/// scratch, and (when the app wants to keep playing) to start the audio again —
/// the session object survives, its configuration does not. This is the ONE
/// place besides setup where the category is taken: nothing is playing into it
/// at this moment (the reset is what killed the audio), so the call cannot
/// interrupt anybody's playback. The star's command is re-asserted in the same
/// breath for the same reason: what the system knew about this app's
/// now-playing furniture is gone.
fn on_media_services_reset() {
    if let Some(session) = session() {
        take_category(&session);
    }
    reassert();
    crate::ios_like::refresh();
}

/// Going to the background while playing is THE transition this module exists
/// for: without an active playback session at that moment, iOS is entitled to
/// treat the app as idle and stop its audio.
///
/// The session is one half of that answer and the keep-alive is the other: a
/// backgrounded app is kept running because it is PLAYING AUDIO, and the audio
/// the user hears is decoded by WebKit's process, not this one. Rendering the
/// keep-alive here — the last moment before the app is treated as idle — is
/// what puts this process on the right side of that question (see the module
/// docs).
fn on_background() {
    IN_BACKGROUND.store(true, Ordering::SeqCst);
    reassert();
    sync_keep_alive();
}

/// Coming back: the session was suspended or handed out while the app was away,
/// so the moment the app is active again it is put back. (`DidBecomeActive`
/// rather than `WillEnterForeground`: it fires once the app really is frontmost,
/// which is also when the star's command must be pressable again.)
///
/// The keep-alive stops in the same breath: the webview is audible again by
/// itself, and a second renderer for the same music is battery for nothing.
fn on_active() {
    IN_BACKGROUND.store(false, Ordering::SeqCst);
    reassert();
    sync_keep_alive();
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
    observe(
        unsafe { UIApplicationDidEnterBackgroundNotification },
        |_: &AnyObject| on_background(),
    );
    observe(
        unsafe { UIApplicationDidBecomeActiveNotification },
        |_: &AnyObject| on_active(),
    );
    observe(
        unsafe { AVAudioSessionInterruptionNotification },
        on_interruption,
    );
    observe(
        unsafe { AVAudioSessionMediaServicesWereResetNotification },
        |_: &AnyObject| on_media_services_reset(),
    );
}
