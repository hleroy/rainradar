// Rain Radar service worker. Hand-written, classic (no imports, no build
// step). Precaches ONLY the static app shell so the UI boots instantly and survives
// a flaky/absent network; data layers fall through to the network unchanged.
//
// Non-negotiables (see CLAUDE.md):
//   - GET-only, same-origin-only. It NEVER calls respondWith for `/api/…`,
//     `/tiles/…`, the lightning SSE (`/api/lightning/stream`), or the cross-origin
//     OSM base — those always hit the network, exactly as before. No new upstream
//     request, no contact with tilecache.rainviewer.com or Blitzortung.
//   - App-shell-only cache. When a shell asset is added/renamed, bump BOTH
//     STATIC_SHELL below AND CACHE_VERSION; old caches are pruned on `activate`.
//   - Auto-update is silent (skipWaiting + clients.claim); the page reloads itself
//     on `controllerchange` (main.js), never on first install, and deferred while a
//     video export is mid-render.

const CACHE_VERSION = "v25"; // bump on every release that changes shell assets
const CACHE_NAME = `rainradar-shell-${CACHE_VERSION}`;

// Explicit (no build manifest exists — vanilla ES modules, no hashing). Every entry
// must resolve to a real file; a 404 makes addAll() fail and the old SW stays active.
// A test asserts each path (except "/") maps to a file under frontend/.
//
// Deliberately absent: /robots.txt, /sitemap.xml and /static/img/og-image.jpg. Those
// are fetched by crawlers and link scrapers out of band, never by the app, so
// precaching them would cost every install ~160 KB for bytes no user ever reads.
const STATIC_SHELL = [
  "/", // the navigation fallback (index.html)
  "/static/css/app.css",
  "/static/js/main.js",
  "/static/js/i18n.js",
  "/static/js/geo.js",
  "/static/js/radar.js",
  "/static/js/lightning.js",
  "/static/js/about.js",
  "/static/js/clip.js",
  "/static/js/alerts.js",
  "/static/js/datesheet.js",
  "/static/js/settings.js",
  "/static/js/onefingerzoom.js",
  "/static/vendor/leaflet.js",
  "/static/vendor/leaflet.css",
  "/static/vendor/mediabunny/mediabunny.js",
  "/static/i18n/fr.json",
  "/static/i18n/en.json",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-512.png",
  "/static/icons/apple-touch-icon-180.png",
  "/static/favicon.svg",
  // The About dialog's /apropos teaser. Part of the shell (unlike the /apropos
  // page's own full-size imagery, which stays out): 64 KB total, and a broken
  // miniature in an offline-opened dialog is a worse trade than the bytes.
  "/static/img/apropos-mini-carte.webp",
  "/static/img/apropos-mini-halo.webp",
  "/static/img/apropos-mini-pluie.webp",
  "/static/img/apropos-mini-eclairs.webp",
];

self.addEventListener("install", (event) => {
  self.skipWaiting(); // silent auto-update: take over without waiting
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      // { cache: "reload" } bypasses the HTTP cache so a new version always stores
      // fresh bytes. addAll is atomic-ish: any 404 fails the whole install.
      cache.addAll(STATIC_SHELL.map((url) => new Request(url, { cache: "reload" }))),
    ),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k.startsWith("rainradar-shell-") && k !== CACHE_NAME)
            .map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()), // → triggers controllerchange in clients
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // mutations: never intercept
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // OSM & any cross-origin: never

  // Never intercept data paths — radar tiles, JSON API, lightning SSE/history. They
  // must reach the network unchanged (the SSE stream must stay unbuffered/uncached).
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/tiles/")) return;

  // Navigations (the shell document): network-first, fall back to the cached "/".
  // The explainer (/apropos, and its English twin /about) is a document of its own,
  // not the shell: answering it with the app would put the radar UI behind an
  // /apropos URL, so let it fall through to the browser's own offline page instead.
  // Its /static/ assets are still cached opportunistically below, but the page is
  // deliberately not part of the shell — precaching ~550 KB of imagery for a
  // secondary page would bloat every install.
  if (req.mode === "navigate") {
    if (url.pathname === "/apropos" || url.pathname === "/about") return;
    event.respondWith(fetch(req).catch(() => caches.match("/", { ignoreSearch: true })));
    return;
  }

  // Static shell assets (/static/…, manifest, icons, favicon): stale-while-revalidate.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(req));
    return;
  }
  // Anything else same-origin (e.g. /sw.js, /healthz): let it hit the network.
});

async function staleWhileRevalidate(req) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(req, { ignoreSearch: true });
  const network = fetch(req)
    .then((res) => {
      // Cache only complete, direct 200s: res.ok would also admit a 206
      // Partial Content body, which would then be served as the full asset
      // forever. Store under a query-stripped key so lookups (ignoreSearch)
      // and stores agree — otherwise ?query variants pile up as duplicates.
      if (res && res.status === 200 && !res.redirected) {
        const key = new URL(req.url);
        key.search = "";
        cache.put(key.href, res.clone());
      }
      return res;
    })
    .catch(() => cached); // offline → whatever we have
  return cached || network; // instant if cached, else wait for the network
}

// Storm alerts: a click on a notification (foreground or Web Push)
// focuses an existing app window or opens one. GET-only fetch rules
// above are untouched — this is a notification handler, not a network path.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      const client = list.find((c) => "focus" in c);
      return client ? client.focus() : self.clients.openWindow("/");
    }),
  );
});

// Storm alerts — background delivery. The push evaluator (archiver)
// sends a small JSON payload with the already-localized title/body; we display it
// verbatim. userVisibleOnly requires we always show a notification, so fall back to a
// generic title if the payload is missing/unparsable. No fetch, no network here.
self.addEventListener("push", (event) => {
  let data = null;
  try {
    data = event.data ? event.data.json() : null;
  } catch {
    data = null; // malformed payload → generic notification below
  }
  event.waitUntil(
    self.registration.showNotification((data && data.title) || "Rain Radar", {
      body: (data && data.body) || "",
      tag: (data && data.tag) || "rainradar-alert",
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
    }),
  );
});
