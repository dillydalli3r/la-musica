/** Every user-visible string the app ships, in English — the SOURCE OF TRUTH.
 *
 *  `keyof typeof en` is the parameter type of `t()` (web/src/lib/i18n.ts), so a
 *  missing or mistyped key is a compile error rather than a silent fallback.
 *  The other bundles must carry exactly this key set: tools/test_i18n.cjs
 *  fails, in both directions, when one drifts — a key added here and forgotten
 *  there, or a key nobody asks for any more.
 *
 *  Placeholders are `{name}` and must be spelled identically in every locale.
 *  Section comments below mark the key groups; the object itself stays flat so
 *  the checker (and any reader) can compare bundles line by line. */
export default {
  // —— Sidebar: the group headings and every entry under them ————————————
  "nav.group.library": "Library",
  "nav.group.acquire": "Acquire",
  "nav.group.maintain": "Maintain",
  "nav.library": "Library",
  "nav.browse": "Browse",
  "nav.genres": "Genres",
  "nav.trash": "Trash",
  "nav.import": "Import",
  "nav.musicbrainz": "MusicBrainz",
  "nav.export": "Export",
  "nav.optimize": "Optimization",
  "nav.inProgress": "In progress",
  "nav.checks": "Checks & scripts",
  "nav.grading": "Grading",
  "nav.dependencies": "Dependencies",
  "nav.equalizer": "Equalizer",
  "nav.settings": "Settings",

  // —— App shell: the top bar, the sidebar rail, global loading ——————————
  "topbar.search": "Search your library…",
  "topbar.back": "Back",
  "topbar.forward": "Forward",
  "topbar.shortcuts": "Keyboard shortcuts",
  "topbar.menu": "Menu",
  "topbar.menu_open": "Open navigation menu",
  "topbar.menu_close": "Close navigation menu",
  "topbar.search_source": "Search source",
  "topbar.search_local": "Local",
  "topbar.search_local_hint": "Library search — filters what is already in your library",
  "topbar.search_mb": "MusicBrainz",
  "topbar.search_mb_placeholder": "Search MusicBrainz…",
  "topbar.search_mb_hint": "MusicBrainz search — results open on musicbrainz.org",
  "loading.page": "Loading page…",
  "toast.dismiss": "Dismiss notification",
  "sidebar.collapse": "Collapse sidebar",
  "sidebar.expand": "Expand sidebar",

  // —— Common actions ————————————————————————————————————————————————
  "action.save": "Save",
  "action.cancel": "Cancel",
  "action.retry": "Retry",
  // —— The details ("…") menu: the generated script entries ————————————
  "menu.scripts": "Scripts",
  "menu.forced": "Forced re-run",
  "menu.forceEntry": "Force: {script}",
  "menu.runAll": "Run all {count} scripts",
  "menu.runAllTitle": "Run all {count} scripts?",
  "menu.runAllHint": "These run over the current selection, in this order:",
  "menu.openTrackPage": "Open track page",
  "menu.openTrackPageHint": "Open this track's own page — its tags, technical readout and lyrics",



  // —— Account (who the app is signed in AS) ——————————————————————————————

  "account.header": "Account",
  "account.title": "Signed in as {user}",
  "account.server_account": "the server's own account",
  "account.choose": "Switch to another user",
  "account.switch": "Switch",
  "account.switching": "Switching…",
  "account.failed": "Could not switch: {error}",
  "account.no_users": "No users yet — everything here belongs to the server's own account. Create users in Settings → Security.",
  "account.retry": "Retry",
  "account.loading": "Reading the user list…",

  // —— Sign-in screen (the server's auth gate) ——————————————————————————
  "auth.sign_in": "Sign in",
  "auth.username": "Username",
  "auth.password": "Password",
  "auth.sign_out": "Sign out",


  // The tray itself: the panel behind the bell.
  "notify.tray_title": "Notifications",
  "notify.tray_empty": "Nothing yet — outcomes land here.",
  "notify.clear_all": "Clear all",
  "notify.dismiss": "Dismiss",
  "notify.unread": "{count} unread",
  "notify.event": "Notification",
  "auth.server_address": "Server address",
  "auth.server_address_help": "Where the la musica server runs — a LAN address, a Tailscale name, or the address that served this page.",
  "auth.use_address": "Use",
  "auth.setup_intro": "Set a password to claim this server",
  "auth.setup_hint": "Nobody has claimed this server yet. Choose a password — every client will sign in with it.",
  "auth.repeat_password": "Repeat password",
  "auth.name_optional": "Name (optional)",
  "auth.name_placeholder": "who this login is for",
  "auth.set_password_sign_in": "Set password and sign in",
  "auth.no_answer": "No answer from that address — check it, and that the backend is running.",
  "auth.gate_help": "The password is stored as a PBKDF2 hash in your config, and sessions live in .mlo/data/auth.db. Only this machine's own address skips the gate — a server reachable from the network always asks.",
  "auth.change_password": "Change password",
  "auth.current_password": "Current password",
  "auth.new_password": "New password",
  "auth.revoke_all": "Sign out everywhere",
  "auth.revoked": "Signed out of every device",
  "auth.changed": "Password changed",
  "auth.setup_done": "Password set — welcome",
  "auth.signed_in": "Signed in",
  "auth.server": "This server",
  "settings.security": "Security",
  "settings.connection": "Server connection",
  "settings.server_version": "Server version {version}",
  "settings.image_build": "Docker image {revision}, built {built}",
  "settings.image_build_no_date": "Docker image {revision}",
  "settings.updater_auto": "A Docker install updates itself: the bundled updater (watchtower) replaces this image within 5 minutes of a release — `docker compose run --rm watchtower --run-once` checks right now.",
  "settings.update_available": "la musica {latest} is available — this server runs {version}.",
  "settings.update_link": "Release notes",
  "settings.update_dismiss": "Dismiss the update notice",
  "update.tray_title": "la musica {version} is available",
  "update.tray_body": "You are on {current}. Settings → Security installs it.",
  "update.toast": "la musica {version} is available — Settings → Security installs it.",
  "update.checking": "Checking for a newer release…",
  "update.up_to_date": "la musica {current} is the newest release",
  "update.check_now": "Check now",
  "update.check_failed": "Could not check for updates: {error}",
  "update.available": "la musica {version} is available — you are on {current}.",
  "update.install": "Update now",
  "update.downloading": "Downloading la musica {version}…",
  "update.installing": "Installing la musica {version} — the app restarts by itself.",
  "update.restart_hint": "la musica restarts by itself when the update lands.",
  "settings.users": "Users",
  "settings.users_help": "Everyone who can sign in to this server. Each user's trash is their own — an album a user removes goes there.",
  "settings.users_you": "you",
  "settings.users_empty": "No users yet — the password above is the only gate.",
  "settings.users_error": "Could not read the user list — the server did not answer.",
  "settings.user_add": "Add user",
  "settings.user_added": "Added {user}.",
  "settings.user_removed": "Removed {user}.",
  "settings.user_password_help": "At least 8 characters.",
  "settings.user_remove": "Remove user",
  "settings.user_remove_self": "You cannot remove the user you are signed in as",
  "settings.notifications": "Notifications",
  "settings.notifications_help": "In-app notifications for imports that need a decision and finished imports — each lands in the bell tray.",


  "page.artist": "Artist",
  "page.album": "Album",
  "page.track": "Track",
  "page.not_found": "Page not found",


  // —— Storage card: the three figures it shows ——————————————————————
  // The label of the row that adds the Library and App rows up, so it names
  // both sides of the sum it shows.
  "storage.total": "Total (App + Library)",

  "charts.kind.tracks": "Tracks",
  "charts.kind.albums": "Albums",

  // —— Browse page: what an empty or failed result says ——————————————————
  "browse.not_asked": "no query run yet",
  "browse.error_title": "The query could not be read",
  "browse.zero_title": "Nothing matches this query",
  "browse.zero_hint": "The engine was asked for {target} matching {n} condition(s) and returned none. Clear the builder to see the whole library, or loosen a row.",
  "browse.empty_title": "The library is empty",
  "browse.empty_hint": "No {target} in the library match this target yet — import some music, or pick the other target.",
  "browse.clear": "Clear the conditions",

  // —— Credits footer and dialog ——————————————————————————————————————
  "credits.title": "Credits",
  "credits.subtitle": "la musica is MIT-licensed and built on these projects, services and data sets.",
  "credits.from": "Credits — data & services from:",
  "credits.repo": "la musica on GitHub",
  "credits.project": "Project",
  "credits.source": "Source code",
  "credits.issues": "Report an issue",
  "credits.more": "Credits · {n} more",
  "credits.open": "Credits, licences and the projects this app is built on",
  "credits.legal": "Full licence texts live in THIRD-PARTY-NOTICES.md in the repository.",

  // —— Settings ———————————————————————————————————————————————————————
  "settings.language": "Language",
  "settings.language_help": "Applies to this browser — the app's own labels, never your file names or tags.",
  // The alias fallback search (mlo/config.py's `lyrics_search_aliases`) is the
  // one Lyrics-tab row whose label and help are translated: it is about the
  // NAMES a track can be looked up under, so a reader whose music is filed
  // under a non-Latin script reads its sentence in their own language.
  "settings.lyrics_search_aliases": "Search lyrics under alias names",
  "settings.lyrics_search_aliases_help": "When the first search for a track's lyrics finds nothing, search again under the artist/album/title's other names from MusicBrainz (aliases) — e.g. Hikaru Utada for 宇多田ヒカル. On by default.",


  // —— Client setup wizard (desktop / iOS / Android shells) ————————————
  "client.title": "Set up this device",
  "client.subtitle": "Point this app at your server and sign in. Nothing here is final — the Settings page runs this wizard again.",
  "client.step_server": "Server",
  "client.step_account": "Account",
  "client.step_done": "Done",
  "client.test": "Test connection",
  "client.test_ok": "Connected — la musica {version}",
  "client.test_fail": "No la musica server at that address: {error}",
  "client.timeout": "no answer within 3s",
  "client.bad_reply": "that address answered, but not like a la musica server",
  "client.need_test": "Test the connection before continuing.",
  "client.back": "Back",
  "client.next": "Next",
  "client.skip": "Skip",
  "client.account_setup": "Nobody has claimed this server yet — choose the password every client will sign in with.",
  "client.account_signin": "Sign in to {server}",
  "client.finish": "Finish",
  "client.done_note": "You can run this wizard again from the Settings page.",
  "client.rerun": "Run setup again",

  // —— Backend choice (a desktop shell's very first run) ——————————————————
  "backend.title": "How should this app get its server?",
  "backend.subtitle": "Choose once. You can change it later from the tray icon.",
  "backend.local_title": "Use the built-in backend",
  "backend.local_hint": "la musica runs its own server on this machine. Recommended — no Docker, nothing else to install.",
  "backend.remote_title": "Connect to a server",
  "backend.remote_hint": "Point the app at a la musica server you run yourself — in Docker, on a laptop, or on a home server.",
  "backend.recommended": "Recommended",
  "backend.starting_title": "Starting the built-in server…",
  "backend.starting_hint": "This can take a few seconds. This window opens the app by itself once the server answers.",
  "backend.error": "That choice did not take effect. Try again.",
"backend.failed_title": "The built-in server did not start",
"backend.failed_hint": "This install bundles its own server, and it did not come up — most often the packaged files are missing or the port range is taken. You can retry, or connect to a la musica server you run elsewhere.",
"backend.failed_retry": "Try again",
"backend.failed_remote": "Connect to a server instead",

  // —— MusicBrainz artist page: add / download one release TYPE ——————————
  "mb.actions_title": "Add or download by release type",
  "mb.actions_groups": "{n} release group(s)",
  "mb.add_to_library": "Add to library",
  "mb.actions_working": "Working…",
  "mb.actions_add_hint": "Add one album per release group of this type. Each one is created in your library as it is added.",
  "mb.actions_queued": "Queued {n}",
  "mb.actions_skipped": "{n} skipped",
  "mb.actions_failed": "{n} failed",
  "mb.actions_nothing": "Nothing was queued.",
  // —— Cover finder: the one cover policy's pick, its reasons and its sources ——
  "cover.best_pick": "Best pick",
  "cover.best_pick_use": "Use the best pick",
  "cover.no_pick": "No candidate reaches the cover target — pick one by hand.",
  "cover.pick_reason": "Why this one",
  "cover.checked_against": "Checked against {identity}",
  "cover.all_rejected": "{count} candidate(s) were rejected — each row says why, and any of them can still be applied by hand.",
  "cover.source_notes": "What each source did",
  "cover.searching": "Searching cover sources…",
  "cover.answered": "Searched {terms}",
  "cover.empty": "No covers found.",
  "cover.empty_hint": "How to widen it: add a source or another region above, or correct the artist and album, then search again.",
  "cover.failed": "The cover search failed",
  "cover.retry": "Retry",
  "cover.blocked": "Nothing to search with",
  "cover.blocked_missing": "This album is missing:",
  "cover.blocked_hint": "Type the artist and album above and search, or tag the files.",
  "cover.term.artist": "artist “{value}”",
  "cover.term.album": "album “{value}”",
  "cover.term.release": "release {value}",
  "cover.term.group": "release group {value}",
  "cover.term.sources": "{value} sources",
  "cover.term.region": "region {value}",
  "cover.missing.names": "artist and album tags",
  "cover.missing.release": "a MusicBrainz release id",
  // —— A framework album: added, not downloaded yet ————————————————————————
  "pending.title": "Waiting for its audio",
  "pending.note": "The folder exists and its page is already prepared — the links and the ranked cover candidates are fetched. The audio has not landed yet.",
  "pending.no_search": "nothing is searching for it right now",
  "pending.short": "not downloaded yet",

  // —— Lyrics kind: synced (timed) or plain ————————————————————————————
  // The two kinds a track's stored lyrics can be, and the reason a plain one
  // is a FAILING state when the user's own `lyrics_allow_plain` is off (the
  // shipped default) — the reason names that setting, because the policy is
  // the user's, not a rule about the words.
  "lyrics.kind.synced": "Synced",
  "lyrics.kind.plain": "Plain",
  "lyrics.kind.synced_hint": "Timed lyrics — every line carries its timestamp",
  "lyrics.kind.plain_hint": "Untimed lyrics — no timestamps, so nothing follows the words",
  "lyrics.kind.plain_reason": "Plain lyrics — Settings → Lyrics has “Accept plain (unsynced) lyrics” off, so this track should hold a synced version.",
  "lyrics.kind.counts": "{synced} synced · {plain} plain",
  "lyrics.kind.plain_failing": "{n} track(s) hold plain lyrics — Settings → Lyrics has “Accept plain (unsynced) lyrics” off, so those tracks fail this check.",
  // —— Import: the digital release's own answers ——————————————————————
  // SOURCE is required on a Digital Media release and nothing in the
  // audio states it; the settle line names what an import decided about
  // the lyrics and the album description when it finished an album.
  "import.source.title": "Source",
  "import.source.why": "Required when the release is Digital Media — nothing in the files says where it came from.",
  "import.source.suggested": "Suggested from the release: {value}",
  "import.source.save": "Save source",
  "import.source.save_hint": "Write this value to every track of the album that has no SOURCE yet — required when MEDIA is Digital Media",
  "import.source.written": "SOURCE written to {n} file(s)",
  "import.source.present": "SOURCE already on every file",
  "import.source.not_digital": "Not a Digital Media release — no SOURCE is required",
  "import.source.asked": "Nothing states a source — say where this release came from",
  "import.settle.title": "Settled by this import",
  "import.settle.lyrics": "Lyrics",
  "import.settle.lyrics_removed": "{n} file(s) had untimed lyrics removed (synced lyrics are required here)",
  "import.settle.lyrics_unformatted": "{n} file(s) had lyrics removed: they are timed but not in the form this install's grading check asks for, and the Lyrics script cannot make them so",
  "import.settle.lyrics_no_fetch": "Left alone: the fetch (script 13) is not in this run's chain",
  "import.settle.lyrics_allow_plain": "Untimed lyrics are allowed here — kept",
  "import.settle.lyrics_ok": "Every lyric is timed",
  // —— Cookie logins: a cookies.txt import, and a note per cookie ————————————
  "cookies.commentPlaceholder": "comment",
  "cookies.commentFor": "Comment for the {name} cookie",
  "cookies.session": "session",
  "cookies.expired": "expired",
  "cookies.keptHosts": "Only cookies for {hosts} are kept — everything else in the export is left out.",

  "cookies.sectionTitle": "Cookie logins — import a cookies.txt",

  // —— Library page: the toolbar's alphabet pair — a name field and the A–Z
  // rail. Two controls over one filter (the typed words and the picked
  // letter), applied to the list the view tabs draw.
  "library.az.namePlaceholder": "Filter by name",
  "library.az.nameAria": "Filter the list by name",
  "library.az.nameHint": "Album title, artist name or track title — accents are ignored, so “asgeir” finds “Ásgeir”. This narrows the list the view tabs are already drawing.",
  "library.az.nameClear": "Clear the name filter",
  "library.az.button": "A–Z",
  "library.az.railAria": "Filter the list by letter",
  "library.az.railTitle": "Jump to a letter — each one shows how many items in this view start with it",
  "library.az.menuTitle": "Starts with",
  "library.az.clear": "Show all",
  "library.az.letterHint": "{letter} — {n} in this view",
  "library.az.empty": "Nothing here matches — clear the search box, the name field, the A–Z letter or the Filter menu.",


} as const;
