"""Human-readable result reports for processing and grading runs."""
import re

from .ui import log, c, Color, fmt_size, fmt_short_bytes, print_separator

ANSI_STRIP_RE = re.compile(r"\x1b\[[0-9;]*m")


def _vis_len(s):
    """Length of a string ignoring embedded ANSI color codes."""
    return len(ANSI_STRIP_RE.sub("", str(s)))


def render_table(headers, rows, aligns=None):
    """Render a unicode box table; returns a list of lines.

    Cells may contain ANSI color codes - column widths are computed from
    visible characters only. aligns is a list of "left"/"right" per column.
    """
    ncols = len(headers)
    aligns = aligns or ["left"] * ncols

    widths = [_vis_len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _vis_len(cell))

    def pad(cell, i):
        s = str(cell)
        gap = widths[i] - _vis_len(s)
        return s + " " * gap if aligns[i] != "right" else " " * gap + s

    def fmt(cells):
        return " │ ".join(pad(cell, i) for i, cell in enumerate(cells))

    # Each column occupies width + 2 (one space of padding on each side).
    top = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    mid = "├" + "┼".join("─" * (w + 2) for w in widths) + "┤"
    bot = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"

    lines = [top, "│ " + fmt(headers) + " │", mid]
    for row in rows:
        lines.append("│ " + fmt(row) + " │")
    lines.append(bot)
    return lines


def fmt_net(n):
    n = int(n)
    if abs(n) >= 1024 * 1024:
        return f"{n / (1024 * 1024):,.2f} MB"
    if abs(n) >= 1024:
        return f"{n / 1024:,.2f} KB"
    return f"{n:,} B"


def print_results(stats, title="RESULTS"):
    """Compact result summary: one counts line + a prominent byte line."""
    print_separator()
    log(c(title, Color.BOLD))

    counts = []
    counts.append(f"processed {stats.get('total_scanned', 0)}")
    counts.append(
        f"modified {c(str(stats.get('modified_count', 0)), Color.GREEN)}"
        if stats.get("modified_count", 0)
        else f"modified {stats.get('modified_count', 0)}"
    )
    counts.append(
        f"skipped {c(str(stats.get('skipped_count', 0)), Color.YELLOW)}"
        if stats.get("skipped_count", 0)
        else f"skipped {stats.get('skipped_count', 0)}"
    )
    counts.append(
        f"errors {c(str(stats.get('error_count', 0)), Color.RED)}"
        if stats.get("error_count", 0)
        else f"errors {stats.get('error_count', 0)}"
    )
    log("  " + " · ".join(counts))

    removed = stats.get("total_bytes_removed", 0)
    added = stats.get("total_bytes_added", 0)
    net = removed - added
    if removed or added:
        if net >= 0:
            net_part = c(f"▼ {fmt_short_bytes(net)} saved", Color.GREEN)
        else:
            net_part = c(f"▲ {fmt_short_bytes(-net)} added", Color.RED)
        log(f"  {net_part}  "
            f"({c('removed ' + fmt_short_bytes(removed), Color.GREEN)} · "
            f"{c('added ' + fmt_short_bytes(added), Color.RED)})")

    if stats.get("errors"):
        log(c("  errors:", Color.RED))
        for path, err in stats["errors"][:50]:
            log(f"    - {path}: {err}")
        if len(stats["errors"]) > 50:
            log(f"    ... and {len(stats['errors']) - 50} more.")

    print_separator()


def print_grade_results(stats, title="GRADE RESULTS"):
    """Compact grading summary: counts, pass rate, top issues."""
    print_separator()
    log(c(title, Color.BOLD))

    gd = stats.get("grade_dist", {})

    counts = [
        f"graded {stats.get('total_scanned', 0)}",
        f"skipped {stats.get('skipped_count', 0)}",
        f"passed {c(str(gd.get('PASS', 0)), Color.GREEN)}",
        f"failed {c(str(gd.get('FAIL', 0)), Color.RED)}",
    ]
    if stats.get("error_count", 0):
        counts.append(f"errors {c(str(stats.get('error_count', 0)), Color.RED)}")
    log("  " + " · ".join(counts))

    if stats.get("summary_total"):
        pct = stats.get("summary_pass", 0) / stats["summary_total"] * 100.0
        log(f"  check pass rate: {pct:.1f}% "
            f"({stats.get('summary_pass', 0)}/{stats['summary_total']} checks)")

    _log_issue_summary(stats)

    if stats.get("errors"):
        log(c("  errors:", Color.RED))
        for path, err in stats["errors"][:50]:
            log(f"    - {path}: {err}")
        if len(stats["errors"]) > 50:
            log(f"    ... and {len(stats['errors']) - 50} more.")

    print_separator()


def _log_issue_summary(stats, indent="  ", limit=5):
    issues = stats.get("issue_counts") or {}
    if not issues:
        return
    ranked = sorted(issues.items(), key=lambda kv: (-kv[1], kv[0]))
    parts = [f"{field}×{count}" for field, count in ranked[:limit]]
    if len(ranked) > limit:
        parts.append(f"+{len(ranked) - limit} more")
    log(f"{indent}top problems: {', '.join(parts)}")


def print_grader_details(name, stats):
    """Detailed grading block shown alongside the combined table."""
    gd = stats.get("grade_dist", {})
    total_checks = stats.get("summary_total", 0)
    if total_checks:
        rate = (
            f"{stats.get('summary_pass', 0) / total_checks * 100:.1f}% "
            f"({stats.get('summary_pass', 0)}/{total_checks} checks)"
        )
    else:
        rate = "n/a"

    log("")
    log(c(f"  {name} — details", Color.BOLD))
    log(
        f"    graded {stats.get('total_scanned', 0)} · skipped "
        f"{stats.get('skipped_count', 0)} · "
        f"passed {c(str(gd.get('PASS', 0)), Color.GREEN)} · "
        f"failed {c(str(gd.get('FAIL', 0)), Color.RED)}"
    )
    log(f"    check pass rate: {rate}")
    _log_issue_summary(stats, indent="    ")


def print_combined_results(per_script, title="COMBINED RESULTS"):
    """Print the per-script summary as a box table plus grader details.

    per_script is a list of (name, stats) pairs in execution order.
    """
    print_separator()
    log(c(title, Color.BOLD))
    print_separator()

    headers = [
        "Script", "Processed", "Modified", "Passed", "Skipped", "Failed",
        "Errors", "Net Saved",
    ]
    rows = []

    tot_modified = tot_passed = 0
    tot_skipped = tot_failed = tot_errors = 0
    # The "Processed" column counts two different units: a grader's stats are
    # per file (audit) and per album (DR/AccurateRip), so one total summing
    # both said nothing at all. They are totalled apart and printed apart.
    tot_files = tot_albums = 0
    net_total = 0
    grader_runs = []

    for name, s in per_script:
        if s.get("is_grader"):
            gd = s.get("grade_dist", {})
            passed = gd.get("PASS", 0)
            failed = gd.get("FAIL", 0)
            grader_runs.append((name, s))
            is_files = s.get("audit_status_counts") is not None
            if is_files:
                tot_files += s.get("total_scanned", 0)
            else:
                tot_albums += s.get("total_scanned", 0)
            tot_passed += passed
            tot_failed += failed
            tot_errors += s.get("error_count", 0)
            rows.append([
                name,
                f"{s.get('total_scanned', 0)} "
                + ("files" if is_files else "albums"),
                "—",
                c(str(passed), Color.GREEN),
                "—",
                c(str(failed), Color.RED),
                str(s.get("error_count", 0)),
                "—",
            ])
        else:
            net = s.get("total_bytes_removed", 0) - s.get("total_bytes_added", 0)
            net_total += net
            tot_modified += s.get("modified_count", 0)
            tot_skipped += s.get("skipped_count", 0)
            tot_errors += s.get("error_count", 0)
            rows.append([
                name,
                str(s.get("total_scanned", 0)),
                str(s.get("modified_count", 0)),
                "—",
                str(s.get("skipped_count", 0)),
                "—",
                c(str(s.get("error_count", 0)),
                  Color.RED if s.get("error_count", 0) else Color.RESET),
                c(fmt_net(net), Color.GREEN if net >= 0 else Color.RED),
            ])

    net_color = Color.GREEN if net_total >= 0 else Color.RED
    # Only the units actually present are named: "N files + M albums" when the
    # run mixed both, a plain number when every script counted the same thing.
    total_parts = []
    if tot_files:
        total_parts.append(f"{tot_files} files")
    if tot_albums:
        total_parts.append(f"{tot_albums} albums")
    rows.append([
        c("TOTAL", Color.BOLD),
        c(" + ".join(total_parts) or "0", Color.BOLD),
        c(str(tot_modified), Color.BOLD),
        c(str(tot_passed), Color.BOLD),
        c(str(tot_skipped), Color.BOLD),
        c(str(tot_failed), Color.BOLD),
        c(str(tot_errors), Color.BOLD),
        c(fmt_net(net_total), net_color),
    ])

    for line in render_table(
        headers, rows,
        aligns=["left", "right", "right", "right", "right", "right",
                "right", "right"],
    ):
        # Printed directly, not through log(): the "[HH:MM:SS] " prefix would
        # hang off the box and break every border row.
        print(line, flush=True)

    for name, s in grader_runs:
        print_grader_details(name, s)

    print_separator()
