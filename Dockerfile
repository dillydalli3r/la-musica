# syntax=docker/dockerfile:1

# ---------- Stage 1: build the React frontend ----------
FROM node:22-alpine AS web-build
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---------- Stage 2: Python backend + system toolchain ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MLO_MUSIC_FOLDER=/music \
    MLO_SERVER_HOST=0.0.0.0

# Core audio/image tools for the optimization pipeline. These are also the
# Linux counterparts of the Windows-only downloads in mlo/fetchdeps.py
# (LINUX_PACKAGES): the in-app installer refuses to fetch .exe assets on Linux
# and points the user at the distro package instead, so everything that has one
# is installed here. oxipng and rsgain are best-effort - they are not in the
# Debian release the base image is based on (bookworm), and the pipeline
# degrades without them, so the image must not fail to build over them
# (README says the same about oxipng).
# libsndfile1 / libgomp1 back the pip-vendored tools Settings -> Dependencies
# installs at runtime (PIP_PACKAGES: librosa imports soundfile -> libsndfile,
# and numba/llvmlite -> libgomp); without them the pip install "succeeds" and
# the import dies.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        flac \
        libjxl-tools \
        libjpeg-turbo-progs \
        libchromaprint-tools \
        libsndfile1 \
        libgomp1 \
        ca-certificates \
    && (apt-get install -y --no-install-recommends oxipng || true) \
    && (apt-get install -y --no-install-recommends rsgain || true) \
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
# alternatives). /app stays writable because Settings -> Dependencies installs
# tools (beets, librosa) into /app/.dependencies at runtime.
RUN groupadd -g 1000 mlo \
    && useradd -u 1000 -g mlo -m -s /usr/sbin/nologin mlo \
    && mkdir -p /music /app/.dependencies \
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
ARG MLO_VERSION=3.1.4
ENV MLO_VERSION=${MLO_VERSION}
LABEL org.opencontainers.image.version="${MLO_VERSION}" \
      org.opencontainers.image.title="la musica" \
      org.opencontainers.image.source="https://github.com/dillydalli3r/la-musica"

# /music: the library (+ all app state under /music/.mlo).
# /app/.dependencies: runtime-fetched tools - a volume so a new image does not
# throw them away.
VOLUME ["/music", "/app/.dependencies"]
EXPOSE 8000

# No curl/wget in the slim image - probe with the Python that is already there.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" 2>/dev/null

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
