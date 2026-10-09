#!/usr/bin/env python3
"""Emit the AltStore/SideStore source that lists this release's IPA.

A sideloading tool shows a name, a version, a bundle id, an icon and a size
for each app it offers. Without a source file the user has to find the right
`.ipa` on the releases page by hand and hope the tool reads its plist — which
is exactly what "the details are wrong when it is imported" means in practice.
SideStore and AltStore both read the JSON below, and a release asset is
reachable at a STABLE url across versions:

    https://github.com/<owner>/<repo>/releases/latest/download/source.json

so one source entry can be added once and always point at the newest build.

The version/buildVersion/size are taken from the IPA actually being released,
not from the source tree, so a mismatch is impossible.

Run:  python tools/make_sidestore_source.py <ipa> <version> <tag> <out.json>
"""
import json
import os
import sys

REPO = "dillydalli3r/la-musica"
RAW = f"https://raw.githubusercontent.com/{REPO}/main"
# A 1024px square with the app's artwork — what SideStore renders for the app
# row and the install sheet.
ICON = f"{RAW}/desktop/src-tauri/icons/ios/AppIcon-512@2x.png"
# The app's own iOS floor, stated in the source so SideStore can
# grey out a device that cannot run it (tauri.conf.json
# `bundle.iOS.minimumSystemVersion`).
MIN_OS = "14.0"

DESCRIPTION = (
    "la musica is a self-hosted music library optimiser and player: it tags, "
    "organizes, grades and streams your own library, and can talk to a server "
    "you run at home. The iOS build is a client — point it at your server "
    "during setup, or at a backend running on the device itself."
)


def build(ipa, version, tag, date=None):
    size = os.path.getsize(ipa)
    download = (f"https://github.com/{REPO}/releases/download/{tag}/"
                f"{os.path.basename(ipa)}")
    entry = {
        "version": version,
        # AltStore treats buildVersion as the tie-breaker between two builds
        # of the same version; a release has one build, so they move together.
        "buildVersion": version,
        # `date` is REQUIRED by AltStore/SideStore's decoder — a source
        # without it fails to import with "Key 'date' not found. No value
        # associated with key CodingKeys" (the exact error the owner hit on
        # their iPhone, 2026-09-24). ISO 8601, as the format asks for.
        "date": date or _now_iso(),
        "downloadURL": download,
        "size": size,
        # The app's own floor (tauri.conf.json bundle.iOS.minimumSystemVersion):
        # iOS 14.0. Stated so SideStore can grey out an incompatible device
        # instead of failing the install.
        "minOSVersion": MIN_OS,
        "localizedDescription": DESCRIPTION,
    }
    return {
        "name": "la musica",
        "identifier": "com.musiclibraryoptimizer.lamusica.source",
        "subtitle": "Self-hosted music library optimiser.",
        "description": DESCRIPTION,
        "iconURL": ICON,
        "website": f"https://github.com/{REPO}",
        "tintColor": "#c084fc",
        "apps": [{
            "name": "la musica",
            "bundleIdentifier": "com.musiclibraryoptimizer.lamusica",
            "developerName": "dillydalli3r",
            "subtitle": "Your library, optimized.",
            "localizedDescription": DESCRIPTION,
            "iconURL": ICON,
            "tintColor": "#c084fc",
            # AltStore accepts a CLOSED set of categories (developer,
            # entertainment, games, lifestyle, other, photo-video, social,
            # utilities) and decodes it into an enum — "music" is not one of
            # them, and a value outside the set is the next decode failure
            # after `date`. `entertainment` is the honest fit for a player.
            "category": "entertainment",
            "screenshots": [],
            "versions": [entry],
        }],
        "news": [],
    }


def _now_iso():
    """The release date, ISO 8601 UTC (AltStore's own example format)."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    date = None
    for a in sys.argv[1:]:
        if a.startswith("--date="):
            date = a.split("=", 1)[1].strip() or None
    if len(args) != 4:
        sys.exit("Run:  python tools/make_sidestore_source.py "
                 "<ipa> <version> <tag> <out.json> [--date=ISO8601]")
    ipa, version, tag, out = args
    if not os.path.isfile(ipa):
        sys.exit(f"make_sidestore_source: no IPA at {ipa}")
    source = build(ipa, version.lstrip("v"), tag, date=date)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(source, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"make_sidestore_source: {source['name']} {version.lstrip('v')} "
          f"({os.path.getsize(ipa)} bytes, {source['apps'][0]['versions'][0]['date']}) -> {out}")


if __name__ == "__main__":
    main()
