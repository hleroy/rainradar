---
name: deploy
description: Deploy Rain Radar to production, or work on production config. Covers the tag-triggered CD webhook, what "deploy" means as a maintainer instruction (cut a vX.Y.Z version via workflow_dispatch), rollbacks, the manual bring-up/recovery fallback, the production service topology, how to enable the dark feature flags (METEOFRANCE_ENABLED, METEOFRANCE_REFLECTIVITY_ENABLED, PUSH_ALERTS_ENABLED), and how to rotate the Météo-France credential (OAuth2 only — never an API key) and restart the containers that read it.
---

# Deploying Rain Radar

**Deploys are tag-triggered CD, not run by hand.** Three entry points to
`deploy.yml`: (1) push a `vX.Y.Z` tag; (2) **Actions ▸ Deploy ▸ Run workflow** with a
new `version` (+ optional `ref`, default `main`) — the workflow runs the suite, then
creates and pushes the tag itself before deploying (the path that works from Claude
Code on the web, where local tag pushes aren't possible); (3) the same dispatch with
an **existing** `version`, which skips tests and redeploys that tag (rollback).

**When the maintainer says "deploy"** (with no further detail), it means: ask which
`vX.Y.Z` version to cut (showing the latest existing tag and a sensible next bump),
then trigger `deploy.yml` via **workflow_dispatch** on GitHub Actions with that
`version` (+ `ref`, default `main`) — i.e. path (2) above. A new version cuts a fresh
release; an existing version is a rollback. Watch the run to green, but remember the
green `deploy` job only means the webhook was **accepted** — the on-host build/migrate
runs async (`journalctl -u webhook`), so don't claim production is updated from the
Actions result alone.

New tags run the dockerized suite first, then `deploy.yml` POSTs an HMAC-SHA256-signed
webhook that a host daemon verifies before running this repo's `deploy.sh` in place at
`/var/repos/rainradar` — checkout the immutable tag, build images on the host (no
registry), migrate fail-fast, recreate the stack. Only the workflows + `deploy.sh`
live here; the webhook/Traefik/host wiring is host infra (Ansible). Migrations must
be **backward-compatible (expand/contract)**. The manual command below is the
bring-up/recovery fallback.

## Topology and manual bring-up

`docker compose -f docker-compose.production.yml up -d --build`. Services: **django**
(uvicorn via gunicorn worker), **archiver** (single replica, internal network only,
no Traefik labels — never scaled), **postgres**, **redis**, **nginx**. TLS/routing is
handled by an **existing host Traefik** (not bundled); `nginx` joins the external
`net` network and carries Traefik labels for `rainradar.hleroy.com`, and mounts the
tile volume `production_radar_tiles:/data:ro` to serve `/tiles` statically (Django
fallback for misses). `django` + `archiver` mount that volume **rw**. The `archiver`
sets `ARCHIVER_ENABLED=true` + `LIGHTNING_ENABLED=true` and needs **outbound** HTTPS
to RainViewer and WSS to Blitzortung (no inbound exposure). Before deploying: create
the `net` network, set strong values in `.envs/.production/.django`
(`DJANGO_SECRET_KEY`, `DJANGO_ADMIN_URL`, `DATABASE_URL`), provision the tile volume,
and ensure the Traefik cert resolver named `letsencrypt` exists on the host.
`collectstatic` runs at image build time so the shared static volume is seeded by
django (not nginx's default page).

## Rotating the Météo-France credential

**On the portal, generate an OAuth2 credential — not an API key.** The app supports
*only* the OAuth2 client-credentials flow: `radar/providers/meteofrance_auth.py` POSTs
`grant_type=client_credentials` to `https://portail-api.meteofrance.fr/token` with
`Authorization: Basic {METEOFRANCE_APPLICATION_ID}` and uses the short-lived bearer
token it gets back (refreshed 60 s before expiry). Nothing in `radar/` ever sends the
`apikey` header a portal API key would need, so an API key silently buys you a
credential the app cannot use. (`scripts/meteofrance_api_check.py` accepts an API key,
but that is a host-run diagnostic probe, not the app.)

`METEOFRANCE_APPLICATION_ID` is **not** a token. It is
`base64(consumer_key:consumer_secret)` — the blob shown in the *Générer token* curl
example on the application's portal page. Don't paste the ~1 h JWT that the button
itself mints; the app mints those. Rotating means **regenerating the consumer secret**
on the application, which changes the blob. Either copy it from the refreshed curl
example, or recompute it:

```bash
printf '%s:%s' "$CONSUMER_KEY" "$CONSUMER_SECRET" | base64 -w0
```

Then, on the host — the live value lives in `/var/repos/rainradar/.envs/.production/.django`
(gitignored; the repo copy is not what production reads):

```bash
# as the deploy user, on the host
$EDITOR /var/repos/rainradar/.envs/.production/.django   # set METEOFRANCE_APPLICATION_ID=
cd /var/repos/rainradar
docker compose -f docker-compose.production.yml up -d --force-recreate django archiver
```

**Both** `django` and `archiver` read the credential, so both must be recreated —
`restart` alone does **not** re-read the env file, and recreating only `django` leaves
the archiver (the container that actually polls Météo-France) on the dead credential.
This touches no image and no migration, so it is a container recreate, not a deploy —
don't cut a tag for it.

`config/settings/production.py` sets `METEOFRANCE_ENABLED = True`, and
`require_meteofrance_credentials` fails fast at startup when the variable is missing —
so a blank or malformed paste means the container **refuses to boot** rather than
degrading quietly. Verify after the recreate:

```bash
docker compose -f docker-compose.production.yml ps          # both Up, no restart loop
docker compose -f docker-compose.production.yml logs --tail=50 archiver
```

A bad *value* (well-formed but wrong) boots fine and fails later at the token
endpoint: look for `Météo-France token endpoint -> HTTP 401` in the archiver logs, and
for Météo-France frames advancing on the site. **Never** echo the variable, the token,
or the logs' surrounding secrets into a shell transcript or an issue.
