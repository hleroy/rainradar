#!/usr/bin/env bash
# Rain Radar production deploy script. Lives in the repo root and runs in place
# on the host at /var/repos/rainradar/deploy.sh (the webhook invokes it there).
#
# The whole body is wrapped in main(), called on the LAST line, so bash reads and
# parses the entire file before executing anything. This matters because the
# `git checkout` below rewrites the working tree — including this script — and
# bash otherwise reads scripts incrementally by byte offset; reading it fully up
# front makes self-modification safe.
#
# Invoked by the adnanh/webhook daemon with the release tag as $1 (already
# HMAC-verified); re-validates everything and never trusts the payload. All
# output goes to the systemd journal. See specs/cd-webhook-3-host-setup.md.
set -euo pipefail

REPO_DIR="/var/repos/rainradar"
COMPOSE_FILE="docker-compose.production.yml"
# Lock lives inside the checkout the `deploy` user owns — the system user has no
# XDG_RUNTIME_DIR and may not be able to write /run/lock, which under `set -e`
# would abort the deploy before the lock is even held. Untracked, so the
# in-script `git checkout --force` never touches it.
LOCK="${REPO_DIR}/.deploy.lock"

log() { printf '[deploy %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

main() {
  # Serialize deploys — a second webhook waits rather than racing the first.
  exec 9>"$LOCK"
  flock 9

  local tag="${1:-}"
  if ! printf '%s' "$tag" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
    log "ERROR: refusing to deploy invalid tag '$tag'"; exit 1
  fi

  cd "$REPO_DIR"
  log "deploying $tag"

  git fetch --tags --prune origin
  git rev-parse -q --verify "refs/tags/${tag}" >/dev/null \
    || { log "ERROR: tag '$tag' not found after fetch"; exit 1; }
  git checkout --force "tags/${tag}"   # detached HEAD at the immutable tag

  log "building images"
  docker compose -f "$COMPOSE_FILE" build

  log "applying migrations (fail-fast before swapping containers)"
  docker compose -f "$COMPOSE_FILE" run --rm django python manage.py migrate --noinput

  log "recreating stack"
  docker compose -f "$COMPOSE_FILE" up -d --remove-orphans

  # nginx's `upstream django { server django:5000; }` resolves that hostname to
  # an IP once, at nginx's own startup, and never again. `up -d` above recreates
  # django (new container -> usually a new IP on the bridge network) but leaves
  # a not-otherwise-changed nginx running untouched, so it keeps proxying to the
  # now-dead old IP (502s) until something makes it reload. Restarting nginx
  # here forces a fresh config load, and with it a fresh DNS resolution, on
  # every deploy. A plain restart (not --force-recreate) is enough: it reuses
  # the same container/network endpoint and just needs nginx's own process to
  # come back up, so it's fast and doesn't touch postgres/redis.
  log "restarting nginx to refresh its cached upstream IP"
  docker compose -f "$COMPOSE_FILE" restart nginx

  log "pruning dangling images"
  docker image prune -f

  log "deploy of $tag complete"
}

main "$@"
