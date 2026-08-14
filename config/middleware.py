"""Dev-only middleware."""

from __future__ import annotations


class NoCacheStaticMiddleware:
    """Tell browsers never to cache the SPA shell + static assets in dev.

    The vanilla-JS frontend has no build step / content hashing, so a browser that
    caches ``/static/js/*.js`` (Django's dev static handler sends no Cache-Control)
    happily keeps a stale ES module across edits — e.g. the radar timeline updates
    but a just-changed overlay module does not. ``no-store`` forces a fresh fetch
    every load. Wired only in ``config.settings.local``; prod has no content
    hashing either, so Nginx serves the same assets with ``Cache-Control: no-cache``
    (revalidate via ETag/304) and the SPA shell with ``no-store`` — see
    ``compose/production/nginx/default.conf``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path == "/" or request.path.startswith("/static/"):
            response["Cache-Control"] = "no-store, must-revalidate"
        return response
