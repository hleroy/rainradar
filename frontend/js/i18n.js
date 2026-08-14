// i18n: locale loading, t() lookup, and Intl-based time formatting.

let dict = {};
let locale = "en";

function detectLang() {
  const stored = localStorage.getItem("lang");
  if (stored === "fr" || stored === "en") {
    return stored;
  }
  return (navigator.language || "en").toLowerCase().startsWith("fr") ? "fr" : "en";
}

async function load(lang) {
  const resp = await fetch(`/static/i18n/${lang}.json`);
  dict = await resp.json();
  locale = lang;
  document.documentElement.lang = lang;
  applyTranslations();
}

// Apply translations to every [data-i18n] element's textContent.
function applyTranslations() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    el.textContent = t(key);
  });
  // Title + aria-labels for controls (textContent of buttons stays as glyphs).
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    const key = el.getAttribute("data-i18n-title");
    el.title = t(key);
    el.setAttribute("aria-label", t(key));
  });
}

export function t(key) {
  return dict[key] ?? key;
}

export function currentLocale() {
  return locale;
}

// HH:MM in the active locale, from epoch seconds.
export function formatTime(epochSeconds) {
  return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" }).format(
    new Date(epochSeconds * 1000),
  );
}

// Compact date in the active locale (e.g. "21 Jun 2026"), for the transport
// bar's small line under the time.
export function formatDateShort(epochSeconds) {
  return new Intl.DateTimeFormat(locale, { day: "numeric", month: "short", year: "numeric" }).format(
    new Date(epochSeconds * 1000),
  );
}

// Long-form date in the active locale (e.g. "21 June 2026"), from epoch seconds.
export function formatDate(epochSeconds) {
  return new Intl.DateTimeFormat(locale, { day: "numeric", month: "long", year: "numeric" }).format(
    new Date(epochSeconds * 1000),
  );
}

export async function initI18n() {
  await load(detectLang());
}

// Toggle FR/EN, persist, and re-apply. Returns the new lang.
export async function toggleLang() {
  const next = locale === "fr" ? "en" : "fr";
  localStorage.setItem("lang", next);
  await load(next);
  return next;
}
