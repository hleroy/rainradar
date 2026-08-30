// One-finger zoom shortcut — the Google Maps gesture: double-tap the map and keep
// the finger down on the second tap, then slide down to zoom in, up to zoom out.
//
// Purely additive and touch-only. Nothing here runs until a double-tap has been
// recognised, so panning, pinch-zoom, the plain double-tap zoom, and taps on
// markers, popups or controls all reach Leaflet exactly as before. The gesture
// owns the sequence only from the moment the finger has travelled far enough to
// rule out a plain double-tap; anything unexpected aborts it and hands the map
// back untouched.
//
// Leaflet 1.9 has no such handler, so this is modelled on its own pinch handler
// (`TouchZoom`): fractional `_move()` per frame while the finger is down, then one
// snapped `_animateZoom()` on release.

const DOUBLE_TAP_MS = 300; // max gap between the first tap's lift and the second's touch
const TAP_MAX_MS = 400; // a tap is short (but not a long-press)…
const TAP_SLOP_PX = 12; // …and near-motionless
const DOUBLE_TAP_SLOP_PX = 40; // and the second one lands near the first
const ENGAGE_PX = 8; // slide this far before the gesture takes over
const PX_PER_ZOOM_LEVEL = 150; // vertical travel that buys one whole zoom level
const DOWN_IS_ZOOM_IN = true; // slide down = zoom in (Google Maps); flip to reverse

// Non-passive + capture: capture puts us ahead of Leaflet's own container-level
// listeners (so an armed sequence can be withheld from the drag handler before it
// ever sees it), and non-passive keeps preventDefault() available.
const LISTEN = { capture: true, passive: false };

export function initOneFingerZoom(map) {
  const container = map.getContainer();

  let lastTapAt = 0; // when the previous tap lifted (0 = no candidate)
  let lastTapPoint = null;
  let seq = null; // the in-flight single-finger sequence
  let zoom = null; // the engaged gesture, once the finger has moved

  const dist = (a, b) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
  const clearTapHistory = () => {
    lastTapAt = 0;
    lastTapPoint = null;
  };

  // A touch counts only when it lands on the map itself — not on the overlay UI
  // (a sibling of #map), and not on a Leaflet control or an open popup, whose own
  // taps (zoom in/out, the popup's content) must keep working.
  function isOnMap(target) {
    if (!target || typeof target.closest !== "function") return false;
    return container.contains(target) && !target.closest(".leaflet-control, .leaflet-popup");
  }

  // Container point → the map center that puts `anchorLatLng` back under it at
  // `z`. Same construction Leaflet's pinch uses, with a fixed anchor: the zoom
  // pivots on the double-tapped point, not on the map center.
  function centerFor(z) {
    return map.unproject(map.project(zoom.anchor, z).subtract(zoom.offset), z);
  }

  function engage() {
    // Kill any pan inertia or in-flight flyTo first: `_move` per frame would
    // otherwise fight a running animation, and the anchor must be read off where
    // the map actually is. (A zoom animation is not stopped by this, which is why
    // arming declines while one runs.)
    map._stop();
    const point = map.mouseEventToContainerPoint(seq.start);
    zoom = {
      start: map.getZoom(),
      level: map.getZoom(),
      anchor: map.containerPointToLatLng(point),
      offset: point.subtract(map.getSize().divideBy(2)),
      center: map.getCenter(),
      frame: 0,
    };
    map._moveStart(true, false);
  }

  function render() {
    if (zoom.frame) return;
    zoom.frame = requestAnimationFrame(() => {
      if (!zoom) return;
      zoom.frame = 0;
      map._move(zoom.center, zoom.level, { pinch: true, round: false }, undefined);
    });
  }

  // Land on a real (snapped) zoom level. `animate` false settles instantly, which
  // is what a hand-off wants: it leaves no zoom animation running for the gesture
  // that interrupted us (a second finger) to fight with.
  function settle(animate) {
    if (!zoom) return;
    if (zoom.frame) cancelAnimationFrame(zoom.frame);
    const level = map._limitZoom(zoom.level);
    const center = centerFor(level);
    zoom = null;
    if (animate && map.options.zoomAnimation) {
      map._animateZoom(center, level, true, map.options.zoomSnap);
    } else {
      map._resetView(center, level);
    }
  }

  function endSequence() {
    document.removeEventListener("touchmove", guardedMove, LISTEN);
    seq = null;
  }

  function onTouchStart(e) {
    // A second finger during our zoom: settle where we are and get out of the
    // way — the event still reaches Leaflet, so a pinch can take over.
    if (zoom) {
      settle(false);
      endSequence();
    }
    if (e.touches.length !== 1) {
      seq = null;
      clearTapHistory();
      return;
    }
    const touch = e.touches[0];
    if (!isOnMap(e.target)) {
      seq = null;
      clearTapHistory();
      return;
    }
    const start = { clientX: touch.clientX, clientY: touch.clientY };
    const armed =
      !!lastTapPoint &&
      Date.now() - lastTapAt <= DOUBLE_TAP_MS &&
      dist(start, lastTapPoint) <= DOUBLE_TAP_SLOP_PX &&
      !map._animatingZoom && // let Leaflet finish its own zoom rather than fight it
      !!map.touchZoom &&
      map.touchZoom.enabled();
    seq = { start, at: Date.now(), armed };
    clearTapHistory(); // consumed either way: a third tap re-arms from scratch
    if (!armed) return;

    // Withhold the touch from Leaflet's drag handler so a slide can never pan the
    // map out from under the zoom. Deliberately NOT preventDefault(): an
    // unprevented tap still synthesises the click/dblclick pair that Leaflet's
    // doubleClickZoom needs when the finger simply lifts again.
    e.stopPropagation();
    document.addEventListener("touchmove", guardedMove, LISTEN);
  }

  function onTouchMove(e) {
    if (!seq || !seq.armed || e.touches.length !== 1) return;
    const touch = e.touches[0];
    const dy = touch.clientY - seq.start.clientY;
    if (!zoom) {
      if (Math.hypot(touch.clientX - seq.start.clientX, dy) < ENGAGE_PX) return;
      engage();
    }
    // Ours now. preventDefault() also cancels the synthetic click/dblclick pair,
    // so doubleClickZoom cannot fire a second zoom on top of this one.
    e.preventDefault();
    e.stopPropagation();
    const delta = (DOWN_IS_ZOOM_IN ? dy : -dy) / PX_PER_ZOOM_LEVEL;
    zoom.level = Math.max(map.getMinZoom(), Math.min(map.getMaxZoom(), zoom.start + delta));
    zoom.center = centerFor(zoom.level);
    render();
  }

  function onTouchEnd(e) {
    if (zoom) {
      e.preventDefault(); // no synthetic click/dblclick in the gesture's wake
      e.stopPropagation();
      settle(true);
      endSequence();
      clearTapHistory();
      return;
    }
    if (!seq) return;
    // Only a short, near-motionless, fully-lifted single touch arms the next one —
    // and never the second tap of a double-tap we already handled.
    const touch = e.changedTouches[0];
    const tapped =
      !seq.armed &&
      e.touches.length === 0 &&
      Date.now() - seq.at <= TAP_MAX_MS &&
      !!touch &&
      dist(touch, seq.start) <= TAP_SLOP_PX;
    endSequence();
    if (tapped) {
      lastTapAt = Date.now();
      lastTapPoint = { clientX: touch.clientX, clientY: touch.clientY };
    } else {
      clearTapHistory();
    }
  }

  function onTouchCancel() {
    settle(false);
    endSequence();
    clearTapHistory();
  }

  // A throw inside a touch listener would otherwise strand the map mid-`_moveStart`.
  // The shortcut is expendable; the map is not.
  function guard(handler) {
    return (e) => {
      try {
        handler(e);
      } catch {
        try {
          settle(false);
        } catch {
          zoom = null;
        }
        endSequence();
        clearTapHistory();
      }
    };
  }

  // The move listener is attached only for an armed sequence, so it costs nothing
  // for ordinary panning; the other three stay on and do near-nothing until a
  // double-tap shows up.
  const guardedMove = guard(onTouchMove);
  document.addEventListener("touchstart", guard(onTouchStart), LISTEN);
  document.addEventListener("touchend", guard(onTouchEnd), LISTEN);
  document.addEventListener("touchcancel", guard(onTouchCancel), LISTEN);

  return { isZooming: () => zoom !== null };
}
