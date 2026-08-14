// Lightning overlay, scrubber-synced. Because our map has a radar timeline
// (unlike a static Keraunos snapshot), lightning is driven by the *radar cursor*:
// each radar frame at time T shows exactly the strikes from its own slice
// `(prevFrameT, T]`, so scrubbing or playing animates rain + lightning together in
// lockstep. Data comes ONLY from our backend (history + SSE) — never a third party.

const MARGIN = 8; // px cull margin around the canvas
// Fallback slice length when a frame has no predecessor (first frame / cold range).
const SLICE_FALLBACK_S = 600; // ~ RainViewer frame cadence (10 min)

// Strike renderer: a cyan spark (+). `before` sets shared canvas state once per
// redraw, then `draw(ctx, x, y)` paints one strike. Goal: stand out over the
// radar ramp (blue→green→yellow→red) AND the pale base map.
const STYLE = {
  before(ctx) {
    ctx.shadowColor = "rgba(0,40,60,0.6)";
    ctx.shadowBlur = 3;
    ctx.strokeStyle = "#00e5ff";
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
  },
  draw(ctx, x, y) {
    const r = 3.6;
    ctx.beginPath();
    ctx.moveTo(x - r, y);
    ctx.lineTo(x + r, y);
    ctx.moveTo(x, y - r);
    ctx.lineTo(x, y + r);
    ctx.stroke();
  },
};

// A canvas pinned to the map's overlay pane. Dots stay glued during a pan because
// the canvas rides the pane's translate; we redraw on move/zoom end, on a cursor
// change, and on a new live strike.
function makeCanvasLayer(L) {
  return L.Layer.extend({
    onAdd(map) {
      this._map = map;
      const canvas = L.DomUtil.create("canvas", "lightning-canvas");
      this._canvas = canvas;
      map.getPanes().overlayPane.appendChild(canvas);
      map.on("moveend zoomend resize viewreset", this._reset, this);
      this._reset();
    },
    onRemove(map) {
      map.off("moveend zoomend resize viewreset", this._reset, this);
      L.DomUtil.remove(this._canvas);
      this._canvas = null;
    },
    setDraw(fn) {
      this._drawFn = fn;
    },
    redraw() {
      if (this._canvas) this._render();
    },
    _reset() {
      if (!this._canvas) return;
      const size = this._map.getSize();
      this._canvas.width = size.x;
      this._canvas.height = size.y;
      L.DomUtil.setPosition(this._canvas, this._map.containerPointToLayerPoint([0, 0]));
      this._render();
    },
    _render() {
      const ctx = this._canvas.getContext("2d");
      ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);
      if (this._drawFn) this._drawFn(ctx, this._map);
    },
  });
}

export function initLightning(map, { t, onAttribution, onAttributionRemove, config }) {
  const btn = document.getElementById("lightning-btn");
  const enabled = !!(config && config.enabled);

  // Backend says the layer is off: hide the controls entirely and return a no-op
  // controller so the rest of the app needn't special-case it.
  if (!enabled) {
    if (btn) btn.hidden = true;
    return { onRadarState() {}, refreshI18n() {}, getExportLayer: () => null };
  }

  const bbox = config.bbox || [41.2, 51.5, -6.0, 9.7];
  const bboxParam = bbox.join(",");
  const attribution = config.attribution;

  let on = false;
  let mode = "live"; // 'live' | 'archive'
  let sliceStart = 0; // show strikes in (sliceStart, sliceEnd] = the current frame's slice
  let sliceEnd = 0;
  let pool = []; // {lat, lon, time, intensity} covering the loaded radar range
  let loadedFrom = null;
  let loadedTo = null;
  let es = null;
  let redrawQueued = false;
  let lastState = null;

  const CanvasLayer = makeCanvasLayer(L);
  const layer = new CanvasLayer();
  layer.setDraw(drawDots);

  // -- rendering --------------------------------------------------------------

  // Paint the strikes whose time falls in (start, end] onto an arbitrary 2D ctx
  // using map projection `m`. The live overlay calls this with the module's
  // sliceStart/sliceEnd; the clip exporter calls it per video frame with an
  // explicit window and its own composite canvas/context.
  function drawSlice(ctx, m, start, end) {
    const w = ctx.canvas.width;
    const h = ctx.canvas.height;
    ctx.save();
    STYLE.before(ctx); // set colour/glow once for the whole batch
    for (const s of pool) {
      if (s.time <= start || s.time > end) continue; // not this frame's slice
      const pt = m.latLngToContainerPoint([s.lat, s.lon]);
      if (pt.x < -MARGIN || pt.y < -MARGIN || pt.x > w + MARGIN || pt.y > h + MARGIN) continue;
      STYLE.draw(ctx, pt.x, pt.y);
    }
    ctx.restore();
  }

  function drawDots(ctx, m) {
    drawSlice(ctx, m, sliceStart, sliceEnd);
  }

  function scheduleRedraw() {
    if (redrawQueued) return;
    redrawQueued = true;
    requestAnimationFrame(() => {
      redrawQueued = false;
      layer.redraw();
    });
  }

  // -- data -------------------------------------------------------------------

  async function fetchHistory(from, to) {
    try {
      const resp = await fetch(
        `/api/lightning/history?from=${Math.floor(from)}&to=${Math.floor(to)}&bbox=${bboxParam}`,
      );
      if (!resp.ok) return [];
      const data = await resp.json();
      return data.strikes || [];
    } catch {
      return []; // history is best-effort; the layer simply stays sparse
    }
  }

  // Ensure the pool covers the radar's loaded frame range (+ one slice of lead-in).
  // Refetch only when that range actually changes (cheap scrubbing within it).
  async function ensurePool(rangeFrom, rangeTo) {
    if (rangeFrom == null || rangeTo == null) return;
    const from = rangeFrom - SLICE_FALLBACK_S;
    if (loadedFrom !== null && from >= loadedFrom && rangeTo <= loadedTo) return;
    loadedFrom = from;
    loadedTo = rangeTo;
    pool = await fetchHistory(from, rangeTo);
  }

  function openSSE() {
    closeSSE();
    es = new EventSource("/api/lightning/stream");
    es.addEventListener("strike", (e) => {
      try {
        const s = JSON.parse(e.data);
        pool.push(s); // appears once the radar cursor reaches its slice
        if (s.time > sliceStart && s.time <= sliceEnd) scheduleRedraw();
      } catch {
        /* ignore a malformed event; the next one is independent */
      }
    });
  }

  function closeSSE() {
    if (es) {
      es.close();
      es = null;
    }
  }

  // A hidden tab doesn't need live strikes: drop the stream (each one holds a
  // server connection + a Redis subscription) and reopen on return — the SSE
  // replay of the recent buffer plus the radar-refresh-driven ensurePool()
  // backfill what was missed. Re-replayed strikes may duplicate in the pool;
  // harmless (same point drawn twice). The storm-alert stream (alerts.js) is
  // separate and deliberately stays open — alerting in the background is its job.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      closeSSE();
    } else if (on && mode === "live" && lastState) {
      openSSE();
    }
  });

  // -- radar sync -------------------------------------------------------------

  // Called by the radar module whenever its mode or scrubber cursor changes.
  async function onRadarState(state) {
    lastState = state;
    if (!on || !state || state.cursorTs == null) return;
    mode = state.mode;
    // This frame's lightning slice = (previous frame, current frame].
    sliceEnd = state.cursorTs;
    sliceStart = state.prevTs != null ? state.prevTs : state.cursorTs - SLICE_FALLBACK_S;
    // Live tails new strikes via SSE; archive is purely historical.
    if (mode === "live") {
      if (!es) openSSE();
    } else {
      closeSSE();
    }
    await ensurePool(state.rangeFrom, state.rangeTo);
    scheduleRedraw();
  }

  // -- toggle + i18n ----------------------------------------------------------

  function updateButton() {
    if (!btn) return;
    btn.dataset.state = on ? "on" : "off";
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.title = t("control.lightning");
    btn.setAttribute("aria-label", t("control.lightning"));
  }

  async function enable() {
    on = true;
    updateButton();
    layer.addTo(map);
    if (onAttribution && attribution) onAttribution(attribution);
    if (lastState) await onRadarState(lastState); // render the current frame's slice
  }

  function disable() {
    on = false;
    updateButton();
    closeSSE();
    map.removeLayer(layer);
    if (onAttributionRemove && attribution) onAttributionRemove(attribution);
  }

  function toggle() {
    if (on) disable();
    else enable();
  }

  function refreshI18n() {
    updateButton();
  }

  // -- init -------------------------------------------------------------------

  if (btn) {
    btn.hidden = false;
    btn.addEventListener("click", toggle);
  }
  updateButton();
  enable(); // default ON when the backend advertises the layer

  // Read-only export hooks for the video clip. `drawSlice` reuses the live
  // map projection (valid because clip.js composites at the current viewport size
  // before final scaling); `ensureRange` widens the strike pool to the clip range.
  function getExportLayer() {
    return {
      active: on,
      attribution,
      ensureRange: (from, to) => ensurePool(from, to),
      drawSlice: (ctx, m, start, end) => drawSlice(ctx, m, start, end),
    };
  }

  return { onRadarState, refreshI18n, getExportLayer };
}
