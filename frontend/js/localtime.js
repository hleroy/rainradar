// Local-calendar helpers for the archive date picker. The picker speaks the
// viewer's wall clock (the same one formatTime/formatDateShort display), so a
// picked day is local midnight → next local midnight — 23 h or 25 h on a DST
// change day. Only the epoch-seconds window reaches the backend; tile paths and
// the archive itself stay UTC (see radar.js utcDate).
//
// "YYYY-MM-DD" / "HH:MM" strings are split by hand, never handed to Date.parse:
// a date-only ISO string parses as UTC midnight, which is the bug this replaces.

const pad2 = (n) => String(n).padStart(2, "0");
const toSeconds = (date) => Math.floor(date.getTime() / 1000);

function splitDate(dateStr) {
  const [y, m, d] = String(dateStr).split("-").map(Number);
  return [y, m, d];
}

function splitTime(timeStr) {
  const [hh, mm] = String(timeStr).split(":").map(Number);
  return [hh, mm];
}

// Local calendar date ("YYYY-MM-DD") of an epoch-seconds ts.
export function localDateStr(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

// Local wall-clock time ("HH:MM") of an epoch-seconds ts.
export function localTimeStr(ts) {
  const d = new Date(ts * 1000);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

// Epoch-seconds window of a local calendar day: [local midnight, next local
// midnight − 1 s], so consecutive days never double-list a frame. Returns null
// for an unparseable date.
export function localDayWindow(dateStr) {
  const [y, m, d] = splitDate(dateStr);
  const from = toSeconds(new Date(y, m - 1, d));
  const next = toSeconds(new Date(y, m - 1, d + 1));
  if (Number.isNaN(from) || Number.isNaN(next)) return null;
  return { from, to: next - 1 };
}

// Epoch seconds of a local date + "HH:MM" wall-clock time, or NaN if unparseable.
export function localMoment(dateStr, timeStr) {
  const [y, m, d] = splitDate(dateStr);
  const [hh, mm] = splitTime(timeStr);
  return toSeconds(new Date(y, m - 1, d, hh, mm));
}

// Move an epoch-seconds ts by whole local calendar days, keeping its wall-clock
// time across a DST change (a fixed 24 h step would drift by an hour).
export function shiftLocalDays(ts, days) {
  const d = new Date(ts * 1000);
  d.setDate(d.getDate() + days);
  return toSeconds(d);
}
