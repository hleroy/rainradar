// Radar frames + tile-layer animation. Tiles come from OUR backend
// (/tiles/{provider}/{date}/{ts}/{z}/{x}/{y}.png — Nginx-static in prod, Django
// fallback), never from RainViewer or Météo-France directly.

const TILE_OPTS = { tileSize: 256, opacity: 0, maxNativeZoom: 7, maxZoom: 12, pane: "radar" };
const FRAME_MS = 500; // play speed
const LIVE_OPACITY = 0.8; // default; the settings popover can override (persisted)
const OPACITY_MIN = 0.2; // a fully invisible radar would read as "broken"
const DAY_SECONDS = 86400;
// Fallback cadence before the providers advert loads (RainViewer's 600 s); once it
// arrives, both the gap tolerance and the refresh anchor follow the active provider's
// frame_interval — the client hardcodes no frame cadence of its own.
const DEFAULT_FRAME_INTERVAL_S = 600;
const DEFAULT_GAP_TOLERANCE_S = 1.5 * DEFAULT_FRAME_INTERVAL_S;

// Live-refresh scheduling. A frame is published some seconds after the timestamp it
// carries (measured in production: ~140–217 s Météo-France, ~94–339 s RainViewer), so
// the refresh is anchored on `newest_ts + frame_interval + lag`, not on a fixed period —
// a fixed period aliases against the cadence and can sit a whole frame behind forever.
// `lag` tracks the observed publication delay: what we measure is an upper bound on the
// true delay, so it decays a little each cycle to probe earlier, and a miss (which costs
// one short retry) ratchets it straight back up. That converges per provider without a
// single provider-specific constant.
const LAG_INITIAL_S = 150; // inside both providers' measured bands
const LAG_MIN_S = 60;
const LAG_MAX_S = 420; // also bounds the retry chase: past this we stop hunting
const LAG_DECAY_S = 30; // ≥ mean jitter, so the estimate can't ratchet upward forever
const JITTER_S = 30; // every client anchors off the SAME newest_ts — spread the herd
const RETRY_S = 30; // short retries while the expected frame hasn't landed yet
const MIN_DELAY_S = 15; // = FRAMES_LIVE_CACHE_TTL; never poll inside the micro-cache
const BACKOFF_MIN_CEILING_MS = 10 * 60 * 1000; // a view labelled DIRECT may not stall longer

// Wall clock in epoch seconds, to compare against frame timestamps.
const nowS = () => Math.floor(Date.now() / 1000);

// UTC calendar date of an epoch-seconds ts (matches storage.utc_date).
const utcDate = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);

// Per-browser choice of radar source; validated against the advert.
function loadStoredProvider() {
  try {
    return localStorage.getItem("radar_provider") || null;
  } catch {
    return null;
  }
}

// User-set rain-tile opacity (display-settings popover). Read here, at init, so
// the very first reveal already uses it — no flash of the default.
function loadStoredOpacity() {
  try {
    const v = Number(localStorage.getItem("radar_opacity"));
    if (v >= OPACITY_MIN && v <= 1) return v;
  } catch {
    /* storage blocked → default */
  }
  return LIVE_OPACITY;
}

export async function initRadar(
  map,
  { formatTime, formatDateShort, t, showToast, onAttribution, onAttributionRemove, onState },
) {
  let mode = "live"; // 'live' | 'archive'
  // Is the cursor pinned to the newest frame? LIVE means "the cursor tracks the
  // live edge", not merely "the live window is loaded" — a user who scrubbed back
  // is not live, and the pill has to say so (it is the way back).
  let following = true;
  let frames = [];
  let gaps = [];
  let lightningConfig = null; // the /api/radar/frames "lightning" advert
  let attribution = null; // the active provider's mandatory credit (HTML), for the clip caption
  let provider = loadStoredProvider(); // active radar source; adopted from the advert if null
  let providers = []; // the /api/radar/frames "providers" advert
  let gapToleranceS = DEFAULT_GAP_TOLERANCE_S; // 1.5 × active provider frame_interval
  let archiveDate = null; // remembered archive window (for re-query on a provider switch)
  let archiveTime = null;

  // The active source's cadence, straight from the advert — the single place the
  // frontend learns a frame interval. Everything cadence-shaped (gap tolerance, the
  // refresh anchor, the backoff ceiling) derives from this.
  function frameIntervalS(name = provider) {
    const entry = providers.find((p) => p.name === name);
    return entry ? entry.frame_interval : DEFAULT_FRAME_INTERVAL_S;
  }

  // Tiles come from our provider-scoped path; the closure captures the active
  // `provider`, so the clip export (which reuses this fn) follows the switch too.
  const tileUrl = (ts) => `/tiles/${provider}/${utcDate(ts)}/${ts}/{z}/{x}/{y}.png`;

  // Tell a listener (the lightning overlay) the current mode, the cursor frame, the
  // previous frame (so it can show exactly *this* frame's lightning slice), and the
  // loaded frame range (so it can fetch the matching strikes). Radar is unaffected.
  function radarState() {
    return {
      mode,
      cursorTs: frames[position]?.timestamp,
      prevTs: frames[position - 1]?.timestamp,
      rangeFrom: frames[0]?.timestamp,
      rangeTo: frames[frames.length - 1]?.timestamp,
    };
  }
  function notifyState() {
    if (onState) onState(radarState());
  }
  let radarBounds = null; // L.LatLngBounds of the radar coverage (from the API)
  let boundsRect = null; // L.Rectangle outlining that coverage on the map
  let position = 0;
  let playing = false;
  let radarOpacity = loadStoredOpacity();
  let timer = null;
  let refreshTimer = null;
  let currentLayer = null;
  // Keyed by frame *timestamp*, not scrubber position: a live refresh replaces the
  // frame list and every position shifts under it, while a timestamp is the frame's
  // identity. That is what lets a refresh keep the layer the user is looking at
  // (see retainCurrentLayer) instead of tearing the whole cache down.
  const layerCache = new Map(); // frame timestamp -> L.TileLayer

  // Dedicated pane for radar tiles so we can clip them to the bbox without
  // touching the base map. Sits above the OSM tiles (zIndex 200) but below the
  // overlay/vector pane (400), so the coverage outline draws on top, unclipped.
  const radarPane = map.createPane("radar");
  radarPane.style.zIndex = 250;

  // Clip the radar pane to the bbox. A lat/lon box is axis-aligned in Web
  // Mercator, so its four corners form a screen-space rectangle. We express the
  // polygon in layer-point coordinates (the pane's own coordinate space), so it
  // stays glued to the geography across pan/zoom; recompute when the origin or
  // size changes.
  function updateRadarClip() {
    if (!radarBounds) return;
    const n = radarBounds.getNorth();
    const s = radarBounds.getSouth();
    const w = radarBounds.getWest();
    const e = radarBounds.getEast();
    const nw = map.latLngToLayerPoint([n, w]);
    const ne = map.latLngToLayerPoint([n, e]);
    const se = map.latLngToLayerPoint([s, e]);
    const sw = map.latLngToLayerPoint([s, w]);
    radarPane.style.clipPath =
      `polygon(${nw.x}px ${nw.y}px, ${ne.x}px ${ne.y}px, ` +
      `${se.x}px ${se.y}px, ${sw.x}px ${sw.y}px)`;
  }
  map.on("move zoom viewreset zoomend resize", updateRadarClip);

  const tsEl = document.getElementById("timestamp");
  const playBtn = document.getElementById("play-btn");
  const playIconUse = playBtn.querySelector("use");
  const liveBtn = document.getElementById("live-btn");
  const scrubber = document.getElementById("scrubber");
  const gapOverlay = document.getElementById("gap-overlay");
  const nodataEl = document.getElementById("nodata");
  const dateLabel = document.getElementById("date-label");
  const scrubStart = document.getElementById("scrub-start");
  const scrubEnd = document.getElementById("scrub-end");
  const scrubBubble = document.getElementById("scrub-bubble");

  function buildLayer(ts) {
    const opts = { ...TILE_OPTS };
    // Bound to the radar coverage so Leaflet never requests out-of-matrix tiles
    // (which only 404 against our backend) for the surrounding viewport.
    if (radarBounds) opts.bounds = radarBounds;
    return L.tileLayer(tileUrl(ts), opts);
  }

  function updateTimestamp() {
    const f = frames[position];
    if (!f) return;
    tsEl.textContent = formatTime(f.timestamp);
    if (mode === "archive" && dateLabel) dateLabel.textContent = formatDateShort(f.timestamp);
  }

  // Paint the played portion of the track (WebKit gradient; Firefox has
  // ::-moz-range-progress) and keep the drag bubble glued to the thumb.
  function updateScrubberVisual() {
    if (!scrubber) return;
    const max = Number(scrubber.max) || 0;
    const pct = max > 0 ? (Number(scrubber.value) / max) * 100 : 100;
    scrubber.style.setProperty("--scrub-pos", `${pct}%`);
    if (scrubBubble && !scrubBubble.hidden) {
      const f = frames[position];
      if (f) scrubBubble.textContent = formatTime(f.timestamp);
      // Clamped so the bubble never pokes out of the bar at either end.
      scrubBubble.style.left = `clamp(20px, ${pct}%, calc(100% - 20px))`;
    }
  }

  // Window end labels answer "what am I scrubbing through?" — the live ~2 h
  // window or an archive day.
  function renderWindowLabels() {
    if (!scrubStart || !scrubEnd) return;
    if (frames.length === 0) {
      scrubStart.textContent = "";
      scrubEnd.textContent = "";
      return;
    }
    scrubStart.textContent = formatTime(frames[0].timestamp);
    scrubEnd.textContent = formatTime(frames[frames.length - 1].timestamp);
  }

  // A frame "lands in a gap" when no neighbouring frame is within tolerance —
  // i.e. the surrounding cadence is broken.
  function inGap(pos) {
    const f = frames[pos];
    if (!f) return true;
    const prev = frames[pos - 1];
    const next = frames[pos + 1];
    const lonely =
      (!prev || f.timestamp - prev.timestamp > gapToleranceS) &&
      (!next || next.timestamp - f.timestamp > gapToleranceS);
    // Gap ranges are half-open: `start` is the first missing slot, `end` is the
    // first frame that came back (the archiver sets gap_end = oldest_up). Comparing
    // `end` inclusively would flag that recovered frame — which we archived and
    // can render — as "no data", the whole timeline when it is the only frame.
    const insideGap = gaps.some((g) => f.timestamp >= g.start && (g.end == null || f.timestamp < g.end));
    return insideGap || lonely;
  }

  function showNodata(show) {
    if (!nodataEl) return;
    nodataEl.dataset.show = show ? "true" : "false";
    if (show) nodataEl.textContent = t("gap.nodata");
  }

  // Hide every cached layer except `keep` (null = hide all). Visibility is always
  // recomputed from the current state — never chained through per-layer "previous
  // frame" closures, which break when tile loads finish out of order (fast
  // scrubbing) and leave stale frames stacked at full opacity.
  function hideAllExcept(keep) {
    for (const [, layer] of layerCache) {
      if (layer !== keep) layer.setOpacity(0);
    }
  }

  function clearRadarLayer() {
    currentLayer = null; // defuses any in-flight reveal (see below)
    hideAllExcept(null);
  }

  // Raise `layer` to visible and sweep the rest — but only if it is still the
  // frame under the cursor. A slow frame the scrubber already left is a no-op.
  function reveal(layer) {
    if (currentLayer !== layer) return;
    layer.setOpacity(radarOpacity);
    hideAllExcept(layer);
  }

  // Warm a small window around the cursor (wrapping, like playback) so the next
  // frames cross-fade instantly. Preloaded layers sit at opacity 0 and stay
  // invisible until reveal() makes one current, so this can never paint stale rain.
  const PRELOAD_AHEAD = 3;
  const PRELOAD_BEHIND = 1;
  function preloadAround() {
    if (frames.length < 2) return;
    for (let off = -PRELOAD_BEHIND; off <= PRELOAD_AHEAD; off++) {
      if (off === 0) continue;
      const p = (((position + off) % frames.length) + frames.length) % frames.length;
      if (p === position || inGap(p)) continue;
      const ts = frames[p].timestamp;
      if (layerCache.has(ts)) continue;
      const layer = buildLayer(ts);
      layerCache.set(ts, layer);
      layer.addTo(map);
    }
  }

  // Cross-fade: the new layer starts (or was preloaded) at opacity 0; once its
  // tiles are in, raise it to the user opacity and hide every other layer (avoids
  // flicker). The previous frame stays visible until then.
  function showFrame(pos) {
    if (frames.length === 0) {
      showNodata(true);
      return;
    }
    position = ((pos % frames.length) + frames.length) % frames.length;
    if (scrubber) scrubber.value = String(position);
    updateTimestamp();
    updateScrubberVisual();
    notifyState(); // keep the lightning age reference in step with the cursor

    if (inGap(position)) {
      showNodata(true);
      clearRadarLayer(); // no stale tiles over a gap
      preloadAround();
      return;
    }
    showNodata(false);

    const ts = frames[position].timestamp;
    let layer = layerCache.get(ts);
    if (!layer) {
      layer = buildLayer(ts);
      layerCache.set(ts, layer);
    }
    if (!map.hasLayer(layer)) layer.addTo(map);
    currentLayer = layer;
    if (layer.isLoading()) {
      layer.once("load", () => reveal(layer));
    } else {
      reveal(layer); // tiles already in (preloaded/revisited layer)
    }
    preloadAround();
  }

  function setPlayGlyph() {
    if (playIconUse) playIconUse.setAttribute("href", playing ? "#i-pause" : "#i-play");
    const label = playing ? t("control.pause") : t("control.play");
    playBtn.title = label;
    playBtn.setAttribute("aria-label", label);
  }

  function tick() {
    showFrame(position + 1); // wraps at ends
    timer = setTimeout(tick, FRAME_MS);
  }

  function play() {
    if (playing || frames.length === 0) return;
    playing = true;
    setPlayGlyph();
    timer = setTimeout(tick, FRAME_MS);
  }

  function pause() {
    playing = false;
    setPlayGlyph();
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function toggle() {
    if (playing) {
      pause();
      // Stopping the replay mid-window makes the cursor a deliberate position, so
      // it stops being the live edge — the same rule as scrubbing or stepping.
      // (Letting the replay run keeps following: each new frame moves it forward.)
      syncFollowing();
    } else {
      play();
    }
  }

  // Memory hygiene: drop non-current cached layers when the user pans/zooms.
  map.on("movestart", () => {
    const keep = frames[position]?.timestamp;
    for (const [ts, layer] of layerCache) {
      if (ts !== keep) {
        map.removeLayer(layer);
        layerCache.delete(ts);
      }
    }
  });

  function clearLayers() {
    for (const [, layer] of layerCache) map.removeLayer(layer);
    layerCache.clear();
    currentLayer = null;
  }

  // Retire every cached layer EXCEPT the one on screen — the live refresh's
  // teardown. clearLayers() would take the visible frame down with the rest, and
  // since showFrame only reveals a layer once its tiles are in, that left the map
  // with no rain at all for the whole load: seconds, whenever a tile misses the
  // static cache and falls through to the app. Keeping the current layer means the
  // previous frame stays up until reveal() cross-fades the new one over it, exactly
  // like a scrub. The survivor stays keyed by its own timestamp, so preloadAround()
  // can reuse it and movestart/the next refresh still retire it.
  function retainCurrentLayer() {
    for (const [ts, layer] of layerCache) {
      if (layer === currentLayer) continue;
      map.removeLayer(layer);
      layerCache.delete(ts);
    }
  }

  // Shade gap segments over the scrubber track, mapped onto the loaded range.
  function renderGaps() {
    if (!gapOverlay) return;
    gapOverlay.replaceChildren();
    if (frames.length < 2 || gaps.length === 0) return;
    const lo = frames[0].timestamp;
    const hi = frames[frames.length - 1].timestamp;
    const span = hi - lo;
    if (span <= 0) return;
    for (const g of gaps) {
      const gStart = Math.max(g.start, lo);
      const gEnd = Math.min(g.end == null ? hi : g.end, hi);
      if (gEnd <= gStart) continue;
      const left = ((gStart - lo) / span) * 100;
      const width = ((gEnd - gStart) / span) * 100;
      const seg = document.createElement("div");
      seg.className = "gap-seg";
      seg.style.left = `${left}%`;
      seg.style.width = `${Math.max(width, 0.5)}%`;
      seg.title = t("gap.nodata");
      gapOverlay.appendChild(seg);
    }
  }

  function setScrubberRange() {
    if (!scrubber) return;
    scrubber.min = "0";
    scrubber.max = String(Math.max(frames.length - 1, 0));
    scrubber.value = String(position);
    scrubber.disabled = frames.length === 0;
    updateScrubberVisual();
    renderWindowLabels();
  }

  function applyData(data) {
    frames = (data && data.frames) || [];
    gaps = (data && data.gaps) || [];
    if (data && data.lightning) lightningConfig = data.lightning;
    if (data && Array.isArray(data.providers)) providers = data.providers;
    // Adopt the response's serving provider when we have none stored (or it was cleaned).
    if (!provider && data && data.provider) provider = data.provider;
    // Gap tolerance + timeline density follow the active provider's cadence.
    gapToleranceS = 1.5 * frameIntervalS();
    // [S, N, W, E] -> Leaflet bounds [[S, W], [N, E]].
    if (data && Array.isArray(data.bbox) && data.bbox.length === 4) {
      const [s, n, w, e] = data.bbox;
      radarBounds = L.latLngBounds([
        [s, w],
        [n, e],
      ]);
      // Outline the radar coverage area once (non-interactive, no fill).
      if (!boundsRect) {
        boundsRect = L.rectangle(radarBounds, {
          color: "#1f2937",
          weight: 1.5,
          dashArray: "6 4",
          fill: false,
          interactive: false,
        }).addTo(map);
      }
      // Clip the radar pane to the freshly known bbox.
      updateRadarClip();
    }
    clearLayers();
    setScrubberRange();
    renderGaps();
  }

  function clearStoredProvider() {
    try {
      localStorage.removeItem("radar_provider");
    } catch {
      /* storage blocked — nothing to clear */
    }
  }

  function framesUrl(params) {
    const q = new URLSearchParams();
    if (provider) q.set("provider", provider);
    if (params && params.from != null) q.set("from", String(params.from));
    if (params && params.to != null) q.set("to", String(params.to));
    const qs = q.toString();
    return `/api/radar/frames${qs ? `?${qs}` : ""}`;
  }

  async function fetchFrames(params) {
    let resp = await fetch(framesUrl(params));
    // A stale localStorage provider the backend no longer serves ⇒ 400. Drop it and
    // retry with the default source so a disabled provider never bricks the load.
    if (resp.status === 400 && provider) {
      clearStoredProvider();
      provider = null;
      resp = await fetch(framesUrl(params));
    }
    if (!resp.ok) return { ok: false, status: resp.status };
    return { ok: true, data: await resp.json() };
  }

  // Localized message for a failed frames fetch. A 429 (the server shedding
  // load) gets its own explanation so a throttled user knows why — and that
  // retrying shortly will work — rather than a generic "unavailable".
  function framesErrorKey(status, archive = false) {
    if (status === 429) return "error.rate_limited";
    if (archive && status === 503) return "error.archive_unavailable";
    return "error.frames_unavailable";
  }

  // Periodic refresh (live only): jump to a newer frame when one appears.
  // Self-scheduled (never a fixed interval) so the next attempt can be anchored on the
  // frame cadence, a 429/503 backs off exponentially instead of keeping the cadence
  // against a struggling server, and a hidden tab skips the network entirely (it
  // catches up on visibilitychange).
  let lagS = LAG_INITIAL_S; // tracked publication delay of the active provider
  let backoffMs = 0; // 0 while healthy; set only when the server sheds load

  async function refresh() {
    if (mode !== "live" || document.hidden) return;
    let res;
    try {
      res = await fetchFrames();
    } catch {
      // A network-level rejection (offline blip) is a failed fetch, not an escaping
      // exception — letting it out of the timeout callback would kill the self-
      // scheduling chain for the life of the page.
      res = { ok: false, status: 0 };
    }
    if (!res.ok) {
      const ceiling = Math.max(frameIntervalS() * 1000, BACKOFF_MIN_CEILING_MS);
      backoffMs = backoffMs ? Math.min(backoffMs * 2, ceiling) : frameIntervalS() * 1000;
      return;
    }
    backoffMs = 0;
    const next = res.data.frames || [];
    if (next.length === 0) return;
    const newest = next[next.length - 1].timestamp;
    const prevNewest = frames.length ? frames[frames.length - 1].timestamp : null;
    if (newest === prevNewest) return;
    // A frame landed: what we just measured is an upper bound on this provider's real
    // publication delay (we only look at discrete moments), so decay it a little before
    // storing. Too-early wakes cost one RETRY_S hop and push the estimate back up.
    lagS = Math.min(LAG_MAX_S, Math.max(LAG_MIN_S, nowS() - newest - LAG_DECAY_S));

    const shownTs = frames[position]?.timestamp;
    gaps = res.data.gaps || [];
    frames = next;
    retainCurrentLayer(); // keep the picture up until the new frame is ready
    setScrubberRange();
    renderGaps();
    // Following the live edge (the default state, and what the blinking pill
    // promises) ⇒ move to the frame that just landed. Without this the paused
    // default pins the cursor to whatever frame was showing when the page loaded:
    // the index keeps refreshing, the window-end label marches on, and the picture
    // silently rots for up to the whole 2 h window. Deliberately scrubbed back ⇒
    // stay on the chosen frame until it ages out of the window, then rejoin.
    if (following) {
      showFrame(frames.length - 1);
      return;
    }
    const idx = frames.findIndex((f) => f.timestamp === shownTs);
    if (idx >= 0) {
      showFrame(idx);
      return;
    }
    setFollowing(true);
    showFrame(frames.length - 1);
  }

  // When to look again. Anchored on the newest frame's OWN timestamp rather than on
  // "now + a period": a period equal to (or a multiple of) the cadence aliases against
  // it, and a client that lands just before each publication stays a whole frame behind
  // indefinitely. Jitter is load-bearing, not cosmetic — every client anchors off the
  // same newest_ts, so without it they would all fetch on the same second.
  function nextDelayMs() {
    const intervalS = frameIntervalS();
    const jitter = Math.random() * JITTER_S;
    const spaced = (intervalS + jitter) * 1000;
    if (backoffMs) return backoffMs;
    // No network happens in these states, so there is nothing to anchor to.
    if (document.hidden || mode !== "live" || frames.length === 0) return spaced;
    const now = nowS();
    const newest = frames[frames.length - 1].timestamp;
    // Past the plausible publication window the frame is not merely late — the provider
    // has stalled or the archive has a gap. Stop hunting and fall back to cadence-spaced
    // polling, so a stall can never become a permanent RETRY_S poll.
    if (now > newest + intervalS + LAG_MAX_S) return spaced;
    // Anchor still ahead ⇒ sleep until it; already past ⇒ short retries until it lands.
    const due = newest + intervalS + lagS;
    const delayS = now < due ? due - now + jitter : RETRY_S + jitter;
    return Math.max(delayS, MIN_DELAY_S) * 1000;
  }

  function scheduleRefresh() {
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
      await refresh();
      scheduleRefresh();
    }, nextDelayMs());
  }

  // Re-anchor from the current state and clear any backoff. Entering LIVE is a "give
  // me the current picture" gesture, so a client that had backed off to the ceiling
  // must not stay parked there after the user asks for live.
  function resetRefresh() {
    backoffMs = 0;
    scheduleRefresh();
  }

  // Returning to the tab: clear the backoff and catch up immediately (the hidden-tab
  // refreshes were skipped), then re-anchor. Coming back to the tab is the same "give
  // me the current picture" gesture as the LIVE button, and clearing only implicitly
  // (on a successful fetch) leaves a client parked at the ceiling when the catch-up
  // itself fails on a flaky network.
  document.addEventListener("visibilitychange", async () => {
    if (document.hidden || mode !== "live" || !refreshTimer) return;
    backoffMs = 0;
    await refresh();
    scheduleRefresh();
  });

  function setLiveButton(active) {
    if (!liveBtn) return;
    liveBtn.dataset.mode = active ? "live" : "archive";
    // The pill is a toggle button, and "am I actually live?" is a state a screen
    // reader must hear too — the blinking dot alone only reaches sighted users.
    liveBtn.setAttribute("aria-pressed", active ? "true" : "false");
  }

  // Single writer for the live-edge state, so the pill can never disagree with it.
  function setFollowing(v) {
    following = v;
    setLiveButton(mode === "live" && following);
  }

  // After a deliberate cursor move (scrub, step): landing on the newest frame
  // re-arms following — dragging the scrubber to the far right is a legitimate way
  // back to live. With no frames loaded there is no edge to be at, so leave it be.
  function syncFollowing() {
    if (frames.length === 0) return;
    setFollowing(position === frames.length - 1);
  }

  // -- public mode switches ---------------------------------------------------

  async function enterLive() {
    mode = "live";
    setFollowing(true); // back to the edge, whatever the cursor was on
    notifyState(); // propagate the mode switch even before frames load
    if (dateLabel) dateLabel.textContent = "";
    pause();
    const res = await fetchFrames();
    if (!res.ok) {
      showToast(t(framesErrorKey(res.status)));
      return;
    }
    applyData(res.data);
    if (frames.length) showFrame(frames.length - 1); // newest on load
    resetRefresh();
  }

  async function enterArchive(dateStr, timeStr) {
    if (!dateStr) return;
    mode = "archive";
    setFollowing(false);
    notifyState(); // propagate the mode switch even before frames load
    pause();
    const dayStart = Math.floor(Date.parse(`${dateStr}T00:00:00Z`) / 1000);
    if (Number.isNaN(dayStart)) return;
    const from = dayStart;
    const to = dayStart + DAY_SECONDS - 1; // exclusive of next midnight (no double-listing)
    archiveDate = dateStr; // remember the window so a provider switch can re-query it
    archiveTime = timeStr || null;
    const res = await fetchFrames({ from, to });
    if (!res.ok) {
      showToast(t(framesErrorKey(res.status, true)));
      return;
    }
    applyData(res.data);
    if (frames.length === 0) {
      showNodata(true);
      clearRadarLayer();
      return;
    }
    // Position at the frame nearest the picked time-of-day.
    let target = dayStart;
    if (timeStr) {
      const t2 = Date.parse(`${dateStr}T${timeStr}:00Z`);
      if (!Number.isNaN(t2)) target = Math.floor(t2 / 1000);
    }
    let nearest = 0;
    let best = Infinity;
    frames.forEach((f, i) => {
      const d = Math.abs(f.timestamp - target);
      if (d < best) {
        best = d;
        nearest = i;
      }
    });
    showFrame(nearest);
    if (best > gapToleranceS) showNodata(true); // picked moment has no data
  }

  // Scrubber drag -> show that frame, with a frame-time bubble while dragging.
  if (scrubber) {
    scrubber.addEventListener("input", () => {
      pause();
      showFrame(Number(scrubber.value));
      syncFollowing(); // scrubbing away from the newest frame drops LIVE
    });
    const showBubble = () => {
      if (frames.length === 0 || !scrubBubble) return;
      scrubBubble.hidden = false;
      updateScrubberVisual();
    };
    const hideBubble = () => {
      if (scrubBubble) scrubBubble.hidden = true;
    };
    scrubber.addEventListener("pointerdown", showBubble);
    scrubber.addEventListener("focus", showBubble);
    scrubber.addEventListener("pointerup", hideBubble);
    scrubber.addEventListener("pointercancel", hideBubble);
    scrubber.addEventListener("blur", hideBubble);
  }

  // -- provider switch (settings popover) -------------------------------------

  // Swap the Leaflet attribution + clip caption to the active provider's credit on a
  // source switch (OSM + lightning credits are untouched — they aren't ours to remove).
  function swapAttribution(entry) {
    const html = entry ? entry.attribution : null;
    if (attribution && onAttributionRemove) onAttributionRemove(attribution);
    attribution = html;
    if (html && onAttribution) onAttribution(html);
  }

  // Switch radar source. Persists per browser, tears down the loaded tiles, and
  // re-runs the current mode's load path — no page reload. LIVE keeps play state;
  // archive re-queries the same date/time window (cursor clamped by enterArchive).
  async function setProvider(name) {
    if (name === provider) return;
    if (!providers.some((p) => p.name === name)) return; // not advertised — ignore
    provider = name;
    try {
      localStorage.setItem("radar_provider", name);
    } catch {
      /* storage blocked; the choice still applies this session */
    }
    const entry = providers.find((p) => p.name === name);
    gapToleranceS = 1.5 * frameIntervalS(name);
    swapAttribution(entry);
    clearLayers(); // drop the previous source's tiles
    if (mode === "live") {
      const wasPlaying = playing;
      await enterLive();
      if (wasPlaying) play();
    } else if (archiveDate) {
      await enterArchive(archiveDate, archiveTime || undefined);
    }
  }

  // Initial load: LIVE.
  const res = await fetchFrames();
  if (!res.ok) {
    showToast(t(framesErrorKey(res.status)));
  } else {
    if (res.data.attribution) attribution = res.data.attribution;
    if (onAttribution && res.data.attribution) onAttribution(res.data.attribution);
    applyData(res.data);
    if (frames.length) showFrame(frames.length - 1); // newest on load
  }
  setFollowing(true);
  setPlayGlyph();
  resetRefresh();

  return {
    play,
    pause,
    toggle,
    next: () => {
      pause();
      showFrame(position + 1);
      syncFollowing(); // stepping off the newest frame drops LIVE (and back on re-arms it)
    },
    prev: () => {
      pause();
      showFrame(position - 1);
      syncFollowing();
    },
    enterLive,
    enterArchive,
    // For the settings popover's "Source du radar" radio group: the advert
    // list, the active source, and the live switch (which persists the choice itself).
    getProviders: () => providers.slice(),
    getProvider: () => provider,
    setProvider,
    // For the display-settings popover: live-adjust + persist the tile opacity.
    // Only a revealed layer is touched — a still-loading one stays at 0 until its
    // own reveal() (which reads the fresh value), so no half-loaded tiles appear.
    getOpacity: () => radarOpacity,
    setOpacity: (v) => {
      const n = Number(v);
      if (!Number.isFinite(n)) return;
      radarOpacity = Math.min(1, Math.max(OPACITY_MIN, n));
      try {
        localStorage.setItem("radar_opacity", String(radarOpacity));
      } catch {
        /* storage blocked; the choice still applies this session */
      }
      if (currentLayer && !currentLayer.isLoading()) currentLayer.setOpacity(radarOpacity);
    },
    // For the lightning overlay: the bootstrap advert + a state snapshot to sync.
    lightning: lightningConfig,
    getState: radarState,
    // For the video export: a pure, side-effect-free snapshot of the
    // inputs clip.js needs to recompose frames. clip.js applies the CLIP_WINDOW_S
    // (2 h) cap relative to cursorTs itself.
    getExportData: () => ({
      frames: frames.filter((_, i) => !inGap(i)), // ordered, gap frames removed
      cursorTs: frames[position]?.timestamp, // current scrubber position
      radarBounds, // L.LatLngBounds | null
      tileUrl, // (ts) => "/tiles/{provider}/{date}/{ts}/{z}/{x}/{y}.png"
      attribution, // provider credit (HTML) for the burned-in caption
      opacity: radarOpacity, // user-set rain opacity (default 0.8)
      tileSize: TILE_OPTS.tileSize, // 256
      maxNativeZoom: TILE_OPTS.maxNativeZoom, // 7
    }),
  };
}
