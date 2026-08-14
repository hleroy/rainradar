// Storm proximity alerts, foreground path. The user arms a bell,
// picks an *anchor* point (their position, or any point on the map), and gets an OS
// notification when live lightning strikes within two fixed rings around it —
// 30 km ("storm approaching") and 10 km ("storm overhead"). Alerts evaluate on the
// arrival of each live strike over our OWN SSE connection, independent of the
// lightning layer's visibility and of the radar cursor; a per-tier 30-min quiet
// window throttles the notifications. Purely additive: a failure here can only toast
// or skip a strike — it never touches radar or the lightning layer (a third,
// most-expendable failure domain). Background delivery (locked phone / closed tab)
// is handled by Web Push and is out of scope here.

const TIER_OUTER_KM = 30; // "storm approaching"
const TIER_INNER_KM = 10; // "storm overhead"
const REARM_S = 1800; // a tier re-arms after this many strike-free seconds
const FRESH_S = 600; // ignore strikes older than this (SSE replays a recent buffer)

const EARTH_KM = 6371;
const DIRS = ["n", "ne", "e", "se", "s", "sw", "w", "nw"]; // 8-wind, clockwise from N

// Fine dotted rings (real meters, so they scale with zoom). Inner ring reads
// stronger than the outer so the hierarchy is legible without labels. Stroke only
// (fillOpacity 0) so they never obscure the radar; non-interactive so they never eat
// map taps. The colour is a teal-leaning cyan — tied to the lightning layer's family
// but dark enough to read on the pale base map, and deliberately NOT the geo blue.
const RING_COLOR = "#0097a7";
const RING_STYLE_OUTER = {
  radius: TIER_OUTER_KM * 1000,
  color: RING_COLOR,
  weight: 2.5,
  opacity: 0.7,
  dashArray: "2 7",
  lineCap: "round",
  fill: false,
  fillOpacity: 0,
  interactive: false,
};
const RING_STYLE_INNER = {
  ...RING_STYLE_OUTER,
  radius: TIER_INNER_KM * 1000,
  weight: 3.5,
  opacity: 0.9,
};
const PIN_STYLE = {
  radius: 6,
  color: RING_COLOR,
  weight: 2,
  fillColor: "#00e5ff",
  fillOpacity: 0.9,
};

// -- geometry (small pure helpers) -------------------------------------------

function toRad(deg) {
  return (deg * Math.PI) / 180;
}

// Great-circle distance in km between [lat, lon] and a strike {lat, lon}.
function haversineKm(anchor, s) {
  const dLat = toRad(s.lat - anchor.lat);
  const dLon = toRad(s.lon - anchor.lon);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(anchor.lat)) * Math.cos(toRad(s.lat)) * Math.sin(dLon / 2) ** 2;
  return EARTH_KM * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// 8-wind compass sector from the anchor toward the strike. The flat-earth bearing
// approximation is fine at ≤ 30 km; we only need one of eight sectors.
function bearing8(anchor, s) {
  const dLon = toRad(s.lon - anchor.lon);
  const dLat = toRad(s.lat - anchor.lat);
  const angle = Math.atan2(dLon * Math.cos(toRad(anchor.lat)), dLat); // 0 = N, CW
  const deg = (angle * 180) / Math.PI;
  const idx = Math.round(((deg % 360) + 360) / 45) % 8;
  return DIRS[idx];
}

export function initAlerts(map, { t, showToast, geo, config, currentLocale }) {
  const btn = document.getElementById("alert-btn");
  const enabled = !!(config && config.enabled);

  // Backend doesn't advertise lightning: hide the button and return a no-op so the
  // rest of the app needn't special-case it (mirrors lightning.js).
  if (!enabled) {
    if (btn) btn.hidden = true;
    return { refreshI18n() {} };
  }

  // Background delivery advert: when the backend serves Web Push and
  // this browser supports it, arming also registers a push subscription so alerts
  // reach a closed app. Absent/unsupported ⇒ the module stays foreground-only.
  const pushConfig = config.push || { enabled: false, vapid_public_key: "" };
  const localeOf = typeof currentLocale === "function" ? currentLocale : () => "en";
  let pushSubscribed = false;

  const bbox = config.bbox || [41.2, 51.5, -6.0, 9.7]; // [S, N, W, E]
  const attribution = config.attribution;
  const onAttribution = (html) => map.attributionControl.addAttribution(html);
  const onAttributionRemove = (html) => map.attributionControl.removeAttribution(html);

  let state = "off"; // off | placing | armed | blocked
  let anchor = null; // { lat, lon, source } | null
  let prevAnchor = null; // remembered while placing, to restore on Cancel
  let prevState = "off";
  let throttle = { outer: { lastStrikeAt: 0 }, inner: { lastStrikeAt: 0 } };
  let es = null;
  let outerRing = null;
  let innerRing = null;
  let pin = null;
  let lastPersist = 0;
  // Display preference (settings popover): whether the 30/10 km warning rings are
  // drawn on the map. Purely cosmetic — evaluation and notifications are untouched,
  // and the anchor pin always stays visible so an armed alert is never invisible.
  let ringsVisible = true;
  try {
    ringsVisible = localStorage.getItem("alert_rings") !== "off";
  } catch {
    /* storage blocked → default (shown) */
  }

  const tiers = [
    { id: "inner", radiusKm: TIER_INNER_KM },
    { id: "outer", radiusKm: TIER_OUTER_KM },
  ];

  // -- DOM handles ------------------------------------------------------------

  const overlay = document.getElementById("alert-overlay");
  const dialog = document.getElementById("alert-dialog");
  const closeBtn = document.getElementById("alert-close");
  const actionsIdle = document.getElementById("alert-actions-idle");
  const actionsArmed = document.getElementById("alert-actions-armed");
  const explainEl = document.getElementById("alert-explain");
  const statusEl = document.getElementById("alert-status");
  const pushStatusEl = document.getElementById("alert-push-status");
  const privacyEl = document.getElementById("alert-privacy");
  const iosHintEl = document.getElementById("alert-ios-hint");
  const placebar = document.getElementById("alert-placebar");
  const crosshair = document.getElementById("alert-crosshair");
  let sheetOpen = false;

  // -- persistence ------------------------------------------------------------

  function persist({ force } = {}) {
    const now = Date.now();
    if (!force && now - lastPersist < 1000) return; // coalesce bursts
    lastPersist = now;
    try {
      localStorage.setItem("alert_state", JSON.stringify(throttle));
    } catch {
      /* storage may be full/blocked; alerts still work in-memory this session */
    }
  }

  function persistIntent() {
    try {
      localStorage.setItem("alert_intent", state === "armed" ? "on" : "off");
      if (anchor) localStorage.setItem("alert_anchor", JSON.stringify(anchor));
    } catch {
      /* ignore */
    }
  }

  function loadThrottle() {
    try {
      const raw = JSON.parse(localStorage.getItem("alert_state") || "");
      if (raw && raw.outer && raw.inner) {
        throttle = {
          outer: { lastStrikeAt: Number(raw.outer.lastStrikeAt) || 0 },
          inner: { lastStrikeAt: Number(raw.inner.lastStrikeAt) || 0 },
        };
      }
    } catch {
      /* corrupt or absent → fresh throttle */
    }
  }

  // -- rendering --------------------------------------------------------------

  function ensureLayers() {
    if (outerRing) return;
    outerRing = L.circle([0, 0], RING_STYLE_OUTER);
    innerRing = L.circle([0, 0], RING_STYLE_INNER);
    pin = L.circleMarker([0, 0], PIN_STYLE);
    pin.on("click", openSheet);
  }

  function moveLayersTo(latlng) {
    ensureLayers();
    outerRing.setLatLng(latlng);
    innerRing.setLatLng(latlng);
    pin.setLatLng(latlng);
  }

  function addLayers() {
    ensureLayers();
    if (ringsVisible) {
      outerRing.addTo(map);
      innerRing.addTo(map);
    }
    pin.addTo(map);
  }

  // Settings popover: show/hide the rings live (and persist). Only touches the map
  // when the anchor chrome is currently displayed (armed or placement preview).
  function setRingsVisible(v) {
    ringsVisible = !!v;
    try {
      localStorage.setItem("alert_rings", ringsVisible ? "on" : "off");
    } catch {
      /* storage blocked; the choice still applies this session */
    }
    if (!outerRing) return;
    if (ringsVisible && (state === "armed" || state === "placing")) {
      outerRing.addTo(map);
      innerRing.addTo(map);
      if (map.hasLayer(pin)) pin.bringToFront(); // keep the pin tappable on top
    } else {
      map.removeLayer(outerRing);
      map.removeLayer(innerRing);
    }
  }

  function removeLayers() {
    if (!outerRing) return;
    map.removeLayer(outerRing);
    map.removeLayer(innerRing);
    map.removeLayer(pin);
  }

  // -- SSE (own connection, independent of the layer) -------------------------

  function openSSE() {
    closeSSE();
    es = new EventSource("/api/lightning/stream");
    es.addEventListener("strike", (e) => {
      try {
        evaluate(JSON.parse(e.data));
      } catch {
        /* malformed event → skip; the next strike is independent */
      }
    });
  }

  function closeSSE() {
    if (es) {
      es.close();
      es = null;
    }
  }

  // A frozen mobile tab can kill the EventSource without its native auto-reconnect
  // firing; re-open on return to foreground. The server's recent-buffer replay plus
  // the fresh/armed gates below make the reconnect safe (no stale re-fires).
  function onVisibility() {
    if (state !== "armed") return;
    if (!es || es.readyState === EventSource.CLOSED) openSSE();
  }

  // -- web push (background delivery) -----------------------------------------

  // True only when the backend advertises push AND this browser can do it. On iOS,
  // PushManager exists only in an installed PWA (a Safari tab has none) — the sheet
  // surfaces that. Everything here is best-effort: any failure leaves the
  // foreground path (own SSE + Notification) fully working.
  function pushSupported() {
    return !!(pushConfig.enabled && "serviceWorker" in navigator && "PushManager" in window);
  }

  function isIOSWithoutPush() {
    return !("PushManager" in window) && /iP(hone|ad|od)/.test(navigator.userAgent || "");
  }

  function urlB64ToUint8Array(base64) {
    const padding = "=".repeat((4 - (base64.length % 4)) % 4);
    const raw = atob((base64 + padding).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
  }

  async function postJSON(url, body) {
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      return r.ok;
    } catch {
      return false; // network failure → caller degrades to foreground-only
    }
  }

  // Register (or refresh) the push subscription for the current anchor + locale. Also
  // the re-upsert path (load reconcile, anchor move, language toggle) — getSubscription
  // reuses the existing browser subscription, so this only refreshes server-side state.
  async function subscribePush() {
    if (!pushSupported() || !anchor || !pushConfig.vapid_public_key) return;
    try {
      const reg = await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) {
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlB64ToUint8Array(pushConfig.vapid_public_key),
        });
      }
      const j = sub.toJSON();
      const ok = await postJSON("/api/alerts/subscribe", {
        endpoint: sub.endpoint,
        keys: { p256dh: j.keys.p256dh, auth: j.keys.auth },
        lat: anchor.lat,
        lon: anchor.lon,
        locale: localeOf(),
      });
      pushSubscribed = ok;
      if (ok) {
        try {
          localStorage.setItem("alert_push_endpoint", sub.endpoint);
        } catch {
          /* storage blocked; the endpoint is still live on the server */
        }
      }
    } catch {
      pushSubscribed = false; // silently foreground-only
    }
  }

  async function unsubscribePush() {
    let endpoint = null;
    try {
      endpoint = localStorage.getItem("alert_push_endpoint");
    } catch {
      endpoint = null;
    }
    try {
      const reg = await navigator.serviceWorker?.ready;
      const sub = reg && (await reg.pushManager.getSubscription());
      if (sub) {
        endpoint = sub.endpoint;
        await sub.unsubscribe();
      }
    } catch {
      /* best effort */
    }
    if (endpoint) await postJSON("/api/alerts/unsubscribe", { endpoint });
    pushSubscribed = false;
    try {
      localStorage.removeItem("alert_push_endpoint");
    } catch {
      /* ignore */
    }
  }

  // -- evaluation -------------------------------------------------------------

  function evaluate(s) {
    if (state !== "armed" || !anchor) return;
    if (typeof s.lat !== "number" || typeof s.lon !== "number" || typeof s.time !== "number") {
      return;
    }
    const dist = haversineKm(anchor, s);
    const nowS = Date.now() / 1000;
    const fresh = nowS - s.time <= FRESH_S;
    for (const tier of tiers) {
      if (dist > tier.radiusKm) continue;
      // Check armed-ness BEFORE refreshing the quiet timer (order is load-bearing).
      const armed = s.time - (throttle[tier.id].lastStrikeAt || 0) > REARM_S;
      if (armed && fresh) notify(tier, dist, bearing8(anchor, s));
      throttle[tier.id].lastStrikeAt = Math.max(throttle[tier.id].lastStrikeAt || 0, s.time);
    }
    persist();
  }

  function notify(tier, distKm, dir) {
    const title = t(`alert.notify.${tier.id}.title`);
    const body = t("alert.notify.body")
      .replace("{dist}", String(Math.max(1, Math.round(distKm))))
      .replace("{dir}", t(`alert.dir.${dir}`));
    const opts = {
      body,
      tag: "rainradar-alert", // one tag: an inner escalation replaces the outer note
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
    };
    (async () => {
      try {
        const reg = await navigator.serviceWorker?.getRegistration();
        if (reg) {
          await reg.showNotification(title, opts);
        } else if ("Notification" in window && Notification.permission === "granted") {
          const n = new Notification(title, opts);
          n.onclick = () => window.focus();
        } else {
          showToast(`${title} — ${body}`);
        }
      } catch {
        showToast(`${title} — ${body}`); // last resort: never throw out of a strike
      }
    })();
  }

  // -- arm / disarm -----------------------------------------------------------

  function insideBbox(a) {
    const [s, n, w, e] = bbox;
    return a.lat >= s && a.lat <= n && a.lon >= w && a.lon <= e;
  }

  // Commit an anchor (from either source). Returns true if armed, false if refused.
  function arm(candidate) {
    if (!insideBbox(candidate)) {
      showToast(t("alert.out_of_coverage"));
      return false;
    }
    // Ask for notification permission inside the click gesture (the bbox check is
    // synchronous, so we're still in it).
    if (!("Notification" in window)) {
      showToast(t("alert.denied"));
      return false;
    }
    const finish = (perm) => {
      if (perm === "granted") {
        anchor = candidate;
        state = "armed";
        throttle = { outer: { lastStrikeAt: 0 }, inner: { lastStrikeAt: 0 } };
        moveLayersTo([anchor.lat, anchor.lon]);
        addLayers();
        if (attribution) onAttribution(attribution);
        openSSE();
        subscribePush(); // best-effort background delivery; foreground works regardless
        persistIntent();
        persist({ force: true });
        updateButton();
        showToast(t("alert.enabled"));
      } else if (perm === "denied") {
        state = "blocked";
        persistIntent();
        updateButton();
        showToast(t("alert.denied"));
      } else {
        // dismissed: keep the anchor persisted so a re-tap re-prompts.
        anchor = candidate;
        state = "off";
        persistIntent();
        updateButton();
      }
    };
    if (Notification.permission === "granted") {
      finish("granted");
    } else {
      Notification.requestPermission().then(finish).catch(() => finish("default"));
    }
    return true;
  }

  function disarm() {
    closeSSE();
    unsubscribePush(); // deletes the server row + browser subscription (best-effort)
    removeLayers();
    if (attribution) onAttributionRemove(attribution);
    state = "off";
    persistIntent();
    updateButton();
    showToast(t("alert.disabled"));
  }

  // Arm silently on load if the user had it on and permission is still granted
  // (mirrors geo.js's intent reconciliation — never auto-prompt).
  function reconcile() {
    let savedAnchor = null;
    try {
      savedAnchor = JSON.parse(localStorage.getItem("alert_anchor") || "");
    } catch {
      savedAnchor = null;
    }
    const validAnchor =
      savedAnchor &&
      typeof savedAnchor.lat === "number" &&
      typeof savedAnchor.lon === "number";
    if (validAnchor) anchor = savedAnchor;
    if (localStorage.getItem("alert_intent") !== "on" || !validAnchor) {
      updateButton();
      return;
    }
    if (!("Notification" in window)) {
      updateButton();
      return;
    }
    if (Notification.permission === "granted") {
      loadThrottle();
      state = "armed";
      moveLayersTo([anchor.lat, anchor.lon]);
      addLayers();
      if (attribution) onAttribution(attribution);
      openSSE();
      subscribePush(); // re-upsert on load: refreshes last_seen_at + anchor + locale
    } else if (Notification.permission === "denied") {
      state = "blocked";
    }
    updateButton();
  }

  // -- placement mode (pan-under-crosshair) -----------------------------------

  function onMapMove() {
    moveLayersTo(map.getCenter());
  }

  function enterPlacement() {
    prevAnchor = anchor;
    prevState = state;
    closeSheet();
    state = "placing";
    updateButton();
    ensureLayers();
    // Preview rings + pin ride the map centre while the user pans beneath the reticle.
    moveLayersTo(map.getCenter());
    addLayers();
    map.on("move", onMapMove);
    crosshair.hidden = false;
    placebar.hidden = false;
  }

  function exitPlacement() {
    map.off("move", onMapMove);
    crosshair.hidden = true;
    placebar.hidden = true;
  }

  function confirmPlacement() {
    const c = map.getCenter();
    const candidate = { lat: c.lat, lon: c.lng, source: "manual" };
    exitPlacement();
    if (!arm(candidate)) {
      // Refused (out of coverage): restore whatever we had before placing.
      restorePrePlacement();
    }
  }

  function cancelPlacement() {
    exitPlacement();
    restorePrePlacement();
  }

  function restorePrePlacement() {
    anchor = prevAnchor;
    if (prevState === "armed" && anchor) {
      state = "armed";
      moveLayersTo([anchor.lat, anchor.lon]);
      addLayers();
    } else {
      state = prevState === "armed" ? "off" : prevState;
      removeLayers();
    }
    updateButton();
  }

  // -- sheet (modal; mirrors datesheet.js chrome) -----------------------------

  function syncSheetVariant() {
    const armed = state === "armed";
    if (actionsIdle) actionsIdle.hidden = armed;
    if (actionsArmed) actionsArmed.hidden = !armed;
    if (explainEl) explainEl.hidden = armed;
    if (statusEl) statusEl.hidden = !armed;
    // Background-delivery status: whether a closed app will still be notified.
    const active = armed && pushSupported() && pushSubscribed;
    if (pushStatusEl) {
      pushStatusEl.hidden = !armed;
      pushStatusEl.textContent = active
        ? t("alert.sheet.push_active")
        : t("alert.sheet.foreground_only");
    }
    if (privacyEl) privacyEl.hidden = !active;
    // iOS Safari tab (no PushManager): point at installing the PWA for background alerts.
    if (iosHintEl) iosHintEl.hidden = !isIOSWithoutPush();
  }

  function openSheet() {
    if (sheetOpen) return;
    if (state === "blocked") {
      showToast(t("alert.denied"));
      return;
    }
    sheetOpen = true;
    syncSheetVariant();
    overlay.hidden = false;
    dialog.focus();
  }

  function closeSheet() {
    if (!sheetOpen) return;
    sheetOpen = false;
    overlay.hidden = true;
    if (btn) btn.focus();
  }

  function focusable() {
    return [
      ...dialog.querySelectorAll('button:not([disabled]), [tabindex]:not([tabindex="-1"])'),
    ].filter((el) => el.offsetParent !== null);
  }

  function onKeydown(e) {
    if (state === "placing" && e.key === "Escape") {
      e.preventDefault();
      cancelPlacement();
      return;
    }
    if (!sheetOpen) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeSheet();
      return;
    }
    if (e.key !== "Tab") return;
    const items = focusable();
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  // "My position": snapshot the current fix (never follows later fixes). Uses the
  // last known fix if present, else a one-shot locate. The sheet stays open; on
  // failure we reuse geo.js's own messaging keys.
  async function useMyPosition() {
    let fix = geo?.getLastFix?.();
    if (!fix) {
      try {
        fix = await geo.requestFix();
      } catch (err) {
        if (err && err.code === err.PERMISSION_DENIED) showToast(t("geo.blocked"));
        else if (err && err.code === err.TIMEOUT) showToast(t("geo.timeout"));
        else showToast(t("geo.unavailable"));
        return;
      }
    }
    const candidate = { lat: fix[0], lon: fix[1], source: "geo" };
    if (arm(candidate)) closeSheet();
  }

  // -- button -----------------------------------------------------------------

  function updateButton() {
    if (!btn) return;
    btn.dataset.state = state;
    btn.setAttribute("aria-pressed", state === "armed" ? "true" : "false");
    btn.title = t("control.alerts");
    btn.setAttribute("aria-label", t("control.alerts"));
  }

  function refreshI18n() {
    updateButton();
    if (sheetOpen) syncSheetVariant(); // re-render the JS-built status strings
    if (state === "armed") subscribePush(); // re-upsert so push copy follows the new locale
  }

  // -- init -------------------------------------------------------------------

  if (btn) {
    btn.hidden = false;
    btn.addEventListener("click", openSheet);
  }
  if (closeBtn) closeBtn.addEventListener("click", closeSheet);
  if (overlay) {
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeSheet();
    });
  }
  document.getElementById("alert-my-position")?.addEventListener("click", useMyPosition);
  document.getElementById("alert-my-position-armed")?.addEventListener("click", useMyPosition);
  document.getElementById("alert-pick-map")?.addEventListener("click", enterPlacement);
  document.getElementById("alert-move")?.addEventListener("click", () => {
    if (anchor) map.setView([anchor.lat, anchor.lon], map.getZoom());
    enterPlacement();
  });
  document.getElementById("alert-disable")?.addEventListener("click", () => {
    closeSheet();
    disarm();
  });
  document.getElementById("alert-confirm")?.addEventListener("click", confirmPlacement);
  document.getElementById("alert-cancel")?.addEventListener("click", cancelPlacement);
  document.addEventListener("keydown", onKeydown);
  document.addEventListener("visibilitychange", onVisibility);

  reconcile();

  return { refreshI18n, setRingsVisible, ringsVisible: () => ringsVisible };
}
