#!/usr/bin/env bash
#
# Generate strong random values for the production secrets in
# .envs/.production/.django and .envs/.production/.postgres.
#
# These files are gitignored — they live only on your machine / the server and
# are never committed. Run this once before the first production deploy, or to
# rotate secrets.
#
# This script does NOT create the env files — they must already exist (it only
# replaces or appends the individual keys below). .envs/.production/.django holds
# ONLY secrets and deployment wiring: DJANGO_SECRET_KEY, DJANGO_ADMIN_URL,
# DJANGO_ALLOWED_HOSTS, WEB_CONCURRENCY, and the commented-out feature secrets.
# Everything else is a settings constant in config/settings/, and the two
# per-container role flags (ARCHIVER_ENABLED / LIGHTNING_ENABLED) are owned by the
# compose `environment:` blocks — do NOT add them here.
#
# Usage:
#   ./scripts/gen-production-credentials.sh          # rotate DJANGO secrets + DB password
#   ./scripts/gen-production-credentials.sh --user   # also regenerate POSTGRES_USER/DB
#   ./scripts/gen-production-credentials.sh --show   # print the generated values
#
# By default POSTGRES_USER and POSTGRES_DB are left untouched, because changing
# them on an existing Postgres volume breaks authentication. Use --user only on a
# fresh deploy (no postgres volume yet).

set -o errexit
set -o nounset
set -o pipefail

# Resolve repo root from this script's location, so it works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DJANGO_ENV="${ROOT}/.envs/.production/.django"
POSTGRES_ENV="${ROOT}/.envs/.production/.postgres"

REGEN_USER=false
SHOW=false
for arg in "$@"; do
  case "$arg" in
    --user) REGEN_USER=true ;;
    --show) SHOW=true ;;
    -h | --help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# --- random generator: URL-safe (A-Za-z0-9_-), safe in dotenv AND in the
#     DATABASE_URL userinfo (no @ : / # that would break URL parsing) ---------
gen() {
  local nbytes="$1"
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "import secrets,sys;print(secrets.token_urlsafe(int(sys.argv[1])))" "$nbytes"
  elif command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 "$((nbytes * 2))" | tr -dc 'A-Za-z0-9_-' | head -c "$((nbytes + nbytes / 3))"
  else
    echo "ERROR: need python3 or openssl to generate secrets." >&2
    exit 1
  fi
}

# --- set KEY=VALUE in a file (replace existing line, else append) ------------
set_var() {
  local file="$1" key="$2" val="$3"
  if [ ! -f "$file" ]; then
    echo "ERROR: missing env file: $file" >&2
    exit 1
  fi
  if grep -qE "^${key}=" "$file"; then
    awk -v k="$key" -v v="$val" '
      BEGIN { FS = OFS = "=" }
      $1 == k { print k "=" v; next }
      { print }
    ' "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$val" >> "$file"
  fi
}

mask() { local s="$1"; printf '%s…%s (%d chars)\n' "${s:0:4}" "${s: -4}" "${#s}"; }

echo "Generating production secrets…"
echo "  django:   $DJANGO_ENV"
echo "  postgres: $POSTGRES_ENV"
echo

SECRET_KEY="$(gen 48)"          # ~64 url-safe chars, well over Django's 50 min
ADMIN_URL="$(gen 16)/"          # random, hard-to-guess admin path (trailing /)
PG_PASSWORD="$(gen 32)"         # url-safe -> safe inside DATABASE_URL

set_var "$DJANGO_ENV" DJANGO_SECRET_KEY "$SECRET_KEY"
set_var "$DJANGO_ENV" DJANGO_ADMIN_URL "$ADMIN_URL"
set_var "$POSTGRES_ENV" POSTGRES_PASSWORD "$PG_PASSWORD"

echo "Updated:"
echo "  DJANGO_SECRET_KEY   -> $(mask "$SECRET_KEY")"
echo "  DJANGO_ADMIN_URL    -> ${ADMIN_URL}"
echo "  POSTGRES_PASSWORD   -> $(mask "$PG_PASSWORD")"

if [ "$REGEN_USER" = true ]; then
  PG_USER="$(gen 16)"
  set_var "$POSTGRES_ENV" POSTGRES_USER "$PG_USER"
  echo "  POSTGRES_USER       -> ${PG_USER}"
  echo
  echo "NOTE: POSTGRES_USER changed — only valid on a FRESH deploy. If a postgres"
  echo "      volume already exists, the new user won't match and auth will fail."
fi

if [ "$SHOW" = true ]; then
  echo
  echo "Full values:"
  echo "  DJANGO_SECRET_KEY=${SECRET_KEY}"
  echo "  DJANGO_ADMIN_URL=${ADMIN_URL}"
  echo "  POSTGRES_PASSWORD=${PG_PASSWORD}"
  [ "$REGEN_USER" = true ] && echo "  POSTGRES_USER=${PG_USER:-}"
fi

echo
echo "Done. These files are gitignored — keep a secure backup of them."
