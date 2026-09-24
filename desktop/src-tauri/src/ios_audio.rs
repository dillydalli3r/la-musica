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
//! One session, two symptoms. The Now Playing module — the lock screen, Control
//! Center, CarPlay — draws its card only for an app whose audio session is a
//! playback session, and the star `src/ios_like.rs` registers lives on that
//! card. With no category there was no card, so "the like button still isn't on
//! ios" and "audio is muted when app is unfocused" were the same bug.
//!
//! ## What it does
//!
//! * [`activate`] runs once, at setup: category `AVAudioSessionCategoryPlayback`,
//!   mode `AVAudioSessionModeDefault`, no options, then `setActive:YES`.
//! * The category comes from the framework's own exported constant rather than a
//!   copied string, and AVFAudio is linked explicitly: `objc_getClass` only
//!   finds a class whose framework is LOADED.
//! * No category options, deliberately: `MixWithOthers` would let the music play
//!   over whatever else the phone is doing, and a player is not a sound effect.
//! * A session that will not take the category — or will not activate — is
//!   logged, never fatal. The same rule as the star: nothing about the OS's
//!   audio furniture is worth the app, which still plays while it is in front.
//!
//! ## What it deliberately does not do
//!
//! It owns no playback state, no now-playing metadata and no interruption
//! handling: the webview's Media Session publishes title/artist/artwork and
//! answers the card's play/pause/next buttons, and WebKit resumes the session
//! after a call or an alarm by itself. This module's whole job is that the
//! session's CATEGORY is the one a music client needs, from launch on.
//!
//! None of this compiles off iOS: `lib.rs` declares the module behind
//! `#[cfg(target_os = "ios")]`, Android plays through its own audio path, and
//! the desktop targets have no `AVAudioSession` to talk to.

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

/// Put this app's audio in the playback category and activate the session —
/// once, at setup. See the module docs for why both symptoms live here.
pub fn activate() {
    let Some(session) = session() else {
        eprintln!(
            "[mlo-desktop] AVAudioSession is unavailable — playback will stop \
             when the app leaves the foreground"
        );
        return;
    };

    // SAFETY: both statics are AVFAudio's own exported constants (linked above,
    // so the framework is loaded and they are non-null), the receiver is the
    // live session, and `options` is the `AVAudioSessionCategoryOptions` bitset
    // — none of them, since this app mixes with nothing. `error:` is an
    // out-parameter nothing here reads: a refusal is the BOOL, logged below.
    let categorised: bool = unsafe {
        msg_send![
            &*session,
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
        return;
    }

    // SAFETY: as above; `setActive:` takes the BOOL the session is asked to be.
    // Not worth asking when the category did not take: that is the request iOS
    // would be free to ignore.
    let activated: bool =
        unsafe { msg_send![&*session, setActive: true, error: None::<&mut AnyObject>] };
    if !activated {
        eprintln!("[mlo-desktop] the iOS audio session would not activate");
    }
}
