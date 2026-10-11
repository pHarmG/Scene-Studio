#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -euo pipefail

bashio::log.info "Rendering Scene Studio runtime configuration..."
python3 /opt/scene-studio/render_config.py --data-dir /data --payload-root /opt/scene-studio

bashio::log.info "Starting Scene Studio — Workbench: http://<host>:5050/local/scene_studio/"
exec appdaemon -c /data
