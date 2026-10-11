# Scene Studio Home Assistant add-on image.
# Supervisor builds this ON THE USER'S MACHINE with the repository root as the
# build context, so the product payload is copied straight from the checkout —
# no registry, no prebuilt images. Build context is narrowed by .dockerignore.

# Stage 1 — build the Workbench SPA. vite.config.js stamps build identity by
# running scripts/version.py, which needs a Python interpreter and accepts the
# "addon" build channel (version-verified, git-free).
FROM node:22-alpine AS workbench
RUN apk add --no-cache python3
WORKDIR /build/workbench
# scripts/version.py (run by vite.config.js for build identity) needs the
# canonical version declarations at their checkout-relative locations.
COPY VERSION /build/VERSION
COPY backend/pyproject.toml /build/backend/pyproject.toml
COPY backend/src/scene_studio /build/backend/src/scene_studio
COPY scripts/version.py /build/scripts/version.py
COPY workbench/package.json workbench/package-lock.json ./
RUN npm ci
COPY workbench/ ./
ARG BUILD_VERSION
ENV SCENE_STUDIO_BUILD_CHANNEL=addon \
    SCENE_STUDIO_RELEASE_TAG=v${BUILD_VERSION} \
    PYTHON=python3
RUN npm run build

# Runtime base images are specified directly (build.yaml is obsolete since
# Home Assistant's BuildKit migration): BuildKit's TARGETARCH selects the
# staged base image matching the target architecture. Supported: amd64,
# arm64 (HA aarch64).
FROM ghcr.io/home-assistant/amd64-base-python:3.11-alpine3.18 AS base-amd64
FROM ghcr.io/home-assistant/aarch64-base-python:3.11-alpine3.18 AS base-arm64
FROM base-${TARGETARCH} AS runtime

# Stage 2 — runtime: AppDaemon hosts the Scene Studio backend (an AppDaemon
# app) and serves the Workbench at /local/scene_studio/ on the same origin.
ENV LANG=C.UTF-8 \
    SCENE_STUDIO_ADDON=1
# The backend imports appdaemon and requests lazily; appdaemon's own
# dependency set provides the pinned requests version it requires. 4.5.x is
# required for AppDaemon's package-module app loading (the backend lives at
# apps/scene_studio/... and is referenced as a dotted module) — 4.4.x matches
# apps to files by module basename only and silently skips dotted modules.
# The C toolchain covers any dependency without a musllinux wheel and is
# removed again in the same layer.
RUN apk add --no-cache build-base \
 && pip3 install --no-cache-dir "appdaemon~=4.5.13" \
 && apk del build-base \
 && rm -rf /var/cache/apk/*
COPY --from=workbench /build/workbench/dist /opt/scene-studio/www/scene_studio
COPY backend/src/scene_studio /opt/scene-studio/apps/scene_studio
COPY run.sh render_config.py /opt/scene-studio/
ARG BUILD_VERSION
# The add-on image carries its own frozen build identity: version-verified
# against the add-on version, git-free (channel "addon", no source digest).
RUN VERSION="${BUILD_VERSION}" python3 -c "import json, os, datetime; print(json.dumps({'version': os.environ['VERSION'], 'update_protocol': 1, 'source_sha': None, 'short_sha': None, 'source_tree_sha256': None, 'channel': 'addon', 'dirty': False, 'tag': None, 'built_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}, indent=2))" > /opt/scene-studio/apps/scene_studio/build-info.json \
 && chmod a+x /opt/scene-studio/run.sh /opt/scene-studio/render_config.py
LABEL \
  io.hass.name="Scene Studio" \
  io.hass.description="Lighting scenes for Home Assistant" \
  io.hass.type="addon" \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.arch="amd64|aarch64"
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5050/api/appdaemon/scene_studio_api', timeout=8)" || exit 1
CMD ["/opt/scene-studio/run.sh"]
