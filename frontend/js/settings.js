// Display settings: a gear on the tool rail toggles a compact NON-modal popover —
// the map stays visible underneath, so the rain-opacity slider previews live while
// dragging (a modal sheet would hide exactly what the user is adjusting). Two
// persisted, purely cosmetic preferences:
//   - rain-tile opacity   (radar.js owns the "radar_opacity" key; applied via
//                          radar.setOpacity, and picked up by the video export)
//   - alert warning rings (alerts.js owns the "alert_rings" key; applied via
//                          alerts.setRingsVisible — alerts keep firing either way)
// Purely additive: a failure here can never touch radar, lightning or alerts.

export function initSettings({ radar, alerts, lightningEnabled }) {
  const btn = document.getElementById("settings-btn");
  const panel = document.getElementById("settings-panel");
  const slider = document.getElementById("setting-opacity");
  const valueEl = document.getElementById("setting-opacity-value");
  const ringsRow = document.getElementById("setting-rings-row");
  const ringsInput = document.getElementById("setting-rings");
  if (!btn || !panel) return;

  // -- rain opacity -----------------------------------------------------------

  function renderValue(pct) {
    if (valueEl) valueEl.textContent = `${pct} %`;
  }

  if (slider) {
    const pct = Math.round(radar.getOpacity() * 100);
    slider.value = String(pct);
    renderValue(pct);
    slider.addEventListener("input", () => {
      const pct2 = Number(slider.value);
      radar.setOpacity(pct2 / 100); // applies to the visible layer + persists
      renderValue(pct2);
    });
  }

  // -- radar source ------------------------------------------------------------
  // A radio per advertised provider (labels are proper nouns from the advert, not
  // translated). Shown only when ≥2 providers are advertised; radar.js owns the
  // "radar_provider" key and persists the choice inside setProvider.
  const sourceRow = document.getElementById("setting-source-row");
  const sourceRadios = document.getElementById("setting-source-radios");
  const providers = radar.getProviders ? radar.getProviders() : [];
  if (sourceRow && sourceRadios && providers.length >= 2) {
    const current = radar.getProvider();
    for (const p of providers) {
      const label = document.createElement("label");
      label.className = "settings-radio";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "radar-source";
      input.value = p.name;
      input.checked = p.name === current;
      input.addEventListener("change", () => {
        if (input.checked) radar.setProvider(p.name);
      });
      const span = document.createElement("span");
      span.textContent = p.label;
      label.append(input, span);
      sourceRadios.appendChild(label);
    }
    sourceRow.hidden = false;
  }

  // -- warning rings ------------------------------------------------------------
  // The row is hidden without the backend lightning advert, mirroring the bell
  // button's gating (the rings can't exist when storm alerts are unavailable).

  if (lightningEnabled && ringsRow && ringsInput) {
    ringsRow.hidden = false;
    ringsInput.checked = alerts.ringsVisible();
    ringsInput.addEventListener("change", () => alerts.setRingsVisible(ringsInput.checked));
  }

  // -- popover open/close -------------------------------------------------------

  function setOpen(open) {
    panel.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  btn.addEventListener("click", () => setOpen(panel.hidden));

  // Light dismiss: any press outside the panel and its gear closes it.
  document.addEventListener("pointerdown", (e) => {
    if (panel.hidden) return;
    if (panel.contains(e.target) || btn.contains(e.target)) return;
    setOpen(false);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.hidden) {
      setOpen(false);
      btn.focus();
    }
  });
}
