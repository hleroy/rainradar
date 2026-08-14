import mimetypes

from django.conf import settings
from django.contrib import admin
from django.http import FileResponse
from django.urls import include
from django.urls import path

# Serve.webmanifest with the right content type in dev (prod sets it in Nginx).
mimetypes.add_type("application/manifest+json", ".webmanifest")


def index(_request):
    """Serve the vanilla-JS SPA entrypoint (dev only; prod uses Nginx).

    Registered at the *exact* empty path, not as a catch-all: the app uses no
    History API routing, so every other URL is a genuine 404 and must say so.
    Django gives that for free here; Nginx needs it spelled out (see the
    ``location = /`` + terminal ``location / { return 404; }`` pair in
    compose/production/nginx/default.conf), because ``try_files /index.html``
    always resolves and would answer every unknown path with a 200 shell.
    """
    return FileResponse(
        (settings.FRONTEND_DIR / "index.html").open("rb"),
        content_type="text/html",
    )


def _crawler_file(name, content_type):
    """Build a view serving one of the crawler-facing files (dev only).

    ``robots.txt`` and ``sitemap.xml`` are plain static files under ``frontend/``,
    so collectstatic already puts them at the Nginx document root in production
    (which serves them from its own ``location`` blocks). These routes exist only
    so dev and prod answer the same URLs.
    """

    def view(_request):
        return FileResponse(
            (settings.FRONTEND_DIR / name).open("rb"),
            content_type=content_type,
        )

    return view


def _explainer(name):
    """Build a view serving one of the standalone explainer pages (dev only).

    They are plain static documents, not part of the SPA: each links its own
    CSS/JS/fonts under /static/ so it satisfies the same strict CSP as the shell
    (no inline script or style, no data: font). The French (/apropos) and English
    (/about) pages share every asset — only the prose differs.
    """

    def view(_request):
        return FileResponse(
            (settings.FRONTEND_DIR / name).open("rb"),
            content_type="text/html",
        )

    return view


def service_worker(_request):
    """Serve the service worker at root scope (/sw.js) so it controls the whole app.

    Dev only; prod serves it via an Nginx ``location = /sw.js``. The
    ``no-cache`` header makes the browser revalidate the SW bytes every load so a new
    deploy's worker is picked up at once; ``Service-Worker-Allowed: /`` permits the
    root scope explicitly.
    """
    resp = FileResponse(
        (settings.FRONTEND_DIR / "sw.js").open("rb"),
        content_type="text/javascript",
    )
    resp["Cache-Control"] = "no-cache"
    resp["Service-Worker-Allowed"] = "/"
    return resp


urlpatterns = [
    # API + health (radar app, provider-agnostic)
    path("", include("radar.urls")),
    # Service worker at root scope (dev; prod uses Nginx) — before the catch-all home.
    path("sw.js", service_worker, name="service-worker"),
    # Standalone explainer, FR + EN (dev; prod uses Nginx) — also before the catch-all.
    path("apropos", _explainer("apropos.html"), name="apropos"),
    path("about", _explainer("about.html"), name="about"),
    # Crawler-facing files (dev; prod uses Nginx).
    path("robots.txt", _crawler_file("robots.txt", "text/plain"), name="robots"),
    path("sitemap.xml", _crawler_file("sitemap.xml", "application/xml"), name="sitemap"),
    # Django admin
    path(settings.ADMIN_URL, admin.site.urls),
    # Frontend entrypoint at / (dev: Django; prod: Nginx serves it). Exact match,
    # not a catch-all — unknown paths 404 (see index's docstring).
    path("", index, name="home"),
]

if settings.DEBUG:
    # Serve /static/... via Django in dev so the SPA assets load without Nginx.
    from django.contrib.staticfiles.urls import staticfiles_urlpatterns

    urlpatterns += staticfiles_urlpatterns()
