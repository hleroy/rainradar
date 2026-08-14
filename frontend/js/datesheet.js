// Date-navigation sheet: a modal calendar + time + quick-jump chips (incl.
// -1d/+1d day stepping) for moving through the 90-day archive. Replaces the old
// always-visible <input type=date>/<input type=time>/Go row, which vanished
// entirely on mobile portrait for lack of space, and absorbs the day-step role
// from the transport bar's former
// « » buttons. Like the About dialog, it's an in-page modal built from the same
// .modal-panel chrome; it only ever calls radar.enterArchive/enterLive, never
// an upstream provider directly.

const DAY_S = 86400;
const WEEKDAY_REF_MS = Date.UTC(2024, 0, 1); // a known Monday, for locale weekday labels

const utcDateStr = (epochSeconds) => new Date(epochSeconds * 1000).toISOString().slice(0, 10);
const utcTimeStr = (epochSeconds) => new Date(epochSeconds * 1000).toISOString().slice(11, 16);
const pad2 = (n) => String(n).padStart(2, "0");
const monthKey = (dateStr) => dateStr.slice(0, 7); // "YYYY-MM"

export function initDateSheet({ currentLocale, radar }) {
  const overlay = document.getElementById("datesheet-overlay");
  const dialog = document.getElementById("datesheet-dialog");
  const trigger = document.getElementById("datesheet-btn");
  const closeBtn = document.getElementById("datesheet-close");
  const quickGroup = document.getElementById("datesheet-quick");
  const calMonthEl = document.getElementById("datesheet-cal-month");
  const calPrevBtn = document.getElementById("datesheet-cal-prev");
  const calNextBtn = document.getElementById("datesheet-cal-next");
  const calWeekdaysEl = document.getElementById("datesheet-cal-weekdays");
  const calGridEl = document.getElementById("datesheet-cal-grid");
  const timeInput = document.getElementById("datesheet-time");
  const goBtn = document.getElementById("datesheet-go");

  let isOpen = false;
  let bounds = null; // { min, max }: "YYYY-MM-DD" strings, or null (unbounded)
  let selectedDate = null; // "YYYY-MM-DD" highlighted in the grid
  let viewYear = 0;
  let viewMonth = 0; // 0-11

  // -- archive bounds (nice-to-have; the sheet still works unbounded) --------

  async function loadBounds() {
    try {
      // Per-provider bounds: Météo-France's archive is shorter until it
      // fills, so the range follows the active source.
      const p = radar.getProvider ? radar.getProvider() : null;
      const qs = p ? `?provider=${encodeURIComponent(p)}` : "";
      const resp = await fetch(`/api/radar/range${qs}`);
      if (!resp.ok) return;
      const { earliest, latest } = await resp.json();
      bounds = {
        min: earliest == null ? null : utcDateStr(earliest),
        max: latest == null ? null : utcDateStr(latest),
      };
      if (isOpen) renderMonth(); // reflect freshly loaded bounds if the sheet is up
    } catch {
      /* bounds are a nice-to-have; the sheet still works unbounded */
    }
  }

  function inBounds(dateStr) {
    if (!bounds) return true;
    if (bounds.min && dateStr < bounds.min) return false;
    if (bounds.max && dateStr > bounds.max) return false;
    return true;
  }

  // -- reference point + jumping ----------------------------------------------

  // The moment day-step/quick-jump offsets are computed from: the current
  // archive cursor if we're mid-archive, otherwise "now".
  function referenceTs() {
    const state = radar.getState();
    if (state.mode === "archive" && state.cursorTs != null) return state.cursorTs;
    return Math.floor(Date.now() / 1000);
  }

  function performJump(dateStr, timeStr) {
    radar.enterArchive(dateStr, timeStr || undefined);
    selectedDate = dateStr;
    if (timeStr) timeInput.value = timeStr;
    close();
  }

  // Shared by the « »  step buttons and the -30d/-7d/-1d/+1d chips: an exact
  // +/-24h-multiple jump from the reference point. Stepping past "now" enters
  // LIVE instead of an empty future archive query.
  function jumpByDays(deltaDays) {
    const target = referenceTs() + deltaDays * DAY_S;
    if (target > Math.floor(Date.now() / 1000)) {
      radar.enterLive();
      close();
      return;
    }
    performJump(utcDateStr(target), utcTimeStr(target));
  }

  // -- calendar grid ------------------------------------------------------

  function monthLabel(year, month) {
    return new Intl.DateTimeFormat(currentLocale(), { month: "long", year: "numeric" }).format(
      new Date(Date.UTC(year, month, 1)),
    );
  }

  function weekdayLabels() {
    const fmt = new Intl.DateTimeFormat(currentLocale(), { weekday: "short" });
    return [0, 1, 2, 3, 4, 5, 6].map((i) => fmt.format(new Date(WEEKDAY_REF_MS + i * DAY_S * 1000)));
  }

  function renderWeekdays() {
    calWeekdaysEl.replaceChildren();
    for (const label of weekdayLabels()) {
      const el = document.createElement("span");
      el.textContent = label;
      calWeekdaysEl.appendChild(el);
    }
  }

  // Monday-first grid, all in UTC (matches the archive's UTC day boundaries).
  function renderMonth() {
    calMonthEl.textContent = monthLabel(viewYear, viewMonth);
    calGridEl.replaceChildren();

    const first = new Date(Date.UTC(viewYear, viewMonth, 1));
    const daysInMonth = new Date(Date.UTC(viewYear, viewMonth + 1, 0)).getUTCDate();
    const leading = (first.getUTCDay() + 6) % 7; // Mon=0 .. Sun=6
    const today = utcDateStr(Math.floor(Date.now() / 1000));

    for (let i = 0; i < leading; i += 1) {
      calGridEl.appendChild(document.createElement("span"));
    }
    for (let day = 1; day <= daysInMonth; day += 1) {
      const dateStr = `${viewYear}-${pad2(viewMonth + 1)}-${pad2(day)}`;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "datesheet-day";
      btn.textContent = String(day);
      if (dateStr === today) btn.classList.add("today");
      if (dateStr === selectedDate) {
        btn.classList.add("selected");
        btn.setAttribute("aria-current", "date");
      }
      if (inBounds(dateStr)) {
        btn.addEventListener("click", () => performJump(dateStr, timeInput.value));
      } else {
        btn.disabled = true;
      }
      calGridEl.appendChild(btn);
    }

    const curKey = `${viewYear}-${pad2(viewMonth + 1)}`;
    calPrevBtn.disabled = !!(bounds && bounds.min && curKey <= monthKey(bounds.min));
    calNextBtn.disabled = !!(bounds && bounds.max && curKey >= monthKey(bounds.max));
  }

  function shiftMonth(delta) {
    viewMonth += delta;
    if (viewMonth < 0) {
      viewMonth = 11;
      viewYear -= 1;
    } else if (viewMonth > 11) {
      viewMonth = 0;
      viewYear += 1;
    }
    renderMonth();
  }

  // -- focus trap (mirrors about.js) -------------------------------------------

  function focusable() {
    return [
      ...dialog.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"]), input'),
    ].filter((el) => el.offsetParent !== null);
  }

  function onKeydown(e) {
    if (!isOpen) return;
    if (e.key === "Escape") {
      e.preventDefault();
      close();
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

  // -- open / close -------------------------------------------------------

  function open() {
    if (isOpen) return;
    isOpen = true;
    const ref = referenceTs();
    selectedDate = utcDateStr(ref);
    timeInput.value = utcTimeStr(ref);
    viewYear = Number(selectedDate.slice(0, 4));
    viewMonth = Number(selectedDate.slice(5, 7)) - 1;
    renderWeekdays();
    renderMonth();
    loadBounds(); // refresh for the active provider (its range may have changed)
    overlay.hidden = false;
    dialog.focus();
  }

  function close() {
    if (!isOpen) return;
    isOpen = false;
    overlay.hidden = true;
    if (trigger) trigger.focus();
  }

  function refreshI18n() {
    if (!isOpen) return;
    renderWeekdays();
    renderMonth();
  }

  // -- wiring ---------------------------------------------------------------

  if (trigger) trigger.addEventListener("click", open);
  if (closeBtn) closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close(); // backdrop click; clicks inside don't close
  });
  document.addEventListener("keydown", onKeydown);

  calPrevBtn.addEventListener("click", () => shiftMonth(-1));
  calNextBtn.addEventListener("click", () => shiftMonth(1));
  quickGroup.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-offset-days]");
    if (btn) jumpByDays(Number(btn.dataset.offsetDays));
  });
  goBtn.addEventListener("click", () => {
    performJump(selectedDate || utcDateStr(referenceTs()), timeInput.value);
  });

  loadBounds();

  return { open, close, refreshI18n };
}
