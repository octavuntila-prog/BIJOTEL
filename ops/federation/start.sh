#!/bin/bash
# start.sh — launch (or relaunch) the bijotel-federation service.
#
# The three key PEMs are multi-line, so they go through the shell environment
# (docker-compose interpolates ${VAR}) instead of a .env file. Keys live in
# ./keys (mode 0600). Re-run after `docker compose down`, a config change, or
# from the health watchdog. On a plain container/host restart Docker reuses the
# environment it captured at create-time, so this is only needed on (re)create.
#
# Pre-req (once): chown the data dir to the image's fed user (uid 999):
#   mkdir -p data && chown -R 999:999 data
set -euo pipefail
cd "$(dirname "$0")"
export BIJOTEL_FED_PRIVATE_KEY_PEM="$(cat keys/bijotel_private.pem)"
export BIJOTEL_FED_PUBLIC_KEY_PEM="$(cat keys/bijotel_public.pem)"
export BIJOTEL_FED_REKOR_PRIVATE_KEY_PEM="$(cat keys/bijotel_ecdsa_private.pem)"
# Admin bearer for the gated /_internal/build-anchor (audit ISSUE-10); 0600 file.
export BIJOTEL_FED_ADMIN_TOKEN="$(cat /opt/.fed_admin_token)"
docker compose up -d
