// Bootstraps the map, base layer, radar animation, geolocation and i18n.

import { currentLocale, formatDate, formatDateShort, formatTime, initI18n, t, toggleLang } from "./i18n.js";
import { initGeo } from "./geo.js";
import { initRadar } from "./radar.js";
import { initLightning } from "./lightning.js";
import { initAbout } from "./about.js";
import { initClip } from "./clip.js";
import { initAlerts } from "./alerts.js";
import { initDateSheet } from "./datesheet.js";
import { initSettings } from "./settings.js";
import { initOneFingerZoom } from "./onefingerzoom.js";

const OSM_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTR =
  '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';

let toastTimer = null;
function showToast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.dataset.show = "true";
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.dataset.show = "false";
  }, 3500);
}

function updateLangToggle() {
  // Segmented FR|EN control: highlight the active locale (both stay visible).
  const btn = document.getElementById("lang-toggle");
  const active = currentLocale();
  btn.querySelectorAll("span[data-lang]").forEach((span) => {
    span.classList.toggle("cur", span.dataset.lang === active);
  });
}

async function main() {
  await initI18n();
  // app.title is the short label in the <h1>; app.document_title is the long,
  // search-facing one. They are separate keys because this assignment overwrites
  // whatever index.html shipped — and the rendered DOM is what crawlers read, so
  // reusing the short label here would silently undo the served <title>.
  document.title = t("app.document_title");

  const map = L.map("map", { maxZoom: 12 }).setView([46.6, 2.5], 6); // France center

  // crossOrigin makes the base tiles CORS-clean so the video export can read
  // them back off a canvas; the capture reuses the same HTTP-cache entries (no extra
  // OSM requests), respecting OSM's tile policy.
  L.tileLayer(OSM_URL, { attribution: OSM_ATTR, maxZoom: 19, crossOrigin: "anonymous" }).addTo(map);

  // One-finger zoom shortcut (double-tap, hold, slide). Additive and touch-only:
  // it recognises the gesture ahead of Leaflet's own handlers and stands aside for
  // everything else, so pan, pinch and plain double-tap zoom are unchanged.
  initOneFingerZoom(map);

  // The active provider's mandatory credit (HTML w/ link) comes from the
  // backend and is fed verbatim into Leaflet's attribution control. The
  // lightning layer adds/removes the Blitzortung credit the same way.
  const onAttribution = (html) => map.attributionControl.addAttribution(html);
  const onAttributionRemove = (html) => map.attributionControl.removeAttribution(html);

  // Forward-declared so radar's onState can reach the lightning controller, which
  // is created just after radar (it needs radar's bootstrap "lightning" advert).
  let lightning = null;
  const radar = await initRadar(map, {
    formatTime,
    formatDateShort,
    t,
    showToast,
    onAttribution,
    onAttributionRemove,
    onState: (state) => lightning && lightning.onRadarState(state),
  });

  // Lightning overlay. Hidden entirely when the backend reports it disabled.
  lightning = initLightning(map, {
    t,
    onAttribution,
    onAttributionRemove,
    config: radar.lightning,
  });
  lightning.onRadarState(radar.getState()); // sync to the current radar mode/cursor

  // About dialog: opened from the app title, independent of radar/lightning.
  const about = initAbout({ t, formatDate, formatTime, currentLocale });

  // Client-side video export: composites the current view + radar (+ lightning)
  // into a short H.264 MP4, then shares/downloads it. Purely additive — a failed
  // export only toasts and never touches radar/lightning.
  const clip = initClip({ map, t, showToast, formatTime, formatDate, radar, lightning });
  document.getElementById("clip-btn").addEventListener("click", clip.export);

  document.getElementById("play-btn").addEventListener("click", radar.toggle);
  document.getElementById("next-btn").addEventListener("click", radar.next);
  document.getElementById("prev-btn").addEventListener("click", radar.prev);

  // Archive seek: date-navigation sheet (calendar + quick jumps + day-step) +
  // LIVE button (replaces the old inline
  // date/time/Go row, which vanished on mobile portrait for lack of space).
  const dateNav = initDateSheet({ currentLocale, radar });
  document.getElementById("live-btn").addEventListener("click", () => radar.enterLive());

  const geo = initGeo(map, { t, showToast });

  // Storm proximity alerts: foreground notifications when lightning strikes
  // near a user-chosen anchor. Purely additive — hidden when the backend doesn't
  // advertise lightning; a failure here only toasts, never touching radar/lightning.
  const alerts = initAlerts(map, { t, showToast, geo, config: radar.lightning, currentLocale });

  // Display settings (gear on the tool rail): rain-tile opacity + warning-ring
  // visibility. All static labels are [data-i18n], so no refreshI18n hook needed.
  initSettings({ radar, alerts, lightningEnabled: !!(radar.lightning && radar.lightning.enabled) });

  document.getElementById("lang-toggle").addEventListener("click", async () => {
    await toggleLang();
    updateLangToggle();
    lightning.refreshI18n(); // re-translate the legend + toggle (built in JS)
    about.refreshI18n(); // re-translate the dialog's JS-built prose + stats
    dateNav.refreshI18n(); // re-render the calendar's locale month/weekday labels
    alerts.refreshI18n(); // re-translate the alert button title/aria
  });
  updateLangToggle();

  // PWA: register the service worker for an installable + offline-shell app.
  // Strictly progressive enhancement — a missing SW or a failed registration leaves
  // the app fully functional, just without install or offline support.
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => registerServiceWorker(clip));
  }
}

// Register the root-scope service worker and wire silent auto-update: on a new
// deploy the new SW skipWaiting()s + claims clients, firing `controllerchange`, and
// we reload — but only on an UPDATE (a controller already existed), never on the
// first-ever install, and never while a video export is mid-render.
function registerServiceWorker(clip) {
  let reloading = false;
  navigator.serviceWorker.register("/sw.js", { scope: "/" })
    .then(() => {
      const hadController = !!navigator.serviceWorker.controller;
      navigator.serviceWorker.addEventListener("controllerchange", () => {
        if (!hadController || reloading) return;
        maybeReload(clip, () => {
          reloading = true;
        });
      });
    })
    .catch(() => {
      /* PWA is progressive enhancement; ignore registration failure */
    });
}

// Reload to the new version, deferring while a video export (M5) is rendering so a
// half-rendered share is never killed. Falls through to an immediate reload if clip
// doesn't expose isExporting() (safe default).
function maybeReload(clip, markReloading) {
  if (clip && typeof clip.isExporting === "function" && clip.isExporting()) {
    const timer = setInterval(() => {
      if (!clip.isExporting()) {
        clearInterval(timer);
        markReloading();
        location.reload();
      }
    }, 1000);
    return;
  }
  markReloading();
  location.reload();
}

main();
