// Client-side video export. Composites the CURRENT map view — OSM base +
// radar overlay (+ per-frame lightning) — across a fixed 2 h window ending at the
// scrubber cursor into a short, silent, looping H.264 MP4, entirely in the browser
// via WebCodecs (vendored mediabunny), then shares it (OS share sheet → WhatsApp/
// Signal) or downloads it.
//
// Non-negotiables this module honours: it contacts NO new origin (radar tiles are
// same-origin `/tiles/…`, lightning comes from the already-loaded pool, OSM is the
// base map already shown and reused from the HTTP cache); tile coverage is computed
// from the live map + bbox, never hardcoded; the mandatory attribution + a UTC
// timestamp are burned into every frame; the user's geolocation marker is never
// drawn; every failure ends in a localized toast — never an uncaught exception and
// never a radar/lightning impact.

import {
  canEncode,
  CanvasSource,
  Output,
  BufferTarget,
  Mp4OutputFormat,
  WebMOutputFormat,
} from "../vendor/mediabunny/mediabunny.js";

const CLIP_FPS = 4; // ~6 s loop for the ~24-frame live window
const CLIP_WINDOW_S = 7200; // 2 h ending at the cursor (live + archive alike)
const CLIP_MAX_DIM = 1280; // longest output side; even-rounded for yuv420p
const CLIP_BITRATE = 4_000_000; // ~4 Mbps; map content compresses well
const SLICE_FALLBACK_S = 600; // lead-in for a frame with no predecessor (~RV cadence)
const OSM_SUBS = ["a", "b", "c"]; // base tile subdomains (agent's choice)

const even = (n) => Math.max(2, 2 * Math.round(n / 2));

// Resolve an Image (CORS-clean) or null on error so a missing tile (gap edge /
// out-of-matrix) never fails the whole frame.
function loadImage(url) {
  return new Promise((resolve) => {
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

// Strip an HTML credit (with <a>) down to plain text for canvas drawing.
// DOMParser builds an inert document: unlike innerHTML on a (even detached)
// div, nothing loads and no event handler can fire, so a hostile attribution
// string from the API can never execute here.
function stripHtml(html) {
  if (!html) return "";
  const doc = new DOMParser().parseFromString(html, "text/html");
  return (doc.body.textContent || "").replace(/\s+/g, " ").trim();
}

// YYYYMMDD-HHMM from the UTC parts of an epoch-seconds timestamp (UTC math per
// CLAUDE.md — independent of the display locale/timezone).
function utcStamp(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return (
    `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}` +
    `-${p(d.getUTCHours())}${p(d.getUTCMinutes())}`
  );
}

export function initClip({ map, t, showToast, formatTime, formatDate, radar, lightning }) {
  const btn = document.getElementById("clip-btn");
  const progressEl = document.getElementById("clip-progress");
  let running = false;

  function setProgress(pct) {
    if (!progressEl) return;
    progressEl.hidden = false;
    progressEl.textContent = t("clip.rendering").replace("{n}", String(pct));
  }

  function setRendering(on) {
    if (!btn) return;
    btn.disabled = on;
    if (on) {
      btn.dataset.state = "rendering";
      btn.setAttribute("aria-busy", "true");
    } else {
      delete btn.dataset.state;
      btn.removeAttribute("aria-busy");
    }
    if (!on && progressEl) progressEl.hidden = true;
  }

  // -- compositing helpers ----------------------------------------------------

  // OSM base for the whole window (it doesn't change), at the composite viewport
  // size. Tiles are already in the HTTP cache from the live map, so this reuses
  // them and adds no new OSM request.
  async function drawBase(ctx, z, pb) {
    const n = 2 ** z;
    const minTX = Math.floor(pb.min.x / 256);
    const maxTX = Math.floor((pb.max.x - 1) / 256);
    const minTY = Math.floor(pb.min.y / 256);
    const maxTY = Math.floor((pb.max.y - 1) / 256);
    const jobs = [];
    for (let tx = minTX; tx <= maxTX; tx += 1) {
      for (let ty = minTY; ty <= maxTY; ty += 1) {
        if (ty < 0 || ty >= n) continue; // no tiles past the poles
        const wx = ((tx % n) + n) % n; // wrap longitude
        const sub = OSM_SUBS[Math.abs(tx + ty) % OSM_SUBS.length];
        const url = `https://${sub}.tile.openstreetmap.org/${z}/${wx}/${ty}.png`;
        const dx = tx * 256 - pb.min.x;
        const dy = ty * 256 - pb.min.y;
        jobs.push(
          loadImage(url).then((img) => {
            if (img) ctx.drawImage(img, dx, dy, 256, 256);
          }),
        );
      }
    }
    await Promise.all(jobs);
  }

  // Radar tiles for one frame, drawn at radar's native max zoom (≤ 7) scaled up to
  // match the displayed overzoom, clipped to the bbox, at the live opacity.
  async function drawRadar(ctx, ts, ex, z, pb) {
    const zr = Math.min(z, ex.maxNativeZoom);
    const scale = 2 ** (z - zr);
    const tsize = ex.tileSize * scale; // displayed size of a native tile at zoom z
    let minTX = Math.floor(pb.min.x / tsize);
    let maxTX = Math.floor((pb.max.x - 1) / tsize);
    let minTY = Math.floor(pb.min.y / tsize);
    let maxTY = Math.floor((pb.max.y - 1) / tsize);
    // Limit to the radar bbox tile range at zr so we don't request out-of-matrix
    // tiles (they only 404 against our backend).
    if (ex.radarBounds) {
      const nw = map.project(ex.radarBounds.getNorthWest(), zr);
      const se = map.project(ex.radarBounds.getSouthEast(), zr);
      minTX = Math.max(minTX, Math.floor(nw.x / ex.tileSize));
      maxTX = Math.min(maxTX, Math.floor(se.x / ex.tileSize));
      minTY = Math.max(minTY, Math.floor(nw.y / ex.tileSize));
      maxTY = Math.min(maxTY, Math.floor(se.y / ex.tileSize));
    }
    const template = ex.tileUrl(ts); // "/tiles/{provider}/{date}/{ts}/…" (same-origin)
    const jobs = [];
    for (let tx = minTX; tx <= maxTX; tx += 1) {
      for (let ty = minTY; ty <= maxTY; ty += 1) {
        const url = template
          .replace("{z}", String(zr))
          .replace("{x}", String(tx))
          .replace("{y}", String(ty));
        const dx = tx * tsize - pb.min.x;
        const dy = ty * tsize - pb.min.y;
        jobs.push(
          loadImage(url).then((img) => {
            if (img) ctx.drawImage(img, dx, dy, tsize, tsize);
          }),
        );
      }
    }
    await Promise.all(jobs);
  }

  // Bottom caption: UTC-derived timestamp (left) + mandatory credits (right) over a
  // legible strip. Burned in because the shared file has no Leaflet attribution
  // control.
  function drawCaption(ctx, w, h, ts, rightText) {
    const fs = Math.max(11, Math.round(h * 0.028));
    const pad = Math.round(fs * 0.6);
    const stripH = fs + pad * 2;
    ctx.save();
    ctx.fillStyle = "rgba(20, 20, 20, 0.55)";
    ctx.fillRect(0, h - stripH, w, stripH);
    ctx.fillStyle = "#fff";
    ctx.font = `${fs}px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`;
    ctx.textBaseline = "middle";
    const cy = h - stripH / 2;
    ctx.textAlign = "left";
    ctx.fillText(`${formatTime(ts)} · ${formatDate(ts)}`, pad, cy);
    ctx.textAlign = "right";
    ctx.fillText(rightText, w - pad, cy);
    ctx.restore();
  }

  // -- export -----------------------------------------------------------------

  async function exportClip() {
    if (running) return; // re-entrancy guard
    running = true;
    setRendering(true);
    try {
      // Codec selection. Prefer H.264/MP4: it autoloops inline when shared to
      // WhatsApp/Signal. Where H.264 is unavailable (e.g. Firefox, which has no
      // working WebCodecs H.264 encoder — mediabunny issue #222), fall back to
      // VP9/WebM, which encodes fine there. WebM can't be shared into WhatsApp, so
      // it's offered as a plain download instead (with a note). Only a browser that
      // can encode neither shows clip.unsupported.
      let codec, format, ext, mime, shareable;
      if (await canEncode("avc")) {
        codec = "avc";
        format = new Mp4OutputFormat();
        ext = "mp4";
        mime = "video/mp4";
        shareable = true;
      } else if (await canEncode("vp9")) {
        codec = "vp9";
        format = new WebMOutputFormat();
        ext = "webm";
        mime = "video/webm";
        shareable = false;
      } else {
        showToast(t("clip.unsupported"));
        return;
      }

      // Inputs: the non-gap frames in [cursorTs − 2 h, cursorTs].
      const ex = radar.getExportData();
      if (ex.cursorTs == null) {
        showToast(t("clip.nodata"));
        return;
      }
      const fromTs = ex.cursorTs - CLIP_WINDOW_S;
      const clipFrames = ex.frames.filter(
        (f) => f.timestamp >= fromTs && f.timestamp <= ex.cursorTs,
      );
      if (clipFrames.length < 2) {
        showToast(t("clip.nodata"));
        return;
      }
      const firstTs = clipFrames[0].timestamp;
      const lastTs = clipFrames[clipFrames.length - 1].timestamp;

      // Geometry: composite at the live viewport size, scale into an even-sided
      // output ≤ CLIP_MAX_DIM.
      const size = map.getSize();
      const cw = size.x;
      const ch = size.y;
      const z = map.getZoom();
      const pb = map.getPixelBounds();
      const fit = Math.min(1, CLIP_MAX_DIM / Math.max(cw, ch));
      const outW = even(cw * fit);
      const outH = even(ch * fit);

      // Lightning hooks; widen the pool to cover the clip range before rendering.
      const lx = lightning.getExportLayer();
      if (lx?.active) await lx.ensureRange(firstTs - SLICE_FALLBACK_S, lastTs);

      // Right-side credits: OSM always, RainViewer always, Blitzortung only when
      // the lightning layer is active. Stripped to plain text.
      const credits = [t("clip.osm_credit"), stripHtml(ex.attribution)];
      if (lx?.active && lx.attribution) credits.push(stripHtml(lx.attribution));
      const rightText = credits.filter(Boolean).join(" · ");

      // Base canvas (built once) + reusable composite + encoder canvases.
      const baseCanvas = document.createElement("canvas");
      baseCanvas.width = cw;
      baseCanvas.height = ch;
      await drawBase(baseCanvas.getContext("2d"), z, pb);

      const composite = document.createElement("canvas");
      composite.width = cw;
      composite.height = ch;
      const cctx = composite.getContext("2d");

      const encoder = document.createElement("canvas");
      encoder.width = outW;
      encoder.height = outH;
      const ectx = encoder.getContext("2d");

      const source = new CanvasSource(encoder, { codec, bitrate: CLIP_BITRATE });
      const output = new Output({ format, target: new BufferTarget() });
      output.addVideoTrack(source, { frameRate: CLIP_FPS });
      await output.start();

      let prevTs = null;
      for (let i = 0; i < clipFrames.length; i += 1) {
        const ts = clipFrames[i].timestamp;
        cctx.clearRect(0, 0, cw, ch);
        cctx.drawImage(baseCanvas, 0, 0);

        // Radar, clipped to the bbox rect and at the live opacity.
        cctx.save();
        if (ex.radarBounds) {
          const a = map.latLngToContainerPoint(ex.radarBounds.getNorthWest());
          const b = map.latLngToContainerPoint(ex.radarBounds.getSouthEast());
          cctx.beginPath();
          cctx.rect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
          cctx.clip();
        }
        cctx.globalAlpha = ex.opacity;
        await drawRadar(cctx, ts, ex, z, pb);
        cctx.restore();

        // Per-frame lightning slice (prevTs, ts], only when the layer is active.
        if (lx?.active) lx.drawSlice(cctx, map, prevTs ?? ts - SLICE_FALLBACK_S, ts);

        drawCaption(cctx, cw, ch, ts, rightText);

        // Scale the finished frame into the encoder canvas and encode it.
        ectx.clearRect(0, 0, outW, outH);
        ectx.drawImage(composite, 0, 0, outW, outH);
        await source.add(i / CLIP_FPS, 1 / CLIP_FPS);

        setProgress(Math.round(((i + 1) / clipFrames.length) * 100));
        prevTs = ts;
      }

      await output.finalize();
      const file = new File([output.target.buffer], `rainradar-${utcStamp(lastTs)}.${ext}`, {
        type: mime,
      });
      await deliver(file, shareable);
    } catch {
      // Any failure (incl. a tainted-canvas SecurityError) degrades to a toast.
      showToast(t("clip.failed"));
    } finally {
      running = false;
      setRendering(false);
    }
  }

  // For the shareable MP4, prefer the OS share sheet (so the file goes straight
  // into WhatsApp/Signal); otherwise download. The WebM fallback can't be shared
  // into those apps, so it's downloaded directly with a note.
  async function deliver(file, shareable) {
    if (shareable && navigator.canShare?.({ files: [file] })) {
      try {
        await navigator.share({ files: [file], title: t("app.title") });
        return;
      } catch (e) {
        if (e.name === "AbortError") return; // user dismissed the sheet
        // otherwise fall through to a download
      }
    }
    const url = URL.createObjectURL(file);
    const a = Object.assign(document.createElement("a"), { href: url, download: file.name });
    a.click();
    URL.revokeObjectURL(url);
    if (!shareable) showToast(t("clip.webm_fallback"));
  }

  // Read-only view of the in-render guard: lets the SW auto-update
  // reload defer until a render finishes, so a half-rendered share is never killed.
  function isExporting() {
    return running;
  }

  return { export: exportClip, isExporting };
}
