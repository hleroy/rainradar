// About dialog. An in-page modal overlay opened from the app title. Like
// the lightning legend, its rich content (intro paragraphs + the credit link) is
// built in JS from t() lookups and re-translated on language toggle — i18n.js's
// applyTranslations() sets textContent, which would strip the credit's <a> and
// collapse the paragraphs. Stats are fetched once per open from /api/stats (the
// server caches), and the dialog degrades gracefully if that fetch fails.

const DASH = "—";
const CREDIT_URL = "https://hleroy.com";
const SOURCE_URL = "https://github.com/hleroy/rainradar";
// GitHub's mark (Octicons, MIT), inlined rather than fetched: the CSP blocks
// remote assets, and one request for 15 px is not worth a file. Built in JS
// because buildProse() rewrites this subtree on every language toggle.
const GH_MARK_PATH =
  "M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.012 8.012 0 0 0 16 8c0-4.42-3.58-8-8-8Z";
const SVG_NS = "http://www.w3.org/2000/svg";
// The standalone layers explainer ships as two documents on two short URLs, not as
// one page with a switch — it is static HTML with no i18n runtime of its own.
const EXPLAINER_URL = { fr: "/apropos", en: "/about" };
// Provider labels are proper nouns (untranslated). These name the archive-stats
// rows, so they stay bare — the advert's "(beta)" suffix qualifies the layer in the
// picker, not the archive it has already accumulated.
const PROVIDER_LABELS = { rainviewer: "RainViewer", meteofrance: "Météo-France" };

export function initAbout({ t, formatDate, formatTime, currentLocale }) {
  const overlay = document.getElementById("about-overlay");
  const dialog = document.getElementById("about-dialog");
  const trigger = document.getElementById("about-trigger");
  const closeBtn = document.getElementById("about-close");
  const body = document.getElementById("about-body");
  const statsList = document.getElementById("about-stats-list");
  const statsError = document.getElementById("about-stats-error");
  const liveLine = document.getElementById("about-live");
  const figures = document.getElementById("about-figures");
  const explainer = document.getElementById("about-explainer");

  let isOpen = false;
  let lastStats = null; // last successful payload, for re-render on locale toggle

  // -- formatting -------------------------------------------------------------

  function groupNumber(n) {
    return new Intl.NumberFormat(currentLocale()).format(n);
  }

  function num(n) {
    return n == null ? DASH : groupNumber(n);
  }

  // Archive size. Below a gigabyte, report megabytes: a fresh or small archive
  // rounded to one decimal of a GB reads "0 GB", which looks like a failure
  // rather than a young archive.
  function gb(bytes) {
    if (bytes == null) return DASH;
    const [scaled, unit, digits] =
      bytes < 1e9 ? [bytes / 1e6, "about.unit.mb", 0] : [bytes / 1e9, "about.unit.gb", 1];
    const value = new Intl.NumberFormat(currentLocale(), {
      maximumFractionDigits: digits,
    }).format(scaled);
    return `${value} ${t(unit)}`;
  }

  function spanText(earliest, latest) {
    if (earliest == null || latest == null) return DASH;
    return `${formatDate(earliest)} → ${formatDate(latest)}`;
  }

  function frameText(ts) {
    return ts == null ? DASH : `${formatTime(ts)} ${formatDate(ts)}`;
  }

  // -- prose + credit (built in JS, re-translated on toggle) ------------------

  function buildProse() {
    body.replaceChildren();
    // Iterate the ordered about.intro.p* keys we find (adding p2 later needs no
    // code change); t() returns the key itself when a key is missing.
    let last = null;
    for (let i = 1; ; i += 1) {
      const key = `about.intro.p${i}`;
      const value = t(key);
      if (value === key) break;
      const p = document.createElement("p");
      p.textContent = value;
      body.appendChild(p);
      last = p;
    }
    // Byline on its own line: who made it, and where the source lives (the AGPL
    // obliges us to offer the latter). Appended to the last intro paragraph these
    // read as part of the sentence, and the source label wrapped mid-phrase — two
    // items earn a line, where the author's name alone did not.
    if (!last) return;
    const byline = document.createElement("p");
    byline.className = "about-byline";
    byline.append(
      externalLink(CREDIT_URL, t("about.credit_name")),
      " · ",
      externalLink(SOURCE_URL, t("about.source"), githubMark()),
    );
    body.appendChild(byline);
  }

  function externalLink(href, label, mark = null) {
    const a = document.createElement("a");
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener";
    if (mark) {
      a.className = "marked"; // CSS keeps mark and label on one line
      a.appendChild(mark);
    }
    a.append(label);
    return a;
  }

  // createElementNS, not innerHTML: SVG is not in the HTML namespace, so an
  // innerHTML-parsed <svg> here would come out inert.
  function githubMark() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "gh-mark");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("width", "15");
    svg.setAttribute("height", "15");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("fill", "currentColor");
    path.setAttribute("d", GH_MARK_PATH);
    svg.appendChild(path);
    return svg;
  }

  // Send the teaser to the explainer written in the language now on screen. The
  // markup hard-codes the French URL, so this must also run with the dialog closed
  // — otherwise a toggle made before the dialog is ever opened leaves it stale.
  function syncExplainerLink() {
    if (!explainer) return;
    const lang = currentLocale();
    explainer.href = EXPLAINER_URL[lang] || EXPLAINER_URL.fr;
    explainer.hreflang = lang;
  }

  // -- stats rows -------------------------------------------------------------

  // `sub` marks a row that decomposes the figure above rather than standing on its
  // own. It only sets a class: the ↳ that shows it is decoration and lives in CSS,
  // because in the label text it landed inside the accessible name and screen
  // readers announced it as part of the provider's.
  function addRow(label, value, sub = false) {
    const dt = document.createElement("dt");
    if (sub) dt.className = "about-sub";
    dt.textContent = label;
    const dd = document.createElement("dd");
    if (sub) dd.className = "about-sub";
    dd.textContent = value;
    statsList.append(dt, dd);
    return dd;
  }

  // A headline number: the two counts that convey the archive's scale get weight,
  // everything else stays a reference row below. `tone` picks the accent from the
  // app's own icon — rain-drop blue for radar, bolt amber for lightning.
  function addFigure(label, value, caption, tone) {
    const fig = document.createElement("div");
    fig.className = "about-figure";
    fig.dataset.tone = tone;
    const v = document.createElement("strong");
    v.textContent = value;
    const l = document.createElement("span");
    l.textContent = label;
    fig.append(v, l);
    if (caption) {
      const c = document.createElement("em");
      c.textContent = caption;
      fig.appendChild(c);
    }
    figures.appendChild(fig);
  }

  function renderStats(data) {
    statsList.replaceChildren();
    figures.replaceChildren();
    liveLine.replaceChildren();
    statsError.hidden = true;

    const radar = data.radar || {};
    const lightning = data.lightning || {};
    const storage = data.storage || {};
    const live = data.live || {};

    // -- is it running right now? ---------------------------------------------
    const dot = document.createElement("i");
    dot.className = "about-dot";
    // Only claim "live" when the backend actually reports a recent frame; the dot
    // is the one animated element in the dialog, so it must not lie.
    dot.dataset.live = String(live.last_frame != null);
    liveLine.append(dot, `${t("about.stat.live")} ${frameText(live.last_frame)}`);
    if (lightning.enabled) {
      const wsKey = lightning.ws_connected ? "about.stat.ws_on" : "about.stat.ws_off";
      liveLine.append(` · ${t(wsKey)}`);
    }

    // -- headline figures ------------------------------------------------------
    addFigure(t("about.figure.frames"), num(radar.frames_total), null, "radar");
    if (lightning.enabled) {
      const delta =
        lightning.strikes_24h == null
          ? null
          : t("about.suffix_24h").replace("{n}", groupNumber(lightning.strikes_24h));
      addFigure(t("about.figure.strikes"), num(lightning.archived_total), delta, "bolt");
    } else {
      // Without lightning there is only one count worth promoting, so disk size
      // takes the second slot rather than leaving the row lopsided.
      addFigure(t("about.figure.size"), gb(storage.bytes), null, "radar");
    }

    // -- reference rows --------------------------------------------------------
    // Per-source breakdown, only when more than one provider is archived. These
    // lead the list so they still read as a breakdown of the frames figure above.
    const providers = Array.isArray(radar.providers) ? radar.providers : [];
    if (providers.length >= 2) {
      for (const p of providers) {
        addRow(PROVIDER_LABELS[p.name] || p.name, num(p.frames), true);
      }
    }
    addRow(t("about.stat.span"), spanText(radar.earliest, radar.latest));
    addRow(
      t("about.stat.retention"),
      radar.retention_days == null ? DASH : `${radar.retention_days} ${t("about.unit.days")}`,
    );
    // Promoted into a figure above when lightning is off — don't say it twice.
    if (lightning.enabled) addRow(t("about.stat.size"), gb(storage.bytes));
  }

  function showStatsError() {
    statsList.replaceChildren();
    figures.replaceChildren();
    liveLine.replaceChildren();
    statsError.hidden = false;
  }

  async function loadStats() {
    try {
      const resp = await fetch("/api/stats");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      lastStats = await resp.json();
      renderStats(lastStats);
    } catch {
      lastStats = null;
      showStatsError(); // prose still shows; only the stats area degrades
    }
  }

  // -- focus trap -------------------------------------------------------------

  function focusable() {
    return [
      ...dialog.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'),
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

  // -- open / close -----------------------------------------------------------

  function open() {
    if (isOpen) return;
    isOpen = true;
    buildProse();
    overlay.hidden = false;
    dialog.focus();
    loadStats();
  }

  function close() {
    if (!isOpen) return;
    isOpen = false;
    overlay.hidden = true;
    if (trigger) trigger.focus(); // restore focus to the activator
  }

  function refreshI18n() {
    syncExplainerLink(); // before the guard: the href is live whether open or not
    // Static labels (title/heading/close/error) are handled by data-i18n; only
    // the JS-built prose, credit and stat rows need an explicit re-render.
    if (!isOpen) return;
    buildProse();
    if (lastStats) renderStats(lastStats);
  }

  // -- wiring -----------------------------------------------------------------

  syncExplainerLink(); // i18n is loaded before initAbout(), so the locale is known
  if (trigger) trigger.addEventListener("click", open);
  if (closeBtn) closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close(); // backdrop click; clicks inside don't close
  });
  document.addEventListener("keydown", onKeydown);

  return { open, close, refreshI18n };
}
