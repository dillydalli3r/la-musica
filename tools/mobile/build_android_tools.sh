#!/usr/bin/env bash
# Build the CLI tools an Android build can execute: ffmpeg, ffprobe, flac,
# metaflac, for aarch64-linux-android.
#
# Why this exists at all: the desktop toolchain (mlo/fetchdeps.py) downloads
# Windows/macOS builds from GitHub releases, and there are no such releases for
# Android — BtbN ships win64, the ffmpeg-kit AARs contain JNI libraries rather
# than command-line programs, and Termux's packages link against a Termux prefix
# that a foreign app does not have. So the only deterministic source is the
# pinned upstream source plus the pinned NDK, which is what this script does.
#
# iOS gets nothing from here on purpose: an iOS app cannot execute another
# program at all, so ffmpeg/flac are simply unavailable in an iOS-local backend
# and the app reports the affected features instead of failing on first use.
#
# Output: <out-dir>/{ffmpeg,ffprobe,flac,metaflac}, statically linked against
# their own libraries, dynamically linked only against Bionic (which is on every
# device). bundle.py copies them into jniLibs as lib<name>.so — AGP only
# packages that shape — and the shell points mlo/tools.py at that directory
# through MLO_BUNDLED_TOOLS.
#
# Usage:  bash tools/mobile/build_android_tools.sh <out-dir> [jobs]
# Needs:  ANDROID_HOME (or NDK_HOME) with an NDK, a C toolchain, make, curl,
#         tar. Job time on a 4-core GitHub runner: ffmpeg ~8 min, flac <1 min.

set -euo pipefail

OUT="${1:?usage: build_android_tools.sh <out-dir> [jobs]}"
# Absolute before anything cd's: the build steps below run inside $WORK, so a
# relative out-dir would resolve against the wrong directory and the final copy
# would fail with "No such file or directory" — after a ten-minute ffmpeg build
# that had already succeeded.
JOBS="${2:-$(nproc 2>/dev/null || echo 4)}"
API=24
TRIPLE="aarch64-linux-android"
ABI="arm64-v8a"

# Pinned source releases. The hash is the whole point: the same commit of this
# repo must build the same binaries, and a re-tagged upstream tarball must stop
# the build rather than change what ships inside the app.
FFMPEG_VERSION="7.1.1"
FFMPEG_SHA256="733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1"
FLAC_VERSION="1.5.0"
FLAC_SHA256="f2c1c76592a82ffff8413ba3c4a1299b6c7ab06c734dee03fd88630485c2b920"

WORK="$(pwd)/.mobile-tools-build"
mkdir -p "$WORK" "$OUT"
OUT="$(cd "$OUT" && pwd)"

# --- locate the NDK -----------------------------------------------------------
NDK="${NDK_HOME:-${ANDROID_NDK_HOME:-}}"
if [ -z "$NDK" ]; then
  SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
  [ -n "$SDK" ] || { echo "ERROR: no ANDROID_HOME/NDK_HOME — the NDK is what cross-links this" >&2; exit 1; }
  NDK="$(ls -d "$SDK"/ndk/* 2>/dev/null | sort -V | tail -1)"
fi
[ -n "$NDK" ] && [ -d "$NDK" ] || { echo "ERROR: no NDK found under ANDROID_HOME=$ANDROID_HOME" >&2; exit 1; }
HOST_TAG="linux-x86_64"
[ "$(uname -s)" = "Darwin" ] && HOST_TAG="darwin-x86_64"
TOOLCHAIN="$NDK/toolchains/llvm/prebuilt/$HOST_TAG"
[ -d "$TOOLCHAIN" ] || { echo "ERROR: $TOOLCHAIN does not exist (unexpected NDK layout)" >&2; exit 1; }
CC="$TOOLCHAIN/bin/${TRIPLE}${API}-clang"
# The C++ driver matters: flac's build links a C++ EXAMPLE program against the
# library it just cross-compiled, and without CXX that link falls back to the
# HOST linker, which rejects the Android objects with "file in wrong format".
# The examples are disabled below, but the toolchain stays correct either way.
CXX="$TOOLCHAIN/bin/${TRIPLE}${API}-clang++"
AR="$TOOLCHAIN/bin/llvm-ar"
RANLIB="$TOOLCHAIN/bin/llvm-ranlib"
STRIP="$TOOLCHAIN/bin/llvm-strip"
[ -x "$CC" ] || { echo "ERROR: $CC missing" >&2; exit 1; }
echo "[tools] NDK $(basename "$NDK") · api $API · $ABI"

fetch_verified() {
  local url="$1" sha="$2" dest="$3"
  if [ ! -f "$dest" ]; then
    echo "[tools] fetching $url"
    curl -fsSL -o "$dest.part" "$url"
    mv "$dest.part" "$dest"
  fi
  echo "$sha  $dest" | sha256sum -c - >/dev/null 2>&1 \
    || { echo "ERROR: $dest is not the pinned $sha (delete it and re-run)" >&2; exit 1; }
}

# --- ffmpeg -------------------------------------------------------------------
# Deliberately NOT the desktop build: no network stack, no external encoders.
# What the backend actually drives ffmpeg for on Android is local file work —
# decoding every format the library holds, lossless source conversion, the
# portable EBU R128 meter, and stream-copying video containers. External
# libraries (x264, openssl) would each need their own cross-build, and libx264
# would drag in the GPL question for a store build, so the re-encode fallback in
# mlo/remux.py stays unavailable on Android — reported, not faked.
build_ffmpeg() {
  local tarball="ffmpeg-$FFMPEG_VERSION.tar.xz"
  fetch_verified "https://ffmpeg.org/releases/$tarball" "$FFMPEG_SHA256" "$WORK/$tarball"
  rm -rf "$WORK/ffmpeg-$FFMPEG_VERSION"
  tar -xf "$WORK/$tarball" -C "$WORK"
  cd "$WORK/ffmpeg-$FFMPEG_VERSION"
  ./configure \
    --prefix="$WORK/ffmpeg-prefix" \
    --target-os=android --arch=aarch64 --cpu=armv8-a \
    --enable-cross-compile --sysroot="$TOOLCHAIN/sysroot" \
    --cc="$CC" --cxx="${CC}++" --ar="$AR" --ranlib="$RANLIB" --strip="$STRIP" \
    --enable-static --disable-shared \
    --disable-network --disable-doc --disable-debug --disable-ffplay \
    --disable-postproc --disable-avdevice --disable-symver
  make -j"$JOBS"
  make install
  cp "$WORK/ffmpeg-prefix/bin/ffmpeg" "$OUT/ffmpeg"
  cp "$WORK/ffmpeg-prefix/bin/ffprobe" "$OUT/ffprobe"
}

# --- flac ---------------------------------------------------------------------
build_flac() {
  local tarball="flac-$FLAC_VERSION.tar.xz"
  fetch_verified "https://downloads.xiph.org/releases/flac/$tarball" "$FLAC_SHA256" "$WORK/$tarball"
  rm -rf "$WORK/flac-$FLAC_VERSION"
  tar -xf "$WORK/$tarball" -C "$WORK"
  cd "$WORK/flac-$FLAC_VERSION"
  ./configure --host="$TRIPLE" --prefix="$WORK/flac-prefix" \
    --disable-shared --enable-static --disable-ogg \
    --disable-examples \
    CC="$CC" CXX="$CXX" AR="$AR" RANLIB="$RANLIB" STRIP="$STRIP" \
    CFLAGS="-O2 -fPIC" CXXFLAGS="-O2 -fPIC"
  make -j"$JOBS"
  make install
  cp "$WORK/flac-prefix/bin/flac" "$OUT/flac"
  cp "$WORK/flac-prefix/bin/metaflac" "$OUT/metaflac"
}

build_ffmpeg
build_flac

# --- what we just built ------------------------------------------------------
# Strip and verify: bundle.py checks the same ELF header, but failing here says
# which tool is wrong instead of which file in a staged tree.
for tool in ffmpeg ffprobe flac metaflac; do
  "$STRIP" --strip-unneeded "$OUT/$tool" 2>/dev/null || true
  chmod 755 "$OUT/$tool"
  file "$OUT/$tool" 2>/dev/null | sed 's/^/  /' || true
  "python3" - "$OUT/$tool" <<'PY'
import struct, sys
head = open(sys.argv[1], "rb").read(20)
assert head[:4] == b"\x7fELF", f"{sys.argv[1]} is not an ELF binary"
machine = struct.unpack_from("<H", head, 18)[0]
assert machine == 0xB7, f"{sys.argv[1]} is machine 0x{machine:x}, expected AArch64"
print(f"  ok {sys.argv[1]} (AArch64 ELF)")
PY
done
echo "[tools] done: $OUT"
