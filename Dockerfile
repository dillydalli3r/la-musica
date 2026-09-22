# syntax=docker/dockerfile:1

# ---------- Stage 1: build the React frontend ----------
FROM node:22-alpine AS web-build
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---------- Stage 2: Python backend + system toolchain ----------
# The Debian release is PINNED (trixie) and not left to float: the package
# names below are that release's — libicu76, libjxl-tools, rsgain — and
# `python:3.12-slim` moved from bookworm to trixie on its own, which quietly
# made every "bookworm has no such package" note here wrong and would have
# broken the build the next time a name changed.
FROM python:3.12-slim-trixie AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MLO_MUSIC_FOLDER=/music \
    MLO_SERVER_HOST=0.0.0.0

# Core audio/image tools for the optimization pipeline. These are the Linux
# counterparts of the downloads in mlo/fetchdeps.py: the in-app installer
# fetches the native Linux builds where upstream ships one (LINUX_BINARIES:
# oxipng, slskd, AudioAuditor, CUETools through the mono runtime, and upstream's
# rsgain, fpcalc, libjxl and libjpeg-turbo) and points at the system package for
# everything the distro provides (LINUX_PACKAGES), so every tool the app knows
# is installable and runnable in this image.
#
# libjxl-tools / libjpeg-turbo-progs stay even though the installer can now
# fetch both: they are this image's copies, on PATH from the first boot, so the
# rows read Ready without a download — and they are the tools an ARM host has,
# where upstream publishes no libjxl build at all (LINUX_BINARIES.libjxl is
# x86-64 only). ffmpeg, flac, rsgain and libchromaprint-tools have no
# fetchable Linux build of their own and are ONLY this.
#
# libsndfile1 / libgomp1 / libicu76 back the runtime-installed tools — librosa's
# soundfile and numba imports; slskd's and AudioAuditor's .NET runtimes, which
# dlopen ICU at startup and refuse to boot without it (a dependency no `ldd`
# shows, since it is loaded by name). php-cli is the Logchecker phar's runtime
# and mono-runtime runs CUETools' console tool (both verified here: the phar
# scores a log, CUETools.ARCUE.exe prints its usage under mono).
#
# System.Drawing is the part mono-runtime does NOT bring: CUETools' ARCUE pass
# loads it to verify a disc, so without libgdiplus and mono's own
# System.Drawing assembly every AccurateRip run died on "Could not load file
# or assembly 'System.Drawing'" and wrote no .accurip at all — a CD album then
# graded as "no .accurip verdict verifies this disc", which is what a missing
# generator and a disc absent from the AccurateRip database look like. Both
# packages verified here: with them, ARCUE verifies a real disc under mono.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        flac \
        libjxl-tools \
        libjpeg-turbo-progs \
        libchromaprint-tools \
        rsgain \
        php-cli \
        mono-runtime \
        libgdiplus \
        libmono-system-drawing4.0-cil \
        libsndfile1 \
        libgomp1 \
        libicu76 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The dependency layer comes first and copies ONLY the requirements file: a
# source edit must not invalidate it and re-download every wheel.
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt

COPY --from=web-build /app/web/dist /app/web/dist
COPY server/ /app/server/
COPY mlo/ /app/mlo/
COPY tools/ /app/tools/

# Run unprivileged. uid/gid 1000 is the usual first desktop user, which is what
# a bind-mounted ./music is normally owned by (docker-compose.yml documents the
# alternatives). /app stays writable for config.json; the tools the Dependencies
# page installs live under /music/.mlo/tools now, with the rest of the app's own
# files, so /music is where an install needs to be able to write.
RUN groupadd -g 1000 mlo \
    && useradd -u 1000 -g mlo -m -s /usr/sbin/nologin mlo \
    && mkdir -p /music \
    && chown -R mlo:mlo /app /music

USER mlo
# Docker does not read HOME from passwd; without it numba/librosa caches would
# be written to "/" and fail.
ENV HOME=/home/mlo

# The version of the code this image was built from. The release workflow
# passes the tag (`--build-arg MLO_VERSION=3.1.0`); a plain `docker build`
# leaves it empty, and the server then reports its own code version instead of
# claiming to be a release it is not. `tools/check_versions.py` keeps the
# ARG default in step with mlo/__init__.py.
ARG MLO_VERSION=3.12.0
ENV MLO_VERSION=${MLO_VERSION}
# The commit the image was built from, and when. The release workflow passes
# both; a plain `docker build` leaves them empty and the server then reports
# the code version alone rather than inventing a revision. They exist so the
# running app can answer "which image am I?" beside "which release is newest?"
# — the two facts a user needs to tell a stale image from a broken updater.
ARG MLO_REVISION=""
ARG MLO_BUILT=""
ENV MLO_REVISION=${MLO_REVISION} \
    MLO_BUILT=${MLO_BUILT}
LABEL org.opencontainers.image.version="${MLO_VERSION}" \
      org.opencontainers.image.revision="${MLO_REVISION}" \
      org.opencontainers.image.created="${MLO_BUILT}" \
      org.opencontainers.image.title="la musica" \
      org.opencontainers.image.source="https://github.com/dillydalli3r/la-musica"

# /music: the library, all app state under /music/.mlo (data/, downloads/,
# trash/) AND the tools the Dependencies page installs (/music/.mlo/tools) — one
# volume, so a new image keeps the library and everything installed into it.
# A pre-move install's /app/.dependencies is still READ when it is mounted (see
# mlo.paths.legacy_tools_dir); nothing writes there any more.
VOLUME ["/music"]
EXPOSE 8000

# No curl/wget in the slim image - probe with the Python that is already there.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" 2>/dev/null

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
