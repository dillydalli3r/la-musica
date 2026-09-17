"""Interactive console menu (python -m mlo). The GUI app uses the modules directly."""
import os

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
try:
    from .accurip import run_generate_accurip
except ImportError:
    from .stats import new_stats as _ns_accurip

    def run_generate_accurip(config):
        from .ui import log
        log("AccurateRip unavailable (import failed)")
        return _ns_accurip()
try:
    from .format_all import run_format_all
except ImportError:
    from .stats import new_stats as _ns_fmtall

    def run_format_all(config):
        from .ui import log
        log("Format All unavailable (import failed)")
        return _ns_fmtall()
from .remux import run_remux_videos
try:
    from .audiometa import run_analyze_audiometa
except ImportError:
    run_analyze_audiometa = None
try:
    from .lyrics_fetch import run_fetch_lyrics
except ImportError:
    run_fetch_lyrics = None
from .report import print_results, print_grade_results, print_combined_results
from .tools import detect_all_tools
from .ui import (
    c, Color, clear_screen, print_header, print_separator,
    pause_for_input, log, fmt_size,
)
from .config import load_config, save_config, DEFAULT_RUN_ALL_ORDER

# Enable ANSI escape sequences on Windows 10+ consoles.
if os.name == "nt":
    os.system("")


def edit_run_all_order(config):
    while True:
        clear_screen()
        print_header("EDIT RUN ALL ORDER")

        print("  Available scripts:")
        print("    1. Format Lyrics")
        print("    2. Format CUEs")
        print("    3. Optimize FLACs")
        print("    4. Grade Library")
        print("    5. Process Images")
        print("    6. Audit Library")
        print("    7. DR & ReplayGain")
        print("    8. Auto Tagging")
        print("    9. AccurateRip")
        print("   10. Format All")
        print("   11. Video Remux (MKV)")
        print("   12. Key & BPM")
        print("   13. Fetch Lyrics")
        print("   14. Beets Tagging")
        print("   15. Lyrics Translate/Transliterate")
        print("-" * 72)

        current = config.get("run_all_order", DEFAULT_RUN_ALL_ORDER)
        print(f"  Current order: {c(','.join(map(str, current)), Color.CYAN)}")
        print("Enter new order (comma-separated, e.g. 3,1,2,5,4) or '0' to cancel:")

        choice = input(c("> ", Color.CYAN)).strip()

        if choice == "0":
            return

        parts = [p.strip() for p in choice.split(",") if p.strip()]
        order = []
        valid = True

        for p in parts:
            if p.isdigit() and 1 <= int(p) <= 15:
                pid = int(p)
                if pid not in order:
                    order.append(pid)
            else:
                print(c(f"  Invalid entry: '{p}'", Color.RED))
                valid = False

        if valid and order:
            config["run_all_order"] = order
            save_config(config)
            print(c("\nSaved.", Color.GREEN))
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
        print(f"  7. Re-encode to JXL         : {config.get('reencode_to_jxl', True)}")
        print(f"  8. Convert JXL Back         : {config.get('convert_jxl_back', False)}")
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
        print(f" 22. Thorough Audit           : {config.get('audit_thorough', False)}")
        print(f" 23. Force Audit              : {config.get('force_audit', False)}")
        print(f" 24. Audit Cutoff Allowance   : {config.get('audit_cutoff_allow', 0)} Hz (0=default)")
        print(f" 25. Audit Clipping           : {config.get('audit_clipping', True)}")
        print(f" 26. Audit MQA                : {config.get('audit_mqa', True)}")
        print(f" 27. Audit AI Detection       : {config.get('audit_ai', True)}")
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
                new_val = int(input("Cutoff allowance in Hz (0 = CLI default 19600, 20000+ for HD masters): ").strip())
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
                "27": "AI detection",
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

        elif choice == "0":
            break

        else:
            print(c("\nInvalid option.", Color.RED))
            pause_for_input()


def show_custom_menu():
    clear_screen()
    print_header("CUSTOM RUN ORDER")

    print("  Available scripts:")
    print("    1. Format Lyrics")
    print("    2. Format CUEs")
    print("    3. Optimize FLACs")
    print("    4. Grade Library")
    print("    5. Process Images")
    print("    6. Audit Library")
    print("    7. DR & ReplayGain")
    print("    8. Auto Tagging")
    print("    9. AccurateRip")
    print("   10. Format All")
    print("   11. Video Remux (MKV)")
    print("   12. Key & BPM")
    print("   13. Fetch Lyrics")
    print("   14. Beets Tagging")
    print("   15. Lyrics Translate/Transliterate")
    print_separator()

    print("Enter the order of scripts to run (comma-separated, e.g. '3,1,2,5'):")
    choice = input(c("> ", Color.CYAN)).strip()

    parts = [p.strip() for p in choice.split(",") if p.strip()]
    order = []

    for p in parts:
        if p in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11",
                 "12", "13", "14", "15"):
            order.append(int(p))
        else:
            print(c(f"  Ignoring invalid entry: '{p}'", Color.YELLOW))

    if order:
        input("\nPress Enter to start...")
        return order

    return []


def build_script_runners():
    """Map script id -> (label, runner) for every pipeline script.

    Optional scripts whose dependency fails to import are omitted, so a
    caller can degrade gracefully (report/skip) instead of crashing.
    """
    runners = {
        1: ("Format Lyrics", run_format_lyrics),
        2: ("Format CUEs", run_format_cues),
        3: ("Optimize FLACs", run_optimize_flacs),
        4: ("Grade Library", run_grade_library),
        5: ("Process Images", run_process_images),
        6: ("Audit Library", run_audit_library),
        7: ("DR & ReplayGain", run_calc_dr_replaygain),
        8: ("Auto Tagging", run_auto_tagging),
        9: ("AccurateRip", run_generate_accurip),
        10: ("Format All", run_format_all),
        11: ("Video Remux", run_remux_videos),
    }
    try:
        from .audiometa import run_analyze_audiometa
        runners[12] = ("Key & BPM", run_analyze_audiometa)
    except ImportError:
        pass
    try:
        from .lyrics_fetch import run_fetch_lyrics
        runners[13] = ("Fetch Lyrics", run_fetch_lyrics)
    except ImportError:
        pass
    try:
        from server.beetscfg import run_beets_tagging
        runners[14] = ("Beets Tagging", run_beets_tagging)
    except ImportError:
        pass
    try:
        from .lyrics_xlit import run_lyrics_xlit
        runners[15] = ("Lyrics Translate/Transliterate", run_lyrics_xlit)
    except ImportError:
        pass
    return runners


def run_scripts_sequence(config, script_ids, title):
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
        try:
            name, runner = runners[script_id]
        except KeyError:
            log(c(f"  Skipping unknown script id: {script_id}", Color.YELLOW))
            continue
        if runner is None:
            log(c(f"  Skipping unavailable script: {name}", Color.YELLOW))
            continue

        if i > 0:
            if auto_advance:
                log(f"\n--- Auto-advancing to: {c(name, Color.CYAN)} ---\n")
            else:
                pause_for_input()
                clear_screen()

        clear_screen()
        print(f">>> Starting: {c(name, Color.BOLD)}")
        print_separator()

        s = runner(config)
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

    print("  1. Format Lyrics    (multi-format + MEDIA/SOURCE normalization)")
    print("  2. Format CUEs      (.cue files)")
    print("  3. Optimize FLACs   (lossless re-encode)")
    print("  4. Grade Library    (detailed human-readable report)")
    print("  5. Process Images   (JXL / lossless / JXL-back)")
    print("  6. Audit Library    (AudioAuditor: fake lossless / AI / MQA)")
    print("  7. DR & ReplayGain  (rsgain + simple-dr-meter tags)")
    print("  8. Auto Tagging     (advisory + instrumental)")
    print("  9. AccurateRip     (CUETools .accurip files)")
    print(" 10. Format All      (canonical trim pass)")
    print(" 11. Video Remux     (any video -> MKV, audio -> FLAC)")
    print(" 12. Key & BPM       (musical key + tempo tags)")
    print(" 13. Fetch Lyrics    (LRCLIB synced/plain)")
    print(" 14. Beets Tagging   (MusicBrainz via beets)")
    print(" 15. Lyrics Xlit     (translate/transliterate)")
    print(f" 16. Run All          {config.get('run_all_order', DEFAULT_RUN_ALL_ORDER)}")
    print(" 17. Run Custom       (select order)")
    print(" 18. Configuration")
    print(" 19. Dependencies     (download latest tools)")
    print(c("  0. Exit", Color.YELLOW))

    print()
    print("  NOTE: Only FLAC receives lossless recompression.")
    print("        Other audio formats receive safe tag operations only.")


def manage_dependencies():
    from .fetchdeps import (
        DISPLAY_NAMES, installed_versions, latest_versions,
        install_dependency, refresh_tool_cache,
    )

    clear_screen()
    print_header("DEPENDENCY MANAGER")

    installed = installed_versions()
    try:
        latest = latest_versions()
    except Exception as e:
        log(c(f"ERROR: could not query GitHub: {e}", Color.RED))
        pause_for_input()
        return

    print()
    for key, name in DISPLAY_NAMES.items():
        iv = installed.get(key, "-")
        lv = latest.get(key, "?")
        state = (
            c("up to date", Color.GREEN) if iv == lv and iv != "-"
            else c("update available", Color.YELLOW) if iv != "-"
            else c("not installed", Color.RED)
        )
        print(f"  {name:<15} installed {iv:<10} latest {lv:<10} {state}")
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

        elif choice in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
                         "11", "12", "13", "14", "15"):
            script_id = int(choice)
            try:
                name, runner = runners[script_id]
            except KeyError:
                print(c("\nScript not available in this menu.", Color.YELLOW))
                pause_for_input()
                continue
            if runner is None:
                print(c("\nScript unavailable (missing optional dependency).",
                        Color.YELLOW))
                pause_for_input()
                continue

            clear_screen()
            print(f">>> Starting: {c(name, Color.BOLD)}")
            print_separator()

            stats = runner(config)

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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        print(c("\n--- FATAL ERROR ---", Color.RED))
        traceback.print_exc()
        print("-------------------")
