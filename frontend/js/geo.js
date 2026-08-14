// Geolocation: button-triggered one-shot locate, blue dot + accuracy halo,
// auto-center once, and intent reconciliation on load.

const GEO_OPTS = { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 };
const DOT_STYLE = {
  radius: 6,
  color: "#1e66f5",
  weight: 2,
  fillColor: "#1e66f5",
  fillOpacity: 0.9,
};
const HALO_STYLE = {
  color: "#1e66f5",
  weight: 1,
  fillColor: "#1e66f5",
  fillOpacity: 0.12,
};

export function initGeo(map, { t, showToast }) {
  const btn = document.getElementById("locate-btn");
  let dot = null;
  let halo = null;
  let lastFix = null;
  let centeredOnce = false;

  function setState(state) {
    btn.dataset.state = state;
  }

  function placeFix(pos, { center }) {
    const { latitude: lat, longitude: lon, accuracy } = pos.coords;
    lastFix = [lat, lon];
    if (dot) {
      dot.setLatLng(lastFix);
      halo.setLatLng(lastFix).setRadius(accuracy);
    } else {
      halo = L.circle(lastFix, { ...HALO_STYLE, radius: accuracy }).addTo(map);
      dot = L.circleMarker(lastFix, DOT_STYLE).addTo(map);
    }
    if (center && !centeredOnce) {
      map.setView(lastFix, 9);
      centeredOnce = true;
    }
    setState("active");
    localStorage.setItem("geo_intent", "on");
  }

  // Remove the location marker + halo from the map (keeps the last coordinate so
  // other consumers, like the alert anchor, can still read it).
  function removeMarker() {
    if (dot) {
      map.removeLayer(dot);
      dot = null;
    }
    if (halo) {
      map.removeLayer(halo);
      halo = null;
    }
  }

  // Toggle off: hide the marker and return to idle, forgetting the auto-locate
  // intent so a reload won't re-prompt.
  function clearFix() {
    removeMarker();
    setState("idle");
    localStorage.setItem("geo_intent", "off");
  }

  // Reflect the browser's live geolocation-permission state on the button —
  // driven by the Permissions API, so blocking (or re-allowing) in the browser
  // updates the icon immediately, without needing a click. `autoLocate` is true
  // only on the initial reconcile when the user previously opted in.
  function applyPermission(state, autoLocate) {
    if (state === "denied") {
      removeMarker();
      setState("blocked");
      localStorage.setItem("geo_intent", "off");
    } else if (state === "granted") {
      if (autoLocate) locate({ center: true });
      else if (btn.dataset.state === "blocked") setState("idle");
    } else if (btn.dataset.state !== "active") {
      // "prompt": awaiting an explicit click (unless a fix is already shown).
      setState("idle");
    }
  }

  function handleError(err) {
    if (err.code === err.PERMISSION_DENIED) {
      setState("blocked");
      localStorage.setItem("geo_intent", "off");
      return;
    }
    if (err.code === err.TIMEOUT) {
      showToast(t("geo.timeout"));
    } else {
      showToast(t("geo.unavailable"));
    }
    // Keep the current (France) view on transient errors.
  }

  function locate({ center } = { center: true }) {
    if (!navigator.geolocation) {
      showToast(t("geo.unavailable"));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => placeFix(pos, { center }),
      handleError,
      GEO_OPTS,
    );
  }

  // One-shot fix for other consumers (the storm-alert anchor): same
  // locate flow (dot + halo placed, no recenter), but promise-shaped and silent —
  // messaging on failure is the caller's job, so nothing toasts twice.
  function requestFix() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) {
        reject(new Error("geolocation unsupported"));
        return;
      }
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          placeFix(pos, { center: false });
          resolve([pos.coords.latitude, pos.coords.longitude]);
        },
        (err) => {
          if (err.code === err.PERMISSION_DENIED) {
            setState("blocked");
            localStorage.setItem("geo_intent", "off");
          }
          reject(err);
        },
        GEO_OPTS,
      );
    });
  }

  btn.addEventListener("click", () => {
    // Toggle: when the location is already shown (blue/active), a click hides it
    // and returns to idle — consistent with the lightning/alert toggles that
    // share the rail and its blue "active" affordance.
    if (btn.dataset.state === "active") {
      clearFix();
      return;
    }
    // Otherwise (idle/blocked): if we still have a prior fix, snap to it
    // immediately for instant feedback, then refresh the position.
    if (lastFix) {
      map.setView(lastFix, Math.max(map.getZoom(), 9));
    }
    locate({ center: true });
  });

  reconcileIntent({ setState, applyPermission });

  return {
    getLastFix: () => lastFix, // [lat, lon] | null
    requestFix,
  };
}

// Reconcile persisted geo_intent with the live permission state, and keep the
// button in sync if the user changes the browser permission mid-session.
function reconcileIntent({ setState, applyPermission }) {
  const intent = localStorage.getItem("geo_intent");
  if (!navigator.permissions?.query) {
    // No Permissions API: show an idle button, never auto-prompt.
    setState("idle");
    return;
  }
  navigator.permissions
    .query({ name: "geolocation" })
    .then((status) => {
      // Initial: honour the persisted opt-in; then reflect every later change
      // (block/allow from the browser) without auto-locating on its own.
      applyPermission(status.state, intent === "on");
      status.addEventListener("change", () => applyPermission(status.state, false));
    })
    .catch(() => setState("idle"));
}
