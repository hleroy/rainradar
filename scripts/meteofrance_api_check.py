#!/usr/bin/env python3
# ruff: noqa: S105
# One-off diagnostic tool: prints freely, opens https URLs, tolerates complexity.
"""One-off probe of the Météo-France "Données Radar" API (DPRadar v1).

Purpose: resolve the open unknowns before implementing the provider — catalog
shape, valid `maille` values, the
actual mosaic file format (HDF5 vs gzipped BUFR), projection metadata, and
rate-limit behavior. This is a host-run network probe, NOT part of the test
suite (the dockerized suite stays offline).

Auth (pick one, via env — first match wins). NB: these four are what this *probe*
accepts, deliberately, to compare them. The app itself supports **only** OAuth2
client-credentials via METEOFRANCE_APPLICATION_ID — `radar/providers/` never sends
an `apikey` header — so do not take this list as a menu when provisioning or
rotating the production credential. See the `deploy` skill, "Rotating the
Météo-France credential".
  METEOFRANCE_CLIENT_ID + METEOFRANCE_CLIENT_SECRET   OAuth2 consumer key/secret
  METEOFRANCE_APPLICATION_ID                          the base64 blob shown in the
                                                      portal's "Générer token"
                                                      curl example (it IS
                                                      base64(key:secret)) — this is
                                                      the one the app uses
  METEOFRANCE_TOKEN                                   a token minted by the
                                                      portal's "Générer token"
                                                      button (JWT, ~1 h)
  METEOFRANCE_API_KEY                                 portal-generated API key
                                                      (probe-only — the app cannot
                                                      use this)

Usage:
  METEOFRANCE_TOKEN=eyJ... python3 scripts/meteofrance_api_check.py

Optional env:
  METEOFRANCE_ZONE        zone to walk (default METROPOLE)
  METEOFRANCE_PROBE_OUT   output dir (default ./_meteofrance_probe)

Stdlib-only. If `h5py` happens to be importable, HDF5 downloads get a full
structure dump (projection, grid shape, timestamps); otherwise you still get
magic-byte identification and the saved file to inspect later.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

API_BASE = "https://public-api.meteofrance.fr/public/DPRadar/v1"
TOKEN_URL = "https://portail-api.meteofrance.fr/token"
ZONE = os.environ.get("METEOFRANCE_ZONE", "METROPOLE")
OUT_DIR = Path(os.environ.get("METEOFRANCE_PROBE_OUT", "_meteofrance_probe"))
TIMEOUT = 60
MAILLE_CANDIDATES = (500, 1000)  # tried only if the catalog links don't say
MAX_PRODUCT_DOWNLOADS = 3  # stay polite

INTERESTING_HEADERS = (
    "content-type",
    "content-disposition",
    "content-length",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "retry-after",
)


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n== {title}\n{'=' * 78}")


def show_jwt_claims(token: str) -> None:
    """Best-effort unverified decode of a JWT payload — reveals iat/exp (TTL)."""
    parts = token.split(".")
    if len(parts) != 3:
        return
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad))
    except ValueError, UnicodeDecodeError:
        return
    print(f"  JWT claims: {json.dumps(claims, indent=2)[:1500]}")
    iat, exp = claims.get("iat"), claims.get("exp")
    if isinstance(iat, int) and isinstance(exp, int):
        print(
            f"  -> token lifetime: {exp - iat} s "
            f"({time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(exp))} expiry, UTC)"
        )


def get_auth_header() -> dict[str, str]:
    client_id = os.environ.get("METEOFRANCE_CLIENT_ID")
    client_secret = os.environ.get("METEOFRANCE_CLIENT_SECRET")
    application_id = os.environ.get("METEOFRANCE_APPLICATION_ID")
    token = os.environ.get("METEOFRANCE_TOKEN")
    api_key = os.environ.get("METEOFRANCE_API_KEY")

    if client_id and client_secret:
        application_id = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    if application_id:
        section("Auth: OAuth2 client-credentials")
        req = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            headers={
                "Authorization": f"Basic {application_id}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read())
        redacted = {
            k: (v if k not in ("access_token", "refresh_token") else f"<{len(str(v))} chars>")
            for k, v in payload.items()
        }
        print(f"token endpoint OK: {json.dumps(redacted, indent=2)}")
        print(f"-> token TTL (expires_in): {payload.get('expires_in')!r} seconds")
        show_jwt_claims(payload["access_token"])
        return {"Authorization": f"Bearer {payload['access_token']}"}

    if token:
        section("Auth: pre-minted OAuth2 token (Bearer)")
        show_jwt_claims(token)
        return {"Authorization": f"Bearer {token}"}

    if api_key:
        section("Auth: portal API key (apikey header)")
        show_jwt_claims(api_key)
        return {"apikey": api_key}

    sys.exit(
        "Set one of: METEOFRANCE_TOKEN (portal 'Générer token' JWT), "
        "METEOFRANCE_CLIENT_ID+METEOFRANCE_CLIENT_SECRET, "
        "METEOFRANCE_APPLICATION_ID, or METEOFRANCE_API_KEY."
    )


def fetch(url: str, auth: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    """GET url; return (status, lowercased headers, body) — errors included."""
    req = urllib.request.Request(url, headers={**auth, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def show_response(status: int, headers: dict[str, str], body: bytes) -> None:
    print(f"HTTP {status}")
    for h in INTERESTING_HEADERS:
        if h in headers:
            print(f"  {h}: {headers[h]}")
    if status == 429:
        try:
            print(f"  429 body: {json.dumps(json.loads(body), indent=2)}")
        except ValueError, UnicodeDecodeError:
            print(f"  429 body: {body[:500]!r}")
    elif status == 401:
        print("  401 — bad/expired credentials. Check the env vars (and, for an")
        print("  API key, that it was generated for THIS API and hasn't expired).")


def get_json(url: str, auth: dict[str, str], save_as: str) -> dict | None:
    print(f"\nGET {url}")
    status, headers, body = fetch(url, auth)
    show_response(status, headers, body)
    if status != 200:
        print(f"  body: {body[:500]!r}")
        return None
    try:
        doc = json.loads(body)
    except ValueError:
        print(f"  NOT JSON despite 200 — first bytes: {body[:200]!r}")
        return None
    path = OUT_DIR / save_as
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    print(f"  saved -> {path}")
    print(json.dumps(doc, indent=2, ensure_ascii=False)[:3000])
    return doc


def links_of(doc: dict | None) -> list[dict]:
    return doc.get("links", []) if isinstance(doc, dict) else []


def sniff(body: bytes) -> str:
    """Identify a payload by magic bytes; recurse into gzip/zip wrappers."""
    if body.startswith(b"\x89HDF\r\n\x1a\n"):
        return "HDF5"
    if body.startswith(b"BUFR"):
        return "BUFR (raw)"
    if body.startswith(b"\x1f\x8b"):
        try:
            inner = gzip.decompress(body)
        except OSError as e:
            return f"gzip (decompress failed: {e})"
        return f"gzip[{sniff(inner)}] ({len(inner)} bytes inner)"
    if body.startswith(b"PK\x03\x04"):
        try:
            names = zipfile.ZipFile(io.BytesIO(body)).namelist()
        except zipfile.BadZipFile:
            names = ["<unreadable>"]
        return f"zip{names}"
    if body[:1] in (b"{", b"["):
        return f"JSON: {body[:300]!r}"
    return f"unknown (first 16 bytes: {body[:16].hex()})"


def dump_hdf5(path: Path) -> None:
    try:
        import h5py
    except ImportError:
        print(
            "  (h5py not installed — skipping structure dump; "
            "`pip install h5py` and rerun for projection/grid details)"
        )
        return

    def visit(name: str, obj: object) -> None:
        kind = "DS " if hasattr(obj, "shape") else "GRP"
        extra = f" shape={obj.shape} dtype={obj.dtype}" if hasattr(obj, "shape") else ""
        print(f"    [{kind}] /{name}{extra}")
        for k, v in getattr(obj, "attrs", {}).items():
            val = v.decode(errors="replace") if isinstance(v, bytes) else v
            print(f"           @{k} = {val!r}")

    with h5py.File(path, "r") as f:
        print("  HDF5 structure (ODIM-style /what /where /how expected):")
        for k, v in f.attrs.items():
            print(f"    @{k} = {v!r}")
        f.visititems(visit)


def try_product(url: str, auth: dict[str, str], tag: str) -> bool:
    print(f"\nGET {url}")
    status, headers, body = fetch(url, auth)
    show_response(status, headers, body)
    if status != 200:
        print(f"  body: {body[:500]!r}")
        return False
    kind = sniff(body)
    print(f"  {len(body)} bytes, detected format: {kind}")
    path = OUT_DIR / f"produit_{tag}.bin"
    path.write_bytes(body)
    print(f"  saved -> {path}")
    if kind == "HDF5":
        dump_hdf5(path)
    elif kind.startswith("gzip[HDF5]"):
        inner = OUT_DIR / f"produit_{tag}.h5"
        inner.write_bytes(gzip.decompress(body))
        print(f"  decompressed -> {inner}")
        dump_hdf5(inner)
    return True


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    auth = get_auth_header()

    section("1. Catalog: GET /mosaiques (available zones)")
    get_json(f"{API_BASE}/mosaiques", auth, "01_mosaiques.json")

    section(f"2. Zone: GET /mosaiques/{ZONE}")
    get_json(f"{API_BASE}/mosaiques/{ZONE}", auth, "02_zone.json")

    section(f"3. Observations: GET /mosaiques/{ZONE}/observations")
    obs_doc = get_json(f"{API_BASE}/mosaiques/{ZONE}/observations", auth, "03_observations.json")

    # Observation names: parse them out of the observation-list links.
    obs_names: list[str] = []
    for link in links_of(obs_doc):
        href = link.get("href", "")
        marker = "/observations/"
        if marker in href:
            tail = href.split(marker, 1)[1]
            name = urllib.parse.unquote(tail.split("/")[0].split("?")[0])
            if name and name not in obs_names:
                obs_names.append(name)
    print(f"\nobservation names discovered: {obs_names or 'NONE — check links above'}")

    product_urls: list[str] = []  # (absolute) produit hrefs from descriptions
    for i, obs in enumerate(obs_names):
        section(f"4.{i + 1} Observation description: {obs}")
        desc = get_json(
            f"{API_BASE}/mosaiques/{ZONE}/observations/{urllib.parse.quote(obs)}",
            auth,
            f"04_obs_{i + 1}_{obs.replace('/', '_')}.json",
        )
        for link in links_of(desc):
            href = link.get("href", "")
            if "/produit" in href:
                product_urls.append(href if href.startswith("http") else API_BASE + href)

    section("5. Product download (the actual mosaic grid)")
    downloaded = 0
    for url in product_urls:
        if downloaded >= MAX_PRODUCT_DOWNLOADS:
            break
        if try_product(url, auth, f"link{downloaded + 1}"):
            downloaded += 1
    if downloaded == 0:
        print("\nNo usable produit links in the catalog — probing maille candidates.")
        for obs in obs_names or ["LAME_DEAU"]:
            for maille in MAILLE_CANDIDATES:
                if downloaded >= MAX_PRODUCT_DOWNLOADS:
                    break
                url = (
                    f"{API_BASE}/mosaiques/{ZONE}/observations/"
                    f"{urllib.parse.quote(obs)}/produit?maille={maille}"
                )
                if try_product(url, auth, f"{obs.replace('/', '_')}_{maille}m"):
                    downloaded += 1

    section("Summary")
    print(f"""
Saved artifacts in {OUT_DIR}/. Read off:
  1. grid format      -> 'detected format' lines above (HDF5 vs gzip[BUFR])
  2. projection       -> HDF5 dump: /where @projdef (+ corner lat/lon attrs)
  3. OAuth2 details   -> token TTL printed at the top (if OAuth2 mode)
  4. catalog shape    -> 01/02/03_*.json: are PAST runs listed, or latest-only?
  5. colormap/legend  -> any legend/style links in the catalogs? (grep the JSON)
  6. rate limiting    -> any x-ratelimit/429 output above
  7. zone list        -> 01_mosaiques.json (overseas zones present?)
Also note each product's timestamp (Content-Disposition filename and/or HDF5
/what @date @time) — two runs ~15 min apart confirm the mosaic cadence.
{"attribution field: check the catalog JSONs for the exact required credit."}
""")


if __name__ == "__main__":
    main()
