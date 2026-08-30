"""Headless frontend checks over the shipped frontend files.

No browser automation (per the project's no-playwright-for-ui-checks rule) — just
static assertions: the About dialog markup + activator exist, the about.* i18n keys
have FR/EN parity, and about.js is wired into main.js; plus the clip button/markup,
clip.* FR/EN parity, mediabunny vendoring, the service-worker shell contract, the
alert UI, the radar-source switch, and the additive radar/lightning export accessors.

The last section covers crawlability: production serves every HTML document as a
plain static file with no templating layer, so the canonical origin is duplicated
across three documents plus robots.txt and sitemap.xml, and only a test can keep
them agreeing.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import pytest
from django.conf import settings

FRONTEND = settings.FRONTEND_DIR


def _read(*parts) -> str:
    return FRONTEND.joinpath(*parts).read_text(encoding="utf-8")


def test_index_html_has_dialog_markup():
    html = _read("index.html")
    assert 'id="about-trigger"' in html
    assert 'aria-haspopup="dialog"' in html
    assert 'id="about-overlay"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'id="about-stats-list"' in html


def test_about_i18n_keys_have_fr_en_parity():
    en = json.loads(_read("i18n", "en.json"))
    fr = json.loads(_read("i18n", "fr.json"))
    en_about = {k for k in en if k.startswith("about.")}
    fr_about = {k for k in fr if k.startswith("about.")}
    assert en_about, "no about.* keys found in en.json"
    assert en_about == fr_about, f"FR/EN about.* mismatch: {en_about ^ fr_about}"
    # The intro renderer walks about.intro.p1, p2, … and stops at the first gap,
    # so p1 must exist or the prose renders empty and the byline is dropped.
    assert "about.intro.p1" in en_about
    # The byline under the intro carries both: author, then the source link.
    assert "about.credit_name" in en_about
    assert "about.source" in en_about


def test_about_js_is_wired_into_main():
    main = _read("js", "main.js")
    assert 'from "./about.js"' in main
    assert "initAbout(" in main
    assert "about.refreshI18n()" in main
    # The controller module exists and exports the expected entry point.
    assert "export function initAbout(" in _read("js", "about.js")


# -- client-side video export -----------------------------------------------


def test_index_html_has_clip_button():
    html = _read("index.html")
    assert 'id="clip-btn"' in html
    assert 'data-i18n-title="clip.export"' in html
    # The render-progress element used by clip.js.
    assert 'id="clip-progress"' in html


def test_clip_i18n_keys_have_fr_en_parity():
    en = json.loads(_read("i18n", "en.json"))
    fr = json.loads(_read("i18n", "fr.json"))
    en_clip = {k for k in en if k.startswith("clip.")}
    fr_clip = {k for k in fr if k.startswith("clip.")}
    assert en_clip, "no clip.* keys found in en.json"
    assert en_clip == fr_clip, f"FR/EN clip.* mismatch: {en_clip ^ fr_clip}"
    # Keys the controller looks up by name.
    assert {
        "clip.export",
        "clip.rendering",
        "clip.unsupported",
        "clip.failed",
        "clip.nodata",
        "clip.osm_credit",
    } <= en_clip


def test_clip_js_is_wired_into_main():
    main = _read("js", "main.js")
    assert 'from "./clip.js"' in main
    assert "initClip(" in main
    # The OSM base layer must be CORS-clean for the capture to read its pixels.
    assert "crossOrigin" in main


def test_clip_js_exists_and_vendors_mediabunny():
    clip = _read("js", "clip.js")
    assert "export function initClip(" in clip
    # Imports the vendored mediabunny ESM (no build step / no bundler).
    assert "vendor/mediabunny/mediabunny.js" in clip
    # The vendored library + its MPL-2.0 license ship in the repo.
    assert FRONTEND.joinpath("vendor", "mediabunny", "mediabunny.js").exists()
    assert FRONTEND.joinpath("vendor", "mediabunny", "LICENSE").exists()


def test_radar_and_lightning_expose_export_accessors():
    assert "getExportData" in _read("js", "radar.js")
    assert "getExportLayer" in _read("js", "lightning.js")


def test_lightning_canvas_rides_the_zoom_animation():
    """The strike canvas must scale with the map mid-zoom, like the tile layers.

    Leaflet CSS-transforms every layer that opts in, over the two events that cover
    both gestures: `zoomanim` (the 250 ms wheel/double-click animation) and `zoom`
    (a pinch, which never animates). A layer wired only to `zoomend` — as this one
    once was — freezes for the whole gesture while OSM and the radar tiles scale,
    then jumps into place at the end.
    """
    js = _read("js", "lightning.js")
    assert 'map.on("zoomanim", this._onAnimZoom, this)' in js
    assert 'map.on("zoom", this._onZoom, this)' in js
    # transform-origin: 0 0, without which the scale is anchored at the centre.
    assert '"leaflet-zoom-animated"' in js
    assert "L.DomUtil.setTransform(this._canvas, offset, scale)" in js
    # Both handlers must come off with the layer.
    assert 'map.off("zoomanim", this._onAnimZoom, this)' in js
    assert 'map.off("zoom", this._onZoom, this)' in js
    # The transform is sized against the view the pixels were drawn for, never the
    # map's live zoom — which has already moved on by the time these fire.
    assert "map.getZoomScale(zoom, this._drawZoom)" in js
    assert "map._latLngToNewLayerPoint(this._drawTopLeft, zoom, center)" in js


# -- installable PWA + offline app shell -------------------------------------


@pytest.mark.django_db
def test_service_worker_route_is_root_scoped(client):
    """The dev /sw.js route serves the worker at root scope with no-cache."""
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/javascript")
    assert resp["Cache-Control"] == "no-cache"
    assert resp["Service-Worker-Allowed"] == "/"


def test_manifest_is_valid():
    manifest = json.loads(_read("manifest.webmanifest"))
    assert manifest["name"]
    assert manifest["short_name"]
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"]
    assert manifest["id"] == "/"
    assert "weather" in manifest["categories"]
    # lang says fr, so the description a store/install prompt shows must be French.
    assert manifest["lang"] == "fr"
    assert "Radar de pluie" in manifest["description"]
    sizes = {icon.get("sizes") for icon in manifest["icons"]}
    assert "192x192" in sizes
    assert "512x512" in sizes
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])


def test_index_html_links_pwa_head():
    html = _read("index.html")
    assert 'rel="manifest"' in html
    assert "/static/manifest.webmanifest" in html
    assert '<meta name="theme-color"' in html
    assert 'rel="apple-touch-icon"' in html


def test_main_js_registers_service_worker():
    main = _read("js", "main.js")
    assert 'navigator.serviceWorker.register("/sw.js"' in main
    # The deferred-reload guard hangs off controllerchange.
    assert "controllerchange" in main
    assert "isExporting" in main


def test_sw_js_integrity():
    sw = _read("sw.js")
    # Vanilla classic worker: no ES imports.
    assert "import " not in sw
    # Versioned, prunable shell cache.
    assert "CACHE_VERSION" in sw
    assert "rainradar-shell-" in sw
    # Data paths are guarded out of the fetch handler.
    assert '"/api/"' in sw
    assert '"/tiles/"' in sw

    # Every STATIC_SHELL entry (except "/") maps to a real file under frontend/.
    array = re.search(r"const STATIC_SHELL = \[(.*?)\];", sw, re.DOTALL)
    assert array, "STATIC_SHELL array not found in sw.js"
    paths = re.findall(r'"([^"]+)"', array.group(1))
    assert "/" in paths
    for path in paths:
        if path == "/":
            continue
        assert path.startswith("/static/"), f"unexpected non-static shell path: {path}"
        rel = path[len("/static/") :]
        assert FRONTEND.joinpath(*rel.split("/")).exists(), f"missing shell file: {path}"


def test_pwa_icons_exist_and_non_empty():
    for name in (
        "icon-192.png",
        "icon-512.png",
        "icon-maskable-512.png",
        "apple-touch-icon-180.png",
    ):
        icon = FRONTEND.joinpath("icons", name)
        assert icon.exists(), f"missing icon: {name}"
        assert icon.stat().st_size > 0, f"empty icon: {name}"


def test_clip_js_exposes_is_exporting():
    assert "isExporting" in _read("js", "clip.js")


# -- foreground storm proximity alerts ---------------------------------------


def test_index_html_has_alert_markup():
    html = _read("index.html")
    # The bell button is hidden until the backend advertises lightning.
    assert 'id="alert-btn"' in html
    assert 'data-i18n-title="control.alerts"' in html
    assert re.search(r'id="alert-btn"[^>]*\bhidden\b', html, re.DOTALL), (
        "alert-btn must ship hidden"
    )
    # The anchor sheet (modal) + placement chrome.
    assert 'id="alert-overlay"' in html
    assert 'id="alert-dialog"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'id="alert-placebar"' in html
    assert 'id="alert-crosshair"' in html


def test_alert_i18n_keys_have_fr_en_parity():
    en = json.loads(_read("i18n", "en.json"))
    fr = json.loads(_read("i18n", "fr.json"))
    en_alert = {k for k in en if k.startswith("alert.")}
    fr_alert = {k for k in fr if k.startswith("alert.")}
    assert en_alert, "no alert.* keys found in en.json"
    assert en_alert == fr_alert, f"FR/EN alert.* mismatch: {en_alert ^ fr_alert}"
    # The control label lives outside the alert.* namespace but is required too.
    assert "control.alerts" in en
    assert "control.alerts" in fr
    # All eight compass directions are used by the notification body.
    dirs = {f"alert.dir.{d}" for d in ("n", "ne", "e", "se", "s", "sw", "w", "nw")}
    assert dirs <= en_alert
    # Keys the controller looks up by name.
    assert {"alert.notify.outer.title", "alert.notify.inner.title", "alert.notify.body"} <= en_alert


def test_alerts_js_exists_with_pinned_constants():
    alerts = _read("js", "alerts.js")
    assert "export function initAlerts(" in alerts
    # The tier/re-arm/freshness constants are pinned by the spec.
    assert "TIER_OUTER_KM = 30" in alerts
    assert "TIER_INNER_KM = 10" in alerts
    assert "REARM_S = 1800" in alerts
    assert "FRESH_S = 600" in alerts


def test_alerts_js_is_wired_into_main():
    main = _read("js", "main.js")
    assert 'from "./alerts.js"' in main
    assert "initAlerts(" in main
    assert "alerts.refreshI18n()" in main


def test_geo_js_exposes_anchor_accessors():
    geo = _read("js", "geo.js")
    assert "getLastFix" in geo
    assert "requestFix" in geo


def test_alerts_js_contacts_no_upstream():
    """Strikes reach the browser only via our own SSE endpoint — never a third party."""
    alerts = _read("js", "alerts.js")
    assert "/api/lightning/stream" in alerts
    assert "rainviewer" not in alerts.lower()
    assert "blitzortung" not in alerts.lower()
    assert "tilecache" not in alerts.lower()


def test_sw_js_has_notificationclick():
    sw = _read("sw.js")
    assert 'addEventListener("notificationclick"' in sw
    # The alerts module joined the precached shell, and the cache version was bumped.
    assert '"/static/js/alerts.js"' in sw
    assert 'CACHE_VERSION = "v5"' not in sw


# -- background storm alerts (Web Push) --------------------------------------


def test_sw_js_has_push_handler():
    sw = _read("sw.js")
    # The push handler ships alongside the (kept) notificationclick handler.
    assert 'addEventListener("push"' in sw
    assert 'addEventListener("notificationclick"' in sw
    # The shell changed again, so the cache version moved off the foreground-only value.
    assert 'CACHE_VERSION = "v6"' not in sw


def test_alerts_js_push_lifecycle_same_origin_only():
    alerts = _read("js", "alerts.js")
    # Registers a Web Push subscription with the userVisibleOnly contract…
    assert "pushManager" in alerts
    assert "userVisibleOnly" in alerts
    # …and talks to our own subscribe/unsubscribe endpoints only.
    assert "/api/alerts/subscribe" in alerts
    assert "/api/alerts/unsubscribe" in alerts
    # Still no third-party origin — the invariant holds for the background path too.
    assert "rainviewer" not in alerts.lower()
    assert "blitzortung" not in alerts.lower()
    assert "googleapis" not in alerts.lower()  # push endpoints come from the browser, not us


def test_alert_push_i18n_keys_present_both_locales():
    en = json.loads(_read("i18n", "en.json"))
    fr = json.loads(_read("i18n", "fr.json"))
    keys = {
        "alert.sheet.push_active",
        "alert.sheet.foreground_only",
        "alert.sheet.ios_install",
        "alert.sheet.privacy",
    }
    assert keys <= set(en)
    assert keys <= set(fr)


def test_index_html_has_push_status_markup():
    html = _read("index.html")
    assert 'id="alert-push-status"' in html
    assert 'id="alert-privacy"' in html
    assert 'id="alert-ios-hint"' in html


# -- Display settings: rain opacity + warning-ring visibility -----------------


def test_index_html_has_settings_markup():
    html = _read("index.html")
    assert 'id="settings-btn"' in html
    assert 'id="setting-opacity"' in html
    assert 'id="setting-rings"' in html
    # Non-modal popover: ships hidden, toggled by the gear.
    assert re.search(r'id="settings-panel"[^>]*\bhidden\b', html)
    # The rings row ships hidden until the backend advertises lightning.
    assert re.search(r'id="setting-rings-row"[^>]*\bhidden\b', html)


def test_settings_i18n_keys_have_fr_en_parity():
    en = json.loads(_read("i18n", "en.json"))
    fr = json.loads(_read("i18n", "fr.json"))
    en_settings = {k for k in en if k.startswith("settings.")}
    fr_settings = {k for k in fr if k.startswith("settings.")}
    assert en_settings, "no settings.* keys found in en.json"
    assert en_settings == fr_settings, f"FR/EN settings.* mismatch: {en_settings ^ fr_settings}"
    assert {"settings.open", "settings.opacity", "settings.rings"} <= en_settings


def test_settings_js_is_wired_into_main():
    main = _read("js", "main.js")
    assert 'from "./settings.js"' in main
    assert "initSettings(" in main
    assert "export function initSettings(" in _read("js", "settings.js")


def test_radar_and_alerts_expose_settings_accessors():
    radar = _read("js", "radar.js")
    # Opacity is applied live and persisted by radar.js (the export reads it too).
    assert "setOpacity" in radar
    assert "radar_opacity" in radar
    alerts = _read("js", "alerts.js")
    # Ring visibility is cosmetic only: evaluation/notification code is untouched.
    assert "setRingsVisible" in alerts
    assert "alert_rings" in alerts


def test_sw_shell_includes_settings_js():
    sw = _read("sw.js")
    # The settings module joined the precached shell, and the cache version moved on.
    assert '"/static/js/settings.js"' in sw
    assert 'CACHE_VERSION = "v10"' not in sw


# -- Météo-France provider + radar source switch ------------------------------


def test_index_html_has_source_switch_markup():
    html = _read("index.html")
    assert 'id="setting-source-row"' in html
    assert 'id="setting-source-radios"' in html
    assert 'role="radiogroup"' in html
    # Ships hidden until the advert lists ≥2 providers.
    assert re.search(r'id="setting-source-row"[^>]*\bhidden\b', html)


def test_settings_source_i18n_parity():
    en = json.loads(_read("i18n", "en.json"))
    fr = json.loads(_read("i18n", "fr.json"))
    assert en["settings.source"] == "Radar source"
    assert fr["settings.source"] == "Source du radar"
    # Full settings.* parity still holds with the new key.
    en_settings = {k for k in en if k.startswith("settings.")}
    fr_settings = {k for k in fr if k.startswith("settings.")}
    assert en_settings == fr_settings


def test_radar_js_exposes_provider_accessors():
    radar = _read("js", "radar.js")
    assert "getProviders" in radar
    assert "getProvider" in radar
    assert "setProvider" in radar
    # Tiles are provider-scoped and the choice is persisted under radar_provider.
    assert "/tiles/${provider}/" in radar
    assert "radar_provider" in radar


def test_settings_js_builds_source_radios():
    settings_js = _read("js", "settings.js")
    assert "setting-source-radios" in settings_js
    assert "getProviders" in settings_js
    assert "setProvider" in settings_js


def test_datesheet_js_carries_provider_on_range():
    datesheet = _read("js", "datesheet.js")
    assert "getProvider" in datesheet
    assert "provider=" in datesheet


def test_sw_cache_version_bumped_for_source_switch():
    sw = _read("sw.js")
    assert 'CACHE_VERSION = "v11"' not in sw  # moved past the previous shell
    # radar/settings/datesheet are already in the precached shell (no new entries).
    assert '"/static/js/radar.js"' in sw
    assert '"/static/js/settings.js"' in sw
    assert '"/static/js/datesheet.js"' in sw


def test_frontend_never_references_meteofrance_upstream():
    """The browser must never call Météo-France directly — only our /tiles/… paths."""
    for name in ("radar.js", "settings.js", "datesheet.js", "main.js"):
        js = _read("js", name).lower()
        assert "meteofrance.fr" not in js
        assert "public-api" not in js


def test_about_js_lists_per_provider_stats():
    about = _read("js", "about.js")
    assert "providers" in about  # renders the /api/stats radar.providers breakdown
    assert "PROVIDER_LABELS" in about


# -- LIVE means "the cursor tracks the live edge" -----------------------------


def test_radar_js_follows_the_live_edge_on_refresh():
    """A paused LIVE viewer must advance as new frames land.

    The refresh re-fetched the index but re-pinned the cursor to the frame that
    was showing, so the default (paused) state silently rotted: window-end label
    marching on, picture frozen — for up to the whole 2 h window.
    """
    radar = _read("js", "radar.js")
    assert "let following = true;" in radar
    # Following ⇒ jump to the frame that just landed.
    assert re.search(r"if \(following\) \{\s*showFrame\(frames\.length - 1\);", radar)
    # Not following ⇒ keep the user's chosen frame, and rejoin the edge only once
    # it has aged out of the window (findIndex miss).
    assert "const idx = frames.findIndex((f) => f.timestamp === shownTs);" in radar
    assert re.search(r"setFollowing\(true\);\s*showFrame\(frames\.length - 1\);", radar)


def test_radar_js_live_pill_reflects_the_live_edge():
    """The pill may only claim LIVE while the cursor is at the newest frame."""
    radar = _read("js", "radar.js")
    # setFollowing is the single writer for the pill, so the two cannot disagree.
    assert 'setLiveButton(mode === "live" && following);' in radar
    # Deliberate cursor moves (scrub + both step buttons) drop it / re-arm it.
    assert radar.count("syncFollowing()") >= 3
    assert "setFollowing(position === frames.length - 1);" in radar
    # State reaches screen readers too, not just the blinking dot.
    assert 'liveBtn.setAttribute("aria-pressed"' in radar


def test_live_button_markup_carries_pressed_state():
    html = _read("index.html")
    live_btn = re.search(r'<button\s+id="live-btn".*?>', html, re.DOTALL)
    assert live_btn, "live-btn markup not found"
    assert 'aria-pressed="true"' in live_btn.group(0)


def test_radar_js_entering_live_clears_the_refresh_backoff():
    """LIVE is a "give me the current picture" gesture: it must re-anchor the
    cadence and drop any 429/503 backoff, not no-op like the old startRefresh."""
    radar = _read("js", "radar.js")
    assert re.search(
        r"function resetRefresh\(\) \{\s*backoffMs = 0;\s*scheduleRefresh\(\);",
        radar,
    )
    assert "startRefresh" not in radar
    # Returning to the tab is the same gesture, so it clears the backoff outright
    # rather than waiting for a fetch to happen to succeed.
    assert re.search(r"backoffMs = 0;\s*await refresh\(\);\s*scheduleRefresh\(\);", radar)


def test_sw_cache_version_bumped_for_live_edge_fix():
    sw = _read("sw.js")
    assert 'CACHE_VERSION = "v13"' not in sw  # shell changed (radar.js + index.html)


# -- a live refresh cross-fades; it never blanks the map ----------------------


def test_radar_js_keeps_the_picture_up_across_a_live_refresh():
    """The refresh must swap frames by cross-fade, not by teardown.

    clearLayers() took the *visible* frame off the map before the replacement had
    requested a single tile, and showFrame only reveals a layer once its tiles are
    in — so the map showed no rain for the whole load. That is seconds whenever a
    tile misses the static cache and falls through to the app, which is exactly what
    a returning tab hits.
    """
    radar = _read("js", "radar.js")
    assert "retainCurrentLayer(); // keep the picture up until the new frame is ready" in radar
    assert re.search(
        r"function retainCurrentLayer\(\) \{\s*for \(const \[ts, layer\] of layerCache\) \{"
        r"\s*if \(layer === currentLayer\) continue;",
        radar,
    )
    # clearLayers still exists — a provider switch and a fresh window must drop the
    # old source's tiles — but the refresh path no longer uses it.
    assert "function clearLayers()" in radar


def test_radar_js_caches_tile_layers_by_timestamp():
    """Layer identity is the frame timestamp, not the scrubber position.

    A live refresh replaces the frame list and every position shifts under it, so a
    position-keyed cache cannot survive the swap — which is why it used to be thrown
    away wholesale.
    """
    radar = _read("js", "radar.js")
    assert "const layerCache = new Map(); // frame timestamp -> L.TileLayer" in radar
    assert "layerCache.set(ts, layer);" in radar
    assert "layerCache.set(position, layer);" not in radar
    assert "layerCache.get(position)" not in radar


def test_sw_cache_version_bumped_for_refresh_crossfade():
    sw = _read("sw.js")
    assert 'CACHE_VERSION = "v14"' not in sw  # shell changed (radar.js)


def test_sw_cache_version_bumped_for_legacy_tile_url_removal():
    sw = _read("sw.js")
    assert 'CACHE_VERSION = "v22"' not in sw  # shell changed (radar.js + clip.js)


# -- standalone layers explainer (/apropos, /about) ---------------------------

# The explainer ships as two documents sharing one stylesheet and one script.
EXPLAINERS = [("/apropos", "apropos.html", "fr"), ("/about", "about.html", "en")]


@pytest.mark.parametrize(("path", "_file", "_lang"), EXPLAINERS)
def test_explainer_route_serves_the_page(client, path, _file, _lang):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/html")


@pytest.mark.parametrize(("_path", "filename", "lang"), EXPLAINERS)
def test_explainer_html_carries_no_inline_asset(_path, filename, lang):
    """The pages must satisfy the same strict CSP as the shell.

    Production serves them under ``script-src 'self'; style-src 'self'`` with no
    inline exemption, and fonts fall back to ``default-src 'self'`` — so an inline
    <style>/<script> or a data: font would silently render the page unstyled and
    inert. It was authored as one self-contained file, which is exactly the shape
    that regresses here, so pin it.
    """
    html = _read(filename)
    assert "<style" not in html, "inline <style> would be blocked by the CSP"
    assert "<script>" not in html, "inline <script> would be blocked by the CSP"
    assert "data:font" not in html, "a data: font would be blocked by the CSP"
    assert "data:image" not in html, "a data: image would be blocked by the CSP"
    assert "<script src=" in html
    assert 'rel="stylesheet"' in html
    # The shared script picks its strings off the document's own lang.
    assert f'<html lang="{lang}">' in html
    css = _read("css", "apropos.css")
    assert "data:font" not in css
    assert "data:image" not in css


@pytest.mark.parametrize("filename", ["index.html", "apropos.html", "about.html"])
def test_html_carries_no_style_attribute(filename):
    """No shipped document may style an element with a ``style="…"`` attribute.

    Both CSPs are ``style-src 'self'`` with no ``'unsafe-hashes'``. style-src-attr
    falls back to style-src, so the browser drops *every* inline style attribute —
    silently, with only a console violation. This shipped: the shell's icon sprite
    lost its ``display: none`` and rendered as a blank block above the map, and the
    explainer's ``--img:`` handoff was dropped, blanking the layer thumbnails.
    CSSOM writes (``el.style.foo = …``) are NOT covered by CSP and stay fine — it
    is only the attribute, in markup or via setAttribute, that is blocked.
    """
    html = _read(filename)
    offenders = re.findall(r"\sstyle=[\"'][^\"']*[\"']", html)
    assert not offenders, f"{filename}: inline style attribute(s) {offenders}"


def test_frontend_js_never_writes_a_style_attribute():
    """setAttribute("style", …) is blocked by the same directive as the markup."""
    for path in sorted(FRONTEND.joinpath("js").glob("*.js")):
        js = path.read_text(encoding="utf-8")
        assert 'setAttribute("style"' not in js, path.name
        assert "setAttribute('style'" not in js, path.name


@pytest.mark.parametrize(("_path", "filename", "_lang"), EXPLAINERS)
def test_explainer_assets_exist(_path, filename, _lang):
    html = _read(filename)
    css = _read("css", "apropos.css")
    refs = set(re.findall(r'"(/static/[^"]+)"', html)) | set(
        re.findall(r"url\((/static/[^)]+)\)", css)
    )
    assert refs, "no /static/ references found"
    for ref in refs:
        rel = ref[len("/static/") :]
        assert FRONTEND.joinpath(*rel.split("/")).exists(), f"missing asset: {ref}"


def test_explainer_script_localises_off_the_document_lang():
    """One script serves both documents; only the provenance line differs.

    Duplicating it per language would duplicate the 166-strike data blob with it, so
    the strings are keyed by the document's lang instead. A French string left
    outside that map would show up on the English page.
    """
    js = _read("js", "apropos.js")
    assert 'document.documentElement.lang === "en"' in js
    assert "Capturé sur un orage réel" in js
    assert "Captured during a real storm" in js
    # The blob is data only — no prose left in it to leak across languages.
    assert '"note"' not in js


def test_about_dialog_links_to_the_explainer():
    html = _read("index.html")
    assert 'id="about-explainer"' in html
    assert 'href="/apropos"' in html  # FR default; about.js swaps it per locale
    assert 'data-i18n="about.explainer"' in html
    js = _read("js", "about.js")
    assert 'EXPLAINER_URL = { fr: "/apropos", en: "/about" }' in js


def test_sw_does_not_answer_the_explainer_with_the_app_shell():
    """Offline, neither explainer URL may fall back to the cached shell.

    The navigation handler answers any failed navigation with "/" — which would put
    the radar UI behind an /apropos or /about URL.
    """
    sw = _read("sw.js")
    assert 'url.pathname === "/apropos" || url.pathname === "/about"' in sw
    assert 'CACHE_VERSION = "v16"' not in sw  # shell changed (about.js, index.html, i18n)
    # shell changed (index.html head, i18n, manifest)
    assert 'CACHE_VERSION = "v19"' not in sw


# -- crawlability / link previews ---------------------------------------------

# The one public origin. It is hardcoded in three static documents plus robots.txt
# and sitemap.xml, because production serves them as plain files from Nginx with no
# templating layer to inject it. That duplication is the thing worth pinning: a
# canonical or an hreflang alternate pointing at the wrong host silently
# de-indexes the page it names.
CANONICAL_ORIGIN = "https://rainradar.hleroy.com"

# Every shipped HTML document, with its canonical URL and declared language.
DOCUMENTS = [
    ("index.html", f"{CANONICAL_ORIGIN}/", "fr"),
    ("apropos.html", f"{CANONICAL_ORIGIN}/apropos", "fr"),
    ("about.html", f"{CANONICAL_ORIGIN}/about", "en"),
]


def _ld_json_blocks(html: str) -> list[dict]:
    """Every application/ld+json data block in a document, parsed."""
    raw = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    return [json.loads(block) for block in raw]


@pytest.mark.parametrize(("filename", "canonical", "lang"), DOCUMENTS)
def test_document_declares_its_canonical_and_language(filename, canonical, lang):
    html = _read(filename)
    assert f'<html lang="{lang}">' in html
    assert f'rel="canonical" href="{canonical}"' in html
    # A description is what a result page shows under the title; without one the
    # engine synthesises a snippet from the visible text, and the shell has none.
    assert re.search(r'<meta\s+name="description"', html), f"{filename} has no description"


@pytest.mark.parametrize(("filename", "_canonical", "_lang"), DOCUMENTS)
def test_document_never_names_another_origin(filename, _canonical, _lang):
    """No absolute self-reference may point anywhere but the canonical origin."""
    html = _read(filename)
    assert "og:url" in html, f"{filename} lost its og:url"
    urls = re.findall(r'(?:href|content)="(https?://[^"]+)"', html)
    # Match on the host, not the whole URL: the source-code link is
    # github.com/hleroy/rainradar, which carries the project name in its *path* and
    # is not a self-reference at all. Hostname matching still catches what this
    # guards against — a stale domain, or the canonical origin dropped to http.
    ours = [u for u in urls if "rainradar" in (urlsplit(u).hostname or "")]
    assert ours, f"{filename} has no absolute self-references"
    for url in ours:
        assert url.startswith(CANONICAL_ORIGIN), f"{filename}: foreign origin {url}"


@pytest.mark.parametrize(("filename", "_canonical", "_lang"), DOCUMENTS)
def test_document_has_a_complete_link_preview_card(filename, _canonical, _lang):
    """og:image must be absolute and sized, or scrapers fall back to no card."""
    html = _read(filename)
    for tag in ("og:type", "og:site_name", "og:url", "og:title", "og:description"):
        assert f'property="{tag}"' in html, f"{filename} missing {tag}"
    assert 'property="og:image:width" content="1200"' in html
    assert 'property="og:image:height" content="630"' in html
    assert 'name="twitter:card" content="summary_large_image"' in html
    images = set(re.findall(r'(?:property|name)="(?:og|twitter):image" content="([^"]+)"', html))
    assert images, f"{filename} declares no preview image"
    for url in images:
        assert url.startswith(f"{CANONICAL_ORIGIN}/static/"), f"{filename}: {url} not absolute"
        rel = url[len(f"{CANONICAL_ORIGIN}/static/") :]
        asset = FRONTEND.joinpath(*rel.split("/"))
        assert asset.exists(), f"{filename}: missing preview image {url}"
        # WhatsApp (a share target the app targets explicitly) drops previews whose
        # image runs to a few hundred KB, so the card's weight is a contract.
        assert asset.stat().st_size < 300_000, f"{url} is too heavy for a link preview"


@pytest.mark.parametrize(("filename", "_canonical", "_lang"), DOCUMENTS)
def test_structured_data_parses(filename, _canonical, _lang):
    """An ld+json block is a data block, not a script — CSP permits it, JSON must parse.

    A malformed block is invisible: browsers ignore it and crawlers drop it silently.
    """
    blocks = _ld_json_blocks(_read(filename))
    assert blocks, f"{filename} carries no structured data"
    for block in blocks:
        assert block["@context"] == "https://schema.org"
        assert block["@type"]
        assert block["url"].startswith(CANONICAL_ORIGIN)


def test_index_head_is_search_facing():
    html = _read("index.html")
    # Pinch-zoom must stay available; maximum-scale is an accessibility failure.
    viewport = re.search(r'<meta name="viewport" content="([^"]+)"', html)
    assert viewport, "index.html lost its viewport meta"
    assert "maximum-scale" not in viewport.group(1)
    # The app has one URL, so it must not advertise language alternates of its own —
    # that pair belongs to the explainer, which really does have two URLs.
    assert not re.search(r'rel="alternate"\s+hreflang=', html)
    assert 'property="og:locale" content="fr_FR"' in html


def test_document_title_is_not_clobbered_by_i18n():
    """main.js overwrites document.title after the dictionary loads.

    The rendered DOM is what crawlers read, so if that assignment reused the short
    ``app.title`` label it would silently undo the search-facing <title> the server
    sent. Separate keys keep both.
    """
    main = _read("js", "main.js")
    assert 'document.title = t("app.document_title")' in main
    en = json.loads(_read("i18n", "en.json"))
    fr = json.loads(_read("i18n", "fr.json"))
    for dictionary in (en, fr):
        assert dictionary["app.document_title"] != dictionary["app.title"]
        # Long enough to carry the qualifiers a result page needs, short enough
        # that Google does not truncate it into nonsense.
        assert 30 < len(dictionary["app.document_title"]) <= 70


def test_explainer_hreflang_group_is_reciprocal_and_symmetric():
    """Each page must list every alternate INCLUDING itself, or the group is dropped."""
    expected = {
        ("fr", f"{CANONICAL_ORIGIN}/apropos"),
        ("en", f"{CANONICAL_ORIGIN}/about"),
        ("x-default", f"{CANONICAL_ORIGIN}/apropos"),
    }
    for filename in ("apropos.html", "about.html"):
        found = set(
            re.findall(r'rel="alternate" hreflang="([^"]+)" href="([^"]+)"', _read(filename))
        )
        assert found == expected, f"{filename}: hreflang group {found}"


def test_robots_txt_shields_the_data_paths():
    robots = _read("robots.txt")
    assert "User-agent: *" in robots
    # Tiles are the substantive one: a full retention window is ~1.6M PNGs.
    for path in ("/api/", "/tiles/", "/metrics", "/healthz", "/readyz"):
        assert f"Disallow: {path}" in robots
    # The Sitemap directive is the one line that must be absolute.
    assert f"Sitemap: {CANONICAL_ORIGIN}/sitemap.xml" in robots


def test_sitemap_lists_every_document_and_nothing_else():
    import xml.etree.ElementTree as ET  # noqa: PLC0415

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(_read("sitemap.xml"))  # noqa: S314 — our own committed file
    locs = [el.text for el in root.findall("sm:url/sm:loc", ns)]
    assert locs == [canonical for _f, canonical, _l in DOCUMENTS], locs
    # The sitemap and the pages must agree; a disagreement is a crawl-budget leak.
    for filename, canonical, _lang in DOCUMENTS:
        assert f'rel="canonical" href="{canonical}"' in _read(filename)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("path", "content_type"),
    [("/robots.txt", "text/plain"), ("/sitemap.xml", "application/xml")],
)
def test_crawler_file_route_serves_the_file(client, path, content_type):
    """Dev must answer the same URLs Nginx does in production, with the same types."""
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith(content_type)


@pytest.mark.django_db
def test_unknown_paths_are_a_real_404(client):
    """The shell is registered at the exact empty path, never as a catch-all.

    Answering every unknown URL with a 200 shell is a soft 404: it hands crawlers an
    unbounded supply of duplicate pages, and it is what used to swallow /robots.txt.
    Nginx needs the same shape spelled out (``location = /`` plus a terminal
    ``location / { return 404; }``) because try_files always resolves.
    """
    assert client.get("/").status_code == 200
    assert client.get("/definitely-not-a-page").status_code == 404


# -- the live refresh is anchored on the provider cadence ---------------------


def test_radar_js_hardcodes_no_frame_cadence():
    """The client learns every cadence from the ``providers`` advert.

    A fixed 5-min poll used to live here. Besides violating "no new hardcoded
    intervals", it aliased: a 300 s period against Météo-France's 300 s cadence can
    settle just before each publication and stay a full frame behind forever.
    """
    radar = _read("js", "radar.js")
    assert "REFRESH_MS" not in radar
    assert "REFRESH_MAX_MS" not in radar
    # One helper is the single place a frame interval enters the frontend.
    assert "return entry ? entry.frame_interval : DEFAULT_FRAME_INTERVAL_S;" in radar
    # Gap tolerance derives from it too, rather than repeating the 900 s literal.
    assert "const DEFAULT_GAP_TOLERANCE_S = 1.5 * DEFAULT_FRAME_INTERVAL_S;" in radar
    assert radar.count("gapToleranceS = 1.5 * frameIntervalS(") == 2


def test_radar_js_anchors_the_refresh_on_the_newest_frame():
    """The next look is scheduled off the frame's OWN timestamp, plus jitter.

    Anchoring is what breaks the aliasing; the jitter is load-bearing rather than
    cosmetic, because every client anchors off the same newest_ts and would
    otherwise hit /api/radar/frames on the very same second.
    """
    radar = _read("js", "radar.js")
    assert "const due = newest + intervalS + lagS;" in radar
    assert "const jitter = Math.random() * JITTER_S;" in radar
    assert "const delayS = now < due ? due - now + jitter : RETRY_S + jitter;" in radar
    # Never poll inside the response micro-cache window.
    assert "return Math.max(delayS, MIN_DELAY_S) * 1000;" in radar


def test_radar_js_tracks_the_publication_lag_within_bounds():
    """The lag estimate self-tunes per provider, and stays clamped.

    What a client measures is an upper bound on the true delay (it only looks at
    discrete moments), so it decays each cycle to probe earlier; a too-early wake
    costs one short retry and pushes it back up.
    """
    radar = _read("js", "radar.js")
    assert (
        "lagS = Math.min(LAG_MAX_S, Math.max(LAG_MIN_S, nowS() - newest - LAG_DECAY_S));" in radar
    )
    # Decay must outweigh the mean jitter, or the estimate ratchets upward forever.
    lag_decay = int(re.search(r"const LAG_DECAY_S = (\d+);", radar).group(1))
    jitter = int(re.search(r"const JITTER_S = (\d+);", radar).group(1))
    assert lag_decay >= jitter / 2


def test_radar_js_bounds_the_retry_chase():
    """A stalled provider must not turn the short retries into a permanent poll."""
    radar = _read("js", "radar.js")
    assert "if (now > newest + intervalS + LAG_MAX_S) return spaced;" in radar


def test_radar_js_backoff_ceiling_is_cadence_scaled():
    """A view labelled DIRECT may not sit half an hour behind while shedding."""
    radar = _read("js", "radar.js")
    assert "const BACKOFF_MIN_CEILING_MS = 10 * 60 * 1000;" in radar
    assert "const ceiling = Math.max(frameIntervalS() * 1000, BACKOFF_MIN_CEILING_MS);" in radar


def test_radar_js_survives_a_network_level_fetch_rejection():
    """An offline blip is a failed fetch, not an escaping exception.

    fetch() rejects rather than resolving on a network error, and the rejection used
    to escape the async setTimeout callback — so the self-scheduling chain never
    re-armed and the live view stayed frozen until a page reload.
    """
    radar = _read("js", "radar.js")
    assert re.search(
        r"try \{\s*res = await fetchFrames\(\);\s*\} catch \{",
        radar,
    )
    assert "res = { ok: false, status: 0 };" in radar


def test_sw_cache_version_bumped_for_anchored_refresh():
    sw = _read("sw.js")
    assert 'CACHE_VERSION = "v21"' not in sw  # shell changed (radar.js)
