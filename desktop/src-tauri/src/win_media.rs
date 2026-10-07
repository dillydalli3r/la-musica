//! Windows' own media session: the card Windows 11's media flyout draws for
//! this app ("now playing", Quick Settings, the media keys, a headset's
//! buttons).
//!
//! ## Why the shell publishes the session instead of the webview
//!
//! The web player already drives `navigator.mediaSession`, and inside WebView2
//! that does reach Windows — but under the RUNTIME's identity, not ours.
//! WebView2's Media Session hands System Media Transport Controls a hidden
//! window of its own process (`gfx::SingletonHwnd`, reached from
//! `content/browser/media/media_keys_listener_manager_impl.cc`), so Windows
//! resolves the session's `SourceAppUserModelId` from a process that carries
//! no Application User Model ID and answers `msedgewebview2.exe` — measured on
//! the running app, with this app's AUMID set on the process and already
//! stamped on the installer's shortcuts. Windows 11's flyout resolves that id
//! to a registered app, resolves nothing, and labels the card **"Unknown
//! app"**.
//!
//! It is not ours to change: the session belongs to another process, and no
//! Chromium switch sets an AUMID (there is no `--app-user-model-id`), which is
//! also what the upstream request has said since 2022
//! (MicrosoftEdge/WebView2Feedback#2236, "tracked", no work happening). The
//! measurement is recorded in `docs/release-notes/release-notes-5.2.0.md`.
//!
//! So the shell publishes the session itself, the way Chromium publishes one
//! for its own window: `ISystemMediaTransportControlsInterop::GetForWindow` on
//! the app's own HWND. Windows then resolves the id through THIS process,
//! which names itself `com.musiclibraryoptimizer.lamusica` in `main.rs` and
//! has that id on the installer's Start Menu shortcut — so the flyout draws
//! "la musica". The resolution was checked directly on this machine:
//! `shell:AppsFolder\com.musiclibraryoptimizer.lamusica` answers "la musica",
//! while `shell:AppsFolder\msedgewebview2.exe` does not exist at all.
//! [`register_shell_name`] adds the same name to the per-user AUMID registry
//! key, which is what makes an unpackaged/dev build resolve too.
//!
//! ## What that costs, and where the other half is
//!
//! Metadata and transport are free when the webview owns the session, because
//! the browser derives them from the page's Media Session. Owning it means the
//! shell has to carry both across the bridge itself, exactly as the iOS bridge
//! carries the audio session:
//!
//! * the page pushes what it is playing through the `set_now_playing` command
//!   ([`NowPlaying`]), which replaces the metadata, the playback status, the
//!   timeline and the enabled transport buttons;
//! * a press in the flyout, on a media key or on a headset arrives as
//!   [`Press`] and leaves the shell as the `mlo-media-key` event, which the
//!   page runs through the same handlers `navigator.mediaSession` used to
//!   receive (see `web/src/lib/winMedia.ts`).
//!
//! The webview's own session is switched OFF in `tauri.conf.json`
//! (`additionalBrowserArgs`): with both alive the flyout would draw two cards
//! for one song, one of them still "Unknown app". `HardwareMediaKeyHandling`
//! is the switch, and it was checked both ways on a real Chromium — with it
//! disabled the page still plays and the OS card is gone.
//!
//! ## Threading
//!
//! The session belongs to the thread that created it (the main thread, in
//! `lib.rs`'s setup hook) and every call below happens there: the command hands
//! its payload to `run_on_main_thread`, and the OS delivers button presses on
//! its own thread but only reaches [`attach`]'s callback, which touches
//! nothing here. Nothing in this module is `Send`, and nothing needs to be.

use std::cell::RefCell;
use std::ffi::c_void;
use std::sync::Arc;

use serde::Deserialize;
use windows::core::{Result as WinResult, HSTRING};
use windows::Foundation::{TimeSpan, TypedEventHandler, Uri};
use windows::Media::{
    MediaPlaybackStatus, MediaPlaybackType, MusicDisplayProperties,
    PlaybackPositionChangeRequestedEventArgs, SystemMediaTransportControls,
    SystemMediaTransportControlsButton, SystemMediaTransportControlsButtonPressedEventArgs,
    SystemMediaTransportControlsDisplayUpdater, SystemMediaTransportControlsTimelineProperties,
};
use windows::Storage::Streams::RandomAccessStreamReference;
use windows::Win32::Foundation::HWND;
use windows::Win32::System::WinRT::{
    ISystemMediaTransportControlsInterop, RoGetActivationFactory, RoInitialize,
    RO_INIT_MULTITHREADED,
};

/// The runtime class whose interop interface hands out a window's session.
const SMTC_CLASS: &str = "Windows.Media.SystemMediaTransportControls";

/// The process's Application User Model ID — the string Windows resolves the
/// media card's name through. MUST match `"identifier"` in
/// `tauri.conf.json` (and the shortcuts the installer writes, which are
/// stamped with it): it is the same one string `main.rs` hands to
/// [`name_the_process`] and [`register_shell_name`].
pub const APP_USER_MODEL_ID: &str = "com.musiclibraryoptimizer.lamusica";

/// One press from the OS, named exactly as the web player's Media Session
/// actions are — the page runs it through the handlers it already has.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Press {
    Play,
    Pause,
    Next,
    Previous,
    SeekForward,
    SeekBackward,
    /// A scrub on the flyout's own progress bar, in seconds.
    SeekTo(f64),
}

impl Press {
    /// The `action` field of the `mlo-media-key` payload.
    pub fn action(self) -> &'static str {
        match self {
            Press::Play => "play",
            Press::Pause => "pause",
            Press::Next => "nexttrack",
            Press::Previous => "previoustrack",
            Press::SeekForward => "seekforward",
            Press::SeekBackward => "seekbackward",
            Press::SeekTo(_) => "seekto",
        }
    }

    /// The `seconds` field: only a scrub has one.
    pub fn seconds(self) -> Option<f64> {
        match self {
            Press::SeekTo(t) => Some(t),
            _ => None,
        }
    }
}

/// What the page is playing, as the page reports it — the whole payload of the
/// `set_now_playing` command.
///
/// It is the same state the player already hands `navigator.mediaSession`
/// (see `PlayerBar.tsx`'s metadata / `playbackState` / `setPositionState`
/// effects), so the page computes nothing twice. `title` and `artist` absent
/// (or null) means "nothing is loaded": the card is closed and cleared rather
/// than left showing the previous track.
#[derive(Clone, Debug, Default, Deserialize)]
pub struct NowPlaying {
    pub title: Option<String>,
    pub artist: Option<String>,
    pub album: Option<String>,
    /// The artwork URL the OS fetches itself — the same URL the page gives
    /// `MediaMetadata.artwork`, i.e. one the bar is already drawing (so it is
    /// in the webview's cache, and on loopback it needs no credentials: the
    /// server's login gate never asks a request from this machine).
    pub artwork: Option<String>,
    /// The element is producing sound (not "the user pressed play").
    pub playing: bool,
    pub position: f64,
    pub duration: f64,
    pub rate: f64,
    /// Whether the queue can step either way — what enables the flyout's
    /// ⏮/⏭.
    #[serde(default)]
    pub next: bool,
    #[serde(default)]
    pub previous: bool,
}

/// The pieces of the session this module keeps writing to, plus the two
/// delegates the OS calls back through (held so nothing can collect them, and
/// so a second `attach` cannot leave a live registration behind).
struct Session {
    smtc: SystemMediaTransportControls,
    updater: SystemMediaTransportControlsDisplayUpdater,
    music: MusicDisplayProperties,
    /// The artwork URL the card is currently showing, so a push that only
    /// moves the playhead does not rebuild the stream reference (the OS
    /// fetches that URL, and re-pointing it every second would restart the
    /// fetch).
    artwork: Option<String>,
    _button_pressed: TypedEventHandler<
        SystemMediaTransportControls,
        SystemMediaTransportControlsButtonPressedEventArgs,
    >,
    _position_requested: TypedEventHandler<
        SystemMediaTransportControls,
        PlaybackPositionChangeRequestedEventArgs,
    >,
}

thread_local! {
    /// The main thread's session. `thread_local` rather than a global `Mutex`
    /// on purpose: this is the thread the COM object was created on, and
    /// saying so in the storage is what keeps every call above legal.
    static SESSION: RefCell<Option<Session>> = const { RefCell::new(None) };
}

/// Publish the session for `hwnd` and route its presses to `on_press`.
///
/// Called once, from setup, on the main thread. Every failure is reported and
/// survivable: a shell with no OS media card is a worse shell, not a broken
/// one, and the webview's own session (switched off in `tauri.conf.json`) is
/// the only thing standing in for it.
pub fn attach(hwnd: HWND, on_press: impl Fn(Press) + Send + Sync + 'static) -> WinResult<()> {
    // A thread already in a WinRT apartment (tao initialises one, as STA)
    // answers RPC_E_CHANGED_MODE — not a failure here: the session only needs
    // an initialised apartment, not this one's init type.
    unsafe {
        let _ = RoInitialize(RO_INIT_MULTITHREADED);
    }
    let interop: ISystemMediaTransportControlsInterop =
        unsafe { RoGetActivationFactory(&HSTRING::from(SMTC_CLASS)) }?;
    let smtc: SystemMediaTransportControls = unsafe { interop.GetForWindow(hwnd) }?;
    let updater = smtc.DisplayUpdater()?;
    updater.SetType(MediaPlaybackType::Music)?;
    let music = updater.MusicProperties()?;

    // Two delegates, one press channel. `Arc` because each one is called by
    // the OS on whichever thread it likes, and the caller's closure is shared.
    let on_press = Arc::new(on_press);
    let buttons = on_press.clone();
    let button_pressed = TypedEventHandler::<
        SystemMediaTransportControls,
        SystemMediaTransportControlsButtonPressedEventArgs,
    >::new(move |_sender, args| {
        if let Ok(args) = args.ok() {
            if let Ok(button) = args.Button() {
                if let Some(press) = press_of(button) {
                    buttons(press);
                }
            }
        }
        Ok(())
    });
    let scrubs = on_press;
    let position_requested = TypedEventHandler::<
        SystemMediaTransportControls,
        PlaybackPositionChangeRequestedEventArgs,
    >::new(move |_sender, args| {
        if let Ok(args) = args.ok() {
            if let Ok(position) = args.RequestedPlaybackPosition() {
                scrubs(Press::SeekTo(seconds(position)));
            }
        }
        Ok(())
    });

    smtc.SetIsEnabled(true)?;
    // The three buttons the flyout draws for a music session. Stop, record and
    // the channel buttons stay off: they are not things this app does, and an
    // enabled button that does nothing is worse than a missing one. The two
    // track buttons are enabled by the state the page pushes (`NowPlaying`).
    smtc.SetIsPlayEnabled(true)?;
    smtc.SetIsPauseEnabled(true)?;
    smtc.SetIsNextEnabled(false)?;
    smtc.SetIsPreviousEnabled(false)?;
    // Scrubbing is an event of its own, not a button: this is what makes the
    // card's progress bar report a new position instead of drawing one.
    smtc.PlaybackPositionChangeRequested(&position_requested)?;
    smtc.ButtonPressed(&button_pressed)?;

    SESSION.with(|cell| {
        *cell.borrow_mut() = Some(Session {
            smtc,
            updater,
            music,
            artwork: None,
            _button_pressed: button_pressed,
            _position_requested: position_requested,
        })
    });
    Ok(())
}

/// Push the page's state onto the OS card. A no-op without a session (an
/// `attach` that failed, or a call from a target with no such session).
pub fn update(now_playing: &NowPlaying) {
    SESSION.with(|cell| {
        let mut cell = cell.borrow_mut();
        let Some(session) = cell.as_mut() else { return };
        if let Err(e) = session.apply(now_playing) {
            eprintln!("[mlo-desktop] media session update failed: {e}");
        }
    });
}

impl Session {
    fn apply(&mut self, np: &NowPlaying) -> WinResult<()> {
        if np.title.is_none() && np.artist.is_none() {
            self.artwork = None;
            self.updater.ClearAll()?;
            self.smtc.SetPlaybackStatus(MediaPlaybackStatus::Closed)?;
            return Ok(());
        }
        if self.artwork != np.artwork {
            // The previous card's art must not survive into a track that has
            // none of its own — and the only way to take a thumbnail back is
            // to clear the updater, which takes the metadata with it, so the
            // properties are re-fetched and the identity set again below.
            self.artwork = np.artwork.clone();
            self.updater.ClearAll()?;
            self.updater.SetType(MediaPlaybackType::Music)?;
            self.music = self.updater.MusicProperties()?;
        }
        self.music
            .SetTitle(&HSTRING::from(np.title.as_deref().unwrap_or_default()))?;
        self.music
            .SetArtist(&HSTRING::from(np.artist.as_deref().unwrap_or_default()))?;
        self.music
            .SetAlbumTitle(&HSTRING::from(np.album.as_deref().unwrap_or_default()))?;
        if let Some(url) = np.artwork.as_deref() {
            // A URL, not bytes: the OS fetches the cover itself, the way it
            // already did for `MediaMetadata.artwork`.
            let uri = Uri::CreateUri(&HSTRING::from(url))?;
            self.updater
                .SetThumbnail(&RandomAccessStreamReference::CreateFromUri(&uri)?)?;
        }
        self.updater.Update()?;
        self.smtc.SetPlaybackStatus(if np.playing {
            MediaPlaybackStatus::Playing
        } else {
            MediaPlaybackStatus::Paused
        })?;
        self.smtc.SetIsNextEnabled(np.next)?;
        self.smtc.SetIsPreviousEnabled(np.previous)?;
        // A rate of zero is refused by `put_PlaybackRate` ("must be greater
        // than 0"), and one refused property write would take the metadata and
        // the timeline of this same push down with it — so a player that
        // reports nothing usable still moves the card.
        self.smtc.SetPlaybackRate(np.rate.max(0.1))?;

        // The card's progress bar. Windows wants the four bounds plus where the
        // playhead is; a duration the page does not know yet (a track that has
        // not decoded) is left at zero rather than guessed, which is what keeps
        // the bar from claiming a length the track does not have.
        let start = TimeSpan { Duration: 0 };
        let end = TimeSpan {
            Duration: to_time_span(np.duration.max(0.0)),
        };
        let position = TimeSpan {
            Duration: to_time_span(np.position.clamp(0.0, np.duration.max(0.0))),
        };
        let timeline = SystemMediaTransportControlsTimelineProperties::new()?;
        timeline.SetStartTime(start)?;
        timeline.SetEndTime(end)?;
        timeline.SetMinSeekTime(start)?;
        timeline.SetMaxSeekTime(end)?;
        timeline.SetPosition(position)?;
        self.smtc.UpdateTimelineProperties(&timeline)?;
        Ok(())
    }
}

/// 100-nanosecond units — the unit every WinRT `TimeSpan` counts in.
fn to_time_span(seconds: f64) -> i64 {
    (seconds * 10_000_000.0).round().max(0.0) as i64
}

fn seconds(span: TimeSpan) -> f64 {
    span.Duration as f64 / 10_000_000.0
}

/// The buttons this app answers. The rest (record, channel up/down) are never
/// enabled, and `None` here keeps a stray one from doing something surprising.
fn press_of(button: SystemMediaTransportControlsButton) -> Option<Press> {
    match button {
        SystemMediaTransportControlsButton::Play => Some(Press::Play),
        SystemMediaTransportControlsButton::Pause => Some(Press::Pause),
        SystemMediaTransportControlsButton::Next => Some(Press::Next),
        SystemMediaTransportControlsButton::Previous => Some(Press::Previous),
        SystemMediaTransportControlsButton::FastForward => Some(Press::SeekForward),
        SystemMediaTransportControlsButton::Rewind => Some(Press::SeekBackward),
        _ => None,
    }
}

/// Name this process as the app the shortcuts and the registry are registered
/// to — the process-side half of the media card's identity.
///
/// Windows 11's media flyout labels a media session by resolving its
/// `SourceAppUserModelId` to a registered shell app, so the session the shell
/// publishes ([`attach`]) has to carry an id something can resolve: with this
/// set, the card reads "la musica". A process with no explicit AUMID answers
/// with its own image name instead — which is exactly what WebView2 does for
/// the session it publishes, and the reason this module exists.
///
/// Called first thing in `main.rs`, before the runtime creates any window: the
/// id is a property of the process. Raw FFI on purpose: the alternative is a
/// new dependency for one call, and `shell32` is linked by every Windows
/// target.
pub fn name_the_process(app_id: &str) {
    #[link(name = "shell32")]
    extern "system" {
        fn SetCurrentProcessExplicitAppUserModelID(app_id: *const u16) -> i32;
    }

    let wide: Vec<u16> = app_id.encode_utf16().chain(std::iter::once(0)).collect();
    // Failure is not fatal: the app runs, the flyout just keeps its generic
    // name, which is exactly the state this call exists to leave behind.
    unsafe {
        SetCurrentProcessExplicitAppUserModelID(wide.as_ptr());
    }
}

/// Name the app in the per-user AUMID registry, so Windows can resolve the
/// media card's id to "la musica" even when nothing installed it.
///
/// A packaged install already resolves: its Start Menu shortcut carries
/// `PKEY_AppUserModel_ID = com.musiclibraryoptimizer.lamusica`, and Windows
/// answers `shell:AppsFolder\com.musiclibraryoptimizer.lamusica` with "la
/// musica" (checked on this machine). A dev build has no shortcut at all, and
/// this key — `HKCU\Software\Classes\AppUserModelId\<id>`, the documented place
/// for an unpackaged app's shell identity — is what keeps its card from
/// reading "Unknown app" again for a different reason.
///
/// Idempotent, per-user, and its failure is only logged: without it the card
/// falls back to whatever Windows makes of the AUMID.
pub fn register_shell_name(app_id: &str, display_name: &str) {
    let subkey = wide(&format!("Software\\Classes\\AppUserModelId\\{app_id}"));
    let name = wide("DisplayName");
    let value = wide(display_name);
    let mut key: isize = 0;
    let mut disposition = 0u32;
    let created = unsafe {
        RegCreateKeyExW(
            HKEY_CURRENT_USER,
            subkey.as_ptr(),
            0,
            std::ptr::null(),
            0,
            KEY_WRITE,
            std::ptr::null(),
            &mut key,
            &mut disposition,
        )
    };
    if created != 0 {
        eprintln!("[mlo-desktop] AUMID registration failed: {created}");
        return;
    }
    unsafe {
        RegSetValueExW(
            key,
            name.as_ptr(),
            0,
            REG_SZ,
            value.as_ptr() as *const u8,
            (value.len() * 2) as u32,
        );
        // The icon the shell draws for the card when it does not have the
        // shortcut's own icon to fall back on.
        if let Ok(exe) = std::env::current_exe() {
            let icon_name = wide("IconUri");
            let icon = wide(&exe.to_string_lossy());
            RegSetValueExW(
                key,
                icon_name.as_ptr(),
                0,
                REG_SZ,
                icon.as_ptr() as *const u8,
                (icon.len() * 2) as u32,
            );
        }
        RegCloseKey(key);
    }
}

const HKEY_CURRENT_USER: isize = 0x8000_0001u32 as i32 as isize;
const KEY_WRITE: u32 = 0x20006;
const REG_SZ: u32 = 1;

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

#[link(name = "advapi32")]
extern "system" {
    fn RegCreateKeyExW(
        hkey: isize,
        subkey: *const u16,
        reserved: u32,
        class: *const u16,
        options: u32,
        sam: u32,
        security: *const c_void,
        key: *mut isize,
        disposition: *mut u32,
    ) -> i32;
    fn RegSetValueExW(
        key: isize,
        name: *const u16,
        reserved: u32,
        kind: u32,
        data: *const u8,
        bytes: u32,
    ) -> i32;
    fn RegCloseKey(key: isize) -> i32;
}