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
    MLO_MUSIC_FOLDER=/music

# Core audio/image tools for the optimization pipeline. oxipng is best-effort:
# the app fetches it from Settings → Dependencies at runtime when the distro
# package is missing, so the image must not fail to build over it (README says
# the same).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        flac \
        libjxl-tools \
        libjpeg-progs \
        ca-certificates \
    && (apt-get install -y --no-install-recommends oxipng || true) \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=web-build /app/web/dist /app/web/dist
COPY server/ /app/server/
COPY mlo/ /app/mlo/
COPY tools/ /app/tools/

RUN pip install --no-cache-dir -r /app/server/requirements.txt

VOLUME ["/music"]
EXPOSE 8000

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]