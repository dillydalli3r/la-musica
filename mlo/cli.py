"""Interactive terminal front end: start it with ``python -m mlo``.

The menus, the Run All / Run Custom sequences and the configuration editor all
live here; mlo/ui.py draws, mlo/report.py summarises, mlo/stats.py accounts.
The scripts are the ones server/script_runners.py exposes over /api/run, so
SCRIPTS below must keep the same ids and names.

Every script runs isolated: one that fails (or whose module cannot be imported)
is reported as an error and the rest of the sequence still runs. Ctrl+C aborts
the session cleanly.
"""
import importlib
import os
import traceback

from .audit import run_audit_library
from .autotag import run_auto_tagging
from .cue import run_format_cues
from .deps import HAS_MUTAGEN
from .paths import DEFAULT_DIGITAL_SOURCE
from .flac import run_optimize_flacs
from .grader import run_grade_library
from .images import run_process_images
from .loudness import run_calc_dr_replaygain
from .lyrics import run_format_lyrics
from .report import print_results, print_grade_results, print_combined_results
from .tools import detect_all_tools
from .ui import (
    c, Color, clear_screen, print_header, print_separator,
    pause_for_input, log, fmt_size,
)
from .config import load_config, save_config, DEFAULT_CONFIG, DEFAULT_RUN_ALL_ORDER

# Enable ANSI escape sequences on Windows 10+ consoles.
if os.name == "nt":
    os.system("")

# The one list every menu is built from: id -> (name, what the script does).
# Names must equal server/script_runners.py RUNNERS (and web/src/lib/scripts.ts)
# so the terminal can never claim a number means something the API does not.
SCRIPTS = (
    (1, "Format lyrics", "multi-format + MEDIA/SOURCE normalization"),
    (2, "Format CUEs", "CD-N rename + FILE/INDEX layout"),
    (3, "Optimize FLACs", "lossless re-encode"),
    (4, "Grade", "per-album tag/lyrics/cover report"),
    (5, "Process images", "JXL / lossless / JXL-back"),
    (6, "Audit library", "AudioAuditor: fake lossless / upscaled / MQA"),
    (7, "DR & ReplayGain", "rsgain + simple-dr-meter tags"),
    (8, "Auto tagging", "advisory / instrumental / mood / energy / genre"),
    (9, "AccurateRip", "CUETools .accurip files"),
    (10, "Format all", "final pass: .accurip / .cue / .lrc / tags"),
    (11, "Remux videos (MKV)", "any video -> MKV, audio -> FLAC"),
    (12, "Key & BPM", "musical key + tempo tags"),
    (13, "Fetch lyrics", "LRCLIB synced/plain"),
    (14, "Beets tagging", "MusicBrainz via beets"),
    (15, "Release tracklist", ".mlo_expected.json manifests"),
    (16, "Mood & Energy", "MOOD/ENERGY from the track's audio"),
    (17, "Lyrics transliterate (AI)", "TRANSLITERATION/TRANSLATION tags + sidecars"),
    (18, "Publish lyrics (LRCLIB)", "submit missing lyrics to the community DB"),
)
SCRIPT_LABELS = {sid: name for sid, name, _ in SCRIPTS}

# Scripts whose feature has its own on/off switch (mirror of the server's
# _DISABLED): with the switch off the runner is a no-op at best, so the CLI
# skips the script instead of reporting an empty run.
SCRIPT_GATES = {7: "dr_replaygain_enabled", 12: "audiometa_enabled",
                16: "mood_enabled", 17: ("lyrics_xlit_enabled", "lyrics_translate_enabled"),
                18: "lrclib_auto_publish"}


def _print_script_list(with_desc=True):
    """'Available scripts' block shared by every menu; numbers stay aligned."""
    name_w = max(len(name) for name in SCRIPT_LABELS.values())
    for sid, name, desc in SCRIPTS:
        tail = f"  ({desc})" if with_desc else ""
        print(f"  {sid:>2}. {name:<{name_w}}{tail}")


def _gate_reason(config, script_id):
    """Why *script_id* is skipped, or '' when it can run."""
    gate = SCRIPT_GATES.get(script_id)
    if not gate:
        return ""
    keys = gate if isinstance(gate, tuple) else (gate,)
    if not any(config.get(k, True) for k in keys):
        joined = " and ".join(keys)
        return (f"{SCRIPT_LABELS[script_id]}: {joined} "
                f"{'are' if len(keys) > 1 else 'is'} off (see Configuration)")
    return ""


def _run_isolated(name, runner, config):
    """Run one script; a failure becomes an error result, never a crash.

    Same contract as the server's run_script: the caller keeps going and the
    failure shows up in the per-script results with a non-zero error count.
    """
    try:
        return runner(config)
    except Exception as e:
        traceback.print_exc()
        log(c(f"ERROR: {name} failed: {e}", Color.RED))
        return {"error_count": 1, "errors": [(name, str(e))]}


def edit_run_all_order(config):
    while True:
        clear_screen()
        print_header("EDIT RUN ALL ORDER")

        print("  Available scripts:")
        _print_script_list(with_desc=False)
        print_separator()

        current = config.get("run_all_order", DEFAULT_RUN_ALL_ORDER)
        print(f"  Current order: {c(','.join(map(str, current)), Color.CYAN)}")
        print("  Scripts you leave out are not dropped: they keep their default")
        print("  order and run after the ones you list.")
        print("Enter new order (comma-separated, e.g. 3,1,2,5,4) or '0' to cancel:")

        choice = input(c("> ", Color.CYAN)).strip()

        if choice == "0":
            return

        parts = [p.strip() for p in choice.split(",") if p.strip()]
        order = []
        valid = True

        for p in parts:
            if p.isdigit() and int(p) in SCRIPT_LABELS:
                pid = int(p)
                if pid not in order:
                    order.append(pid)
            else:
                print(c(f"  Invalid entry: '{p}'", Color.RED))
                valid = False

        if valid and order:
            # Run All must cover every script: ids that were left out keep their
            # default order behind the listed ones.
            order += [sid for sid in DEFAULT_RUN_ALL_ORDER if sid not in order]
            config["run_all_order"] = order
            save_config(config)
            # save_config normalizes on write (legacy orders are migrated and
            # missing scripts anchored by position), so keep the stored order —
            # the menu and Run All must show and execute what the next session
            # loads.
            config["run_all_order"] = load_config()["run_all_order"]
            print(c(f"\nSaved. Effective order: {config['run_all_order']}", Color.GREEN))
            pause_for_input()
            return
        elif not valid:
            pause_for_input()


def show_config_menu(config):
    def tf(prompt):
        v = input(prompt).strip().lower()
        return v in ("y", "yes", "true", "1")

    while True:
        clear_screen()
        print_header("CONFIGURATION")

        tools = detect_all_tools()

        fv = tools.get("flac", {}).get("version", "(none)")
        jv = tools.get("libjxl", {}).get("version", "(none)")
        lv = tools.get("libjpeg_turbo", {}).get("version", "(none)")
        ov = tools.get("oxipng", {}).get("version", "(none)")

        print(f"  1. Music Folder             : {config['music_folder']}")
        print(f"  2. FLAC Level               : -{config['flac_level']} (0-8)")
        print(f"  3. Add SeekTables           : {config['add_seektables']}")
        print(f"  4. Force Re-encode FLACs    : {config.get('force_reencode_flac', False)}")
        print(f"  5. JPEG XL Effort           : {config['jpegxl_effort']} (1-10)")
        print(f"  6. Re-encode Images         : {config.get('reencode_images', True)}")
        print(f"  7. Re-encode to JXL         : {config.get('reencode_to_jxl', DEFAULT_CONFIG['reencode_to_jxl'])}")
        print(f"  8. Convert JXL Back         : {config.get('convert_jxl_back', DEFAULT_CONFIG['convert_jxl_back'])}")
        print(f"  9. Rename to Cover          : {config.get('rename_to_cover', True)}")
        print(f" 10. Remove Alpha             : {config.get('remove_alpha', True)}")
        print(f" 11. Force Re-encode Images   : {config.get('force_reencode_images', False)}")
        print(f" 12. Optimize LRC             : {config.get('optimize_lrc', True)}")
        print(f" 13. Optimize Embedded Lyrics : {config.get('optimize_embedded_lyrics', True)}")
        print(f" 14. Lyrics Format            : {config.get('lyrics_format', 'EMBEDDED').upper()}")
        print(f" 15. Auto-Advance             : {config.get('auto_advance', True)}")
        print(f" 16. Keep Empty CUE Lines     : {config.get('keep_empty_cue_lines', False)}")
        print(f" 17. Keep Other CUE Lines     : {config.get('keep_other_cue_lines', False)}")
        print(f" 18. Normalize MEDIA/SOURCE   : {config.get('normalize_media_source', True)}")
        print(
            " 19. Digital SOURCE Value     : "
            f"{config.get('digital_media_source_value', DEFAULT_DIGITAL_SOURCE)}"
        )
        print(f" 20. Grade Verbose            : {config.get('grade_verbose', True)}")
        print(f" 21. Edit Run All Order       : {config.get('run_all_order', DEFAULT_RUN_ALL_ORDER)}")
        print(f" 22. Thorough Audit           : {config.get('audit_thorough', DEFAULT_CONFIG['audit_thorough'])}")
        print(f" 23. Force Audit              : {config.get('force_audit', False)}")
        print(f" 24. Audit Cutoff Allowance   : {config.get('audit_cutoff_allow', 0)} Hz (0=default)")
        print(f" 25. Audit Clipping           : {config.get('audit_clipping', True)}")
        print(f" 26. Audit MQA                : {config.get('audit_mqa', True)}")
        print(f" 27. Detect Upscaled Audio    : {config.get('audit_ai', True)}")
        print(f" 28. Audit Fake Stereo        : {config.get('audit_fake_stereo', True)}")
        print(f" 29. Audit Silence            : {config.get('audit_silence', True)}")
        print(f" 30. Audit Dynamic Range      : {config.get('audit_dynamic_range', True)}")
        print(f" 31. Audit True Peak          : {config.get('audit_true_peak', True)}")
        print(f" 32. Audit LUFS               : {config.get('audit_lufs', True)}")
        print(f" 33. Audit BPM                : {config.get('audit_bpm', True)}")
        print(f" 34. DR/ReplayGain Enabled    : {config.get('dr_replaygain_enabled', True)}")
        print(f" 35. ReplayGain Skip Existing : {config.get('replaygain_skip_existing', True)}")
        print(f" 36. Auto Album Advisory      : {config.get('auto_advisory', True)}")
        print(f" 37. Auto Instrumental Tag    : {config.get('auto_instrumental', True)}")
        print(f" 38. Force Auto Tagging       : {config.get('force_auto_tag', False)}")
        print(f" 39. Key & BPM Enabled        : {config.get('audiometa_enabled', True)}")

        print_separator()
        print("  Auto-detected encoder versions (.dependencies):")
        print(f"      flac           v{fv}")
        print(f"      libjxl         v{jv}")
        print(f"      libjpeg-turbo  v{lv}")
        print(f"      oxipng         v{ov}")
        print_separator()

        print("  Encoder marker tags:")
        print("      FLAC: ENCODER_PROGRAM / ENCODER_QUALITY / ENCODER_VERSION")
        print("      JPEG: XMP enc:ENCODER_PROGRAM / QUALITY / VERSION")
        print("      PNG : tEXt ENCODER_PROGRAM / QUALITY / VERSION")
        print("      JXL : XMP enc:ENCODER_PROGRAM / QUALITY / VERSION")

        print()
        print("  Digital SOURCE Value explanation:")
        print("      If MEDIA is Digital Media and SOURCE is missing, this value")
        print("      is written to SOURCE. Existing SOURCE values are preserved.")

        print(c("  0. Back to Main Menu", Color.YELLOW))
        print_separator()

        choice = input(c("Select option: ", Color.CYAN)).strip()

        if choice == "1":
            new_val = input("Enter music folder path: ").strip()
            if new_val:
                config["music_folder"] = new_val
                save_config(config)
                print(c("\nSaved.", Color.GREEN))
            pause_for_input()

        elif choice == "2":
            try:
                new_val = int(input("Enter FLAC level (0-8): ").strip())
                if 0 <= new_val <= 8:
                    config["flac_level"] = new_val
                    save_config(config)
                    print(c("\nSaved.", Color.GREEN))
                    pause_for_input()
                else:
                    print(c("\nValue must be between 0 and 8.", Color.RED))
                    pause_for_input()
            except ValueError:
                print(c("\nInvalid value.", Color.RED))
                pause_for_input()

        elif choice == "3":
            config["add_seektables"] = tf("Add seektables to FLAC files? (y/n): ")
            save_config(config)
            print(f"\nSaved. Add SeekTables = {config['add_seektables']}")
            if not config["add_seektables"]:
                print(c("  -> SeekTables will be actively REMOVED from FLACs.", Color.YELLOW))
            pause_for_input()

        elif choice == "4":
            config["force_reencode_flac"] = tf("Force re-encode of FLAC files? (y/n): ")
            save_config(config)
            print(f"\nSaved. Force Re-encode FLACs = {config['force_reencode_flac']}")
            pause_for_input()

        elif choice == "5":
            try:
                new_val = int(input("Enter JPEG XL effort (1-10): ").strip())
                if 1 <= new_val <= 10:
                    config["jpegxl_effort"] = new_val
                    save_config(config)
                    print(c("\nSaved.", Color.GREEN))
                    pause_for_input()
                else:
                    print(c("\nValue must be between 1 and 10.", Color.RED))
                    pause_for_input()
            except ValueError:
                print(c("\nInvalid value.", Color.RED))
                pause_for_input()

        elif choice == "6":
            config["reencode_images"] = tf("Re-encode images at all? (y/n): ")
            save_config(config)
            print(f"\nSaved. Re-encode Images = {config['reencode_images']}")
            pause_for_input()

        elif choice == "7":
            config["reencode_to_jxl"] = tf("Re-encode images to JPEG XL? (y/n): ")
            save_config(config)
            print(f"\nSaved. Re-encode to JXL = {config['reencode_to_jxl']}")
            pause_for_input()

        elif choice == "8":
            config["convert_jxl_back"] = tf("Convert JXL files back to JPEG/PNG? (y/n): ")
            save_config(config)
            print(f"\nSaved. Convert JXL Back = {config['convert_jxl_back']}")
            pause_for_input()

        elif choice == "9":
            config["rename_to_cover"] = tf("Rename all images to cover.<ext>? (y/n): ")
            save_config(config)
            print(f"\nSaved. Rename to Cover = {config['rename_to_cover']}")
            pause_for_input()

        elif choice == "10":
            config["remove_alpha"] = tf("Remove alpha transparency from PNGs? (y/n): ")
            save_config(config)
            print(f"\nSaved. Remove Alpha = {config['remove_alpha']}")
            pause_for_input()

        elif choice == "11":
            config["force_reencode_images"] = tf("Force re-encode of images? (y/n): ")
            save_config(config)
            print(f"\nSaved. Force Re-encode Images = {config['force_reencode_images']}")
            pause_for_input()

        elif choice == "12":
            config["optimize_lrc"] = tf("Optimize .lrc files? (y/n): ")
            save_config(config)
            print(f"\nSaved. Optimize LRC = {config['optimize_lrc']}")
            pause_for_input()

        elif choice == "13":
            config["optimize_embedded_lyrics"] = tf("Optimize embedded LYRICS tags? (y/n): ")
            save_config(config)
            print(f"\nSaved. Optimize Embedded Lyrics = {config['optimize_embedded_lyrics']}")
            pause_for_input()

        elif choice == "14":
            v = input("Lyrics format (EMBEDDED, LRC or BOTH): ").strip().upper()
            if v in ("EMBEDDED", "LRC", "BOTH"):
                config["lyrics_format"] = v
                save_config(config)
                print(f"\nSaved. Lyrics Format = {config['lyrics_format']}")
            else:
                print(c("\nInvalid choice. Must be EMBEDDED, LRC or BOTH.", Color.RED))
            pause_for_input()

        elif choice == "15":
            config["auto_advance"] = tf("Auto-advance between scripts? (y/n): ")
            save_config(config)
            print(f"\nSaved. Auto-Advance = {config['auto_advance']}")
            pause_for_input()

        elif choice == "16":
            config["keep_empty_cue_lines"] = tf("Keep empty lines in .cue files? (y/n): ")
            save_config(config)
            print(f"\nSaved. Keep Empty CUE Lines = {config['keep_empty_cue_lines']}")
            pause_for_input()

        elif choice == "17":
            config["keep_other_cue_lines"] = tf("Keep non-standard lines in .cue files? (y/n): ")
            save_config(config)
            print(f"\nSaved. Keep Other CUE Lines = {config['keep_other_cue_lines']}")
            pause_for_input()

        elif choice == "18":
            config["normalize_media_source"] = tf("Normalize MEDIA/SOURCE tags? (y/n): ")
            save_config(config)
            print(f"\nSaved. Normalize MEDIA/SOURCE = {config['normalize_media_source']}")
            pause_for_input()

        elif choice == "19":
            new_val = input(
                "Default SOURCE value for Digital Media albums "
                f"(current: {config.get('digital_media_source_value', DEFAULT_DIGITAL_SOURCE)}): "
            ).strip()

            if new_val:
                config["digital_media_source_value"] = new_val
                save_config(config)
                print(c("\nSaved.", Color.GREEN))
            else:
                print(c("\nValue unchanged.", Color.YELLOW))

            pause_for_input()

        elif choice == "20":
            config["grade_verbose"] = tf("Verbose grading output? (y/n): ")
            save_config(config)
            print(f"\nSaved. Grade Verbose = {config['grade_verbose']}")
            pause_for_input()

        elif choice == "21":
            edit_run_all_order(config)

        elif choice == "22":
            config["audit_thorough"] = tf("Thorough audit (silence, DR, true peak, LUFS, BPM)? (y/n): ")
            save_config(config)
            print(f"\nSaved. Thorough Audit = {config['audit_thorough']}")
            pause_for_input()

        elif choice == "23":
            config["force_audit"] = tf("Force re-audit of files that already carry an AUDIT tag? (y/n): ")
            save_config(config)
            print(f"\nSaved. Force Audit = {config['force_audit']}")
            pause_for_input()

        elif choice == "24":
            try:
                new_val = int(input(
                    "Cutoff allowance in Hz (0 = pass no --cutoff-allow flag, "
                    "20000+ for HD masters): "
                ).strip())
                if 0 <= new_val <= 24000:
                    config["audit_cutoff_allow"] = new_val
                    save_config(config)
                    print(c("\nSaved.", Color.GREEN))
                else:
                    print(c("\nValue must be between 0 and 24000.", Color.RED))
            except ValueError:
                print(c("\nInvalid value.", Color.RED))
            pause_for_input()

        elif choice in ("25", "26", "27", "28", "29", "30", "31", "32", "33"):
            key = {
                "25": "audit_clipping",
                "26": "audit_mqa",
                "27": "audit_ai",
                "28": "audit_fake_stereo",
                "29": "audit_silence",
                "30": "audit_dynamic_range",
                "31": "audit_true_peak",
                "32": "audit_lufs",
                "33": "audit_bpm",
            }[choice]
            label = {
                "25": "clipping detection",
                "26": "MQA detection",
                "27": "upscaled-audio detection",
                "28": "fake stereo detection",
                "29": "silence detection",
                "30": "dynamic range",
                "31": "true peak",
                "32": "LUFS",
                "33": "BPM",
            }[choice]
            config[key] = tf(f"Enable audit {label}? (y/n): ")
            save_config(config)
            print(f"\nSaved. Audit {label} = {config[key]}")
            pause_for_input()

        elif choice == "34":
            config["dr_replaygain_enabled"] = tf("Enable DR & ReplayGain calculation? (y/n): ")
            save_config(config)
            print(f"\nSaved. DR/ReplayGain = {config['dr_replaygain_enabled']}")
            pause_for_input()

        elif choice == "35":
            config["replaygain_skip_existing"] = tf("Skip files that already have ReplayGain tags? (y/n): ")
            save_config(config)
            print(f"\nSaved. ReplayGain Skip Existing = {config['replaygain_skip_existing']}")
            pause_for_input()

        elif choice == "36":
            config["auto_advisory"] = tf("Auto-derive ALBUMITUNESADVISORY from track ITUNESADVISORY? (y/n): ")
            save_config(config)
            print(f"\nSaved. Auto Album Advisory = {config['auto_advisory']}")
            pause_for_input()

        elif choice == "37":
            config["auto_instrumental"] = tf("Auto-set INSTRUMENTAL from lyrics presence? (y/n): ")
            save_config(config)
            print(f"\nSaved. Auto Instrumental = {config['auto_instrumental']}")
            pause_for_input()

        elif choice == "38":
            config["force_auto_tag"] = tf("Force re-tagging even when tags are already correct? (y/n): ")
            save_config(config)
            print(f"\nSaved. Force Auto Tagging = {config['force_auto_tag']}")
            pause_for_input()

        elif choice == "39":
            config["audiometa_enabled"] = tf("Enable Key & BPM analysis (script 12)? (y/n): ")
            save_config(config)
            print(f"\nSaved. Key & BPM Enabled = {config['audiometa_enabled']}")
            if not config["audiometa_enabled"]:
                print(c("  -> Script 12 is skipped by Run All while this is off.", Color.YELLOW))
            pause_for_input()

        elif choice == "0":
            break

        else:
            print(c("\nInvalid option.", Color.RED))
            pause_for_input()


def show_custom_menu():
    clear_screen()
    print_header("CUSTOM RUN ORDER")

    print("  Available scripts:")
    _print_script_list(with_desc=False)
    print_separator()

    print("Enter the order of scripts to run (comma-separated, e.g. '3,1,2,5'):")
    choice = input(c("> ", Color.CYAN)).strip()

    parts = [p.strip() for p in choice.split(",") if p.strip()]
    order = []

    for p in parts:
        if p.isdigit() and int(p) in SCRIPT_LABELS:
            order.append(int(p))
        else:
            print(c(f"  Ignoring invalid entry: '{p}'", Color.YELLOW))

    return order


def _optional_runner(module, attr):
    """Resolve an optional script runner lazily.

    A module that cannot be imported must not look like a clean run: the
    returned callable raises, so the failure is reported as a per-script error
    (the server answers 400 "runner N not available" for the same case).
    """
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as e:
        def _unavailable(config, _module=module, _err=e):
            raise RuntimeError(f"{_module} unavailable: {_err}")

        return _unavailable


def build_script_runners():
    """Map script id -> (label, runner) for every pipeline script.

    Labels come from SCRIPTS, so the terminal and the API name every script the
    same way; scripts 9-14 resolve their module on first use.
    """
    runners = {
        1: run_format_lyrics,
        2: run_format_cues,
        3: run_optimize_flacs,
        4: run_grade_library,
        5: run_process_images,
        6: run_audit_library,
        7: run_calc_dr_replaygain,
        8: run_auto_tagging,
        9: _optional_runner("mlo.accurip", "run_generate_accurip"),
        10: _optional_runner("mlo.format_all", "run_format_all"),
        11: _optional_runner("mlo.remux", "run_remux_videos"),
        12: _optional_runner("mlo.audiometa", "run_analyze_audiometa"),
        13: _optional_runner("mlo.lyrics_fetch", "run_fetch_lyrics"),
        14: _optional_runner("server.beetscfg", "run_beets_tagging"),
        # 15 was missing from this map (the CLI could list it and not run it):
        # labels come from SCRIPTS, so every id the menu shows has a runner.
        15: _optional_runner("server.script_runners", "run_release_tracklist"),
        16: _optional_runner("mlo.moods", "run_detect_mood_energy"),
        17: _optional_runner("mlo.lyrics_xlit", "run_lyrics_xlit"),
        18: _optional_runner("mlo.lyrics_publish", "run_publish_lyrics"),
    }
    return {sid: (SCRIPT_LABELS[sid], runner) for sid, runner in runners.items()}


def run_scripts_sequence(config, script_ids, title):
    """Run *script_ids* in order, one script per step.

    Each script is isolated: a failure is reported for that script and the
    sequence continues. Ctrl+C still aborts the whole session.
    """
    runners = build_script_runners()

    auto_advance = config.get("auto_advance", True)

    per_script = []
    total_bytes_added = 0
    total_bytes_removed = 0
    total_errors = 0
    all_errors = []

    clear_screen()

    print(f">>> {c(title, Color.BOLD)}")
    print(f">>> Scripts: {' -> '.join(str(x) for x in script_ids)}")
    print(f">>> Auto-Advance: {auto_advance}")
    print()

    input("Press Enter to start...")

    for i, script_id in enumerate(script_ids):
        entry = runners.get(script_id)
        if entry is None:
            log(c(f"  Skipping unknown script id: {script_id}", Color.YELLOW))
            continue
        name, runner = entry

        reason = _gate_reason(config, script_id)
        if reason:
            log(c(f"  Skipping {reason}", Color.YELLOW))
            continue

        if i > 0:
            if auto_advance:
                # Clear first, then announce: the notice stays on screen while
                # the script starts instead of being wiped right after printing.
                clear_screen()
                log(f"\n--- Auto-advancing to: {c(name, Color.CYAN)} ---\n")
            else:
                pause_for_input()
                clear_screen()

        print(f">>> Starting: {c(name, Color.BOLD)}")
        print_separator()

        s = _run_isolated(name, runner, config)
        per_script.append((name, s))

        if not s.get("is_grader"):
            total_bytes_added += s.get("total_bytes_added", 0)
            total_bytes_removed += s.get("total_bytes_removed", 0)
            total_errors += s.get("error_count", 0)
            all_errors.extend(s.get("errors", []))

    print("\n")
    print_combined_results(per_script, title="COMBINED RESULTS - ALL SCRIPTS")

    log(f"Total Bytes Added   : {fmt_size(total_bytes_added)}")
    log(f"Total Bytes Removed : {fmt_size(total_bytes_removed)}")

    net = total_bytes_removed - total_bytes_added
    net_color = Color.GREEN if net >= 0 else Color.RED
    log(f"Total Net Saved     : {c(fmt_size(net), net_color)}")
    log(f"Total Errors        : {c(total_errors, Color.RED)}")

    if all_errors:
        log(c("All Errors log:", Color.RED))
        for path, err in all_errors[:50]:
            log(f"  - {path}")
            log(f"      {err}")
        if len(all_errors) > 50:
            log(f"  ... and {len(all_errors) - 50} more error(s).")

    pause_for_input()


def show_main_menu(config):
    clear_screen()
    print_header("AUDIO & IMAGE PROCESSING SUITE (Stable Final Edition)")

    _print_script_list()

    print(f"  16. {'Run All':<23}  {config.get('run_all_order', DEFAULT_RUN_ALL_ORDER)}")
    print(f"  17. {'Run Custom':<23}  (select order and scripts)")
    print(f"  18. {'Configuration':<23}  (39 options incl. Run All order)")
    print(f"  19. {'Dependencies':<23}  (download latest tools)")
    print(c(f"   0. {'Exit':<23}", Color.YELLOW))

    print()
    print("  NOTE: Only FLAC receives lossless recompression.")
    print("        Other audio formats receive safe tag operations only.")


def manage_dependencies():
    from .fetchdeps import (
        DISPLAY_NAMES, dependency_rows, install_dependency, refresh_tool_cache,
    )

    clear_screen()
    print_header("DEPENDENCY MANAGER")

    # block=True: the table is printed once and the CLI has nobody to return
    # to, so the live GitHub check runs inline instead of in the background.
    try:
        rows = dependency_rows(block=True)
    except Exception as e:
        log(c(f"ERROR: could not query GitHub: {e}", Color.RED))
        pause_for_input()
        return

    print()
    name_w = max(len(name) for name in DISPLAY_NAMES.values())
    # Same states, same words as the GUI: `installed` vs. the pinned `target`
    # the installer fetches vs. what upstream actually releases.
    state_text = {
        "ok": ("up to date", Color.GREEN),
        "update": ("update available", Color.YELLOW),
        "missing": ("not installed", Color.RED),
        "error": ("check failed", Color.RED),
    }
    for row in rows:
        iv = row["installed_version"] or row["detected_version"] or "-"
        lv = row["latest_version"] or "?"
        uv = row["upstream_version"] or "?"
        label, color = state_text.get(row["state"], (row["state"], Color.RED))
        print(
            f"  {row['name']:<{name_w}}  installed {iv:<10} target {lv:<10} "
            f"available {uv:<10} {c(label, color)}"
        )
    print()

    choice = input("Install/update all tools now? (y/n): ").strip().lower()
    if choice in ("y", "yes"):
        for key in DISPLAY_NAMES:
            try:
                install_dependency(key, log=log)
            except Exception as e:
                log(c(f"FAILED {DISPLAY_NAMES[key]}: {e}", Color.RED))
        tools = refresh_tool_cache()
        log(
            c(
                f"Detected {len(tools)}/{len(DISPLAY_NAMES)} tools: "
                + ", ".join(f"{k} v{v['version']}" for k, v in tools.items()),
                Color.GREEN,
            )
        )

    pause_for_input()


def main():
    """Interactive session; Ctrl+C aborts it, other errors are reported.

    The handler lives here (not in `__main__`) so `python -m mlo` really gets
    it: a failing runner used to kill the CLI with a raw traceback.
    """
    try:
        _session()
    except KeyboardInterrupt:
        print(c("\nAborted (Ctrl+C).", Color.YELLOW))
    except Exception:
        print(c("\n--- FATAL ERROR ---", Color.RED))
        traceback.print_exc()
        print("-------------------")


def _session():
    if not HAS_MUTAGEN:
        print(c("ERROR: mutagen is required.  pip install mutagen", Color.RED))
        return

    import mlo.tools as _tools_mod
    _tools_mod._TOOLS_CACHE = None

    config = load_config()

    runners = build_script_runners()

    while True:
        show_main_menu(config)
        choice = input(c("Select option: ", Color.CYAN)).strip()

        if choice == "0":
            break

        elif choice.isdigit() and int(choice) in SCRIPT_LABELS:
            script_id = int(choice)
            name, runner = runners[script_id]

            reason = _gate_reason(config, script_id)
            if reason:
                print(c(f"\nSkipped: {reason}", Color.YELLOW))
                pause_for_input()
                continue

            clear_screen()
            print(f">>> Starting: {c(name, Color.BOLD)}")
            print_separator()

            stats = _run_isolated(name, runner, config)

            if stats.get("is_grader"):
                print_grade_results(stats, title=f"RESULTS - {name}")
            else:
                print_results(stats, title=f"RESULTS - {name}")

            pause_for_input()

        elif choice == "16":
            order = config.get("run_all_order", DEFAULT_RUN_ALL_ORDER)
            run_scripts_sequence(config, order, title="RUN ALL SCRIPTS")

        elif choice == "17":
            order = show_custom_menu()

            if not order:
                print(c("\nNo valid scripts selected.", Color.YELLOW))
                pause_for_input()
                continue

            run_scripts_sequence(config, order, title="CUSTOM RUN ORDER")

        elif choice == "18":
            show_config_menu(config)

        elif choice == "19":
            manage_dependencies()

        else:
            print(c("\nInvalid option. Please try again.", Color.RED))
            pause_for_input()
