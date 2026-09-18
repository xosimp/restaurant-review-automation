"""
http_layer.py — what every response gets on the way out.

Extracted from hosted_dashboard.py so it can be exercised without booting the
app. Importing hosted_dashboard initialises the database, seeds demo data and
starts the scheduler thread, and re-importing it inside a test re-runs
csrf_protect() on blueprints Flask has already registered — so the response
layer had no way to be tested directly. It is now a module you can attach to
a throwaway Flask app.

Two concerns, both measured in audit #17:

  COMPRESSION. dashboard.html renders to ~989 KB of HTML, CSS and JS in one
  document and shipped uncompressed to every client on every load — /login
  returned byte-identical responses with and without Accept-Encoding: gzip.

  CACHE HEADERS. Nothing set Cache-Control at all, which leaves every
  intermediary free to apply its own heuristic freshness to authenticated
  JSON carrying one restaurant's labor cost and revenue.
"""
import gzip

from flask import request

_COMPRESSIBLE = ("text/html", "text/css", "text/plain", "text/xml",
                 "application/json", "application/javascript",
                 "text/javascript", "image/svg+xml")

# Below this the gzip header costs more than the body saves, and the CPU is
# wasted on every small JSON reply.
COMPRESS_MIN_BYTES = 1024
COMPRESS_LEVEL = 6      # the knee of the size/CPU curve for text


def compress_response(response):
    """gzip text responses that are worth compressing.

    Never touches: a streamed response (direct_passthrough — the body has not
    been produced yet and reading it here would defeat streaming), anything
    already encoded, a non-text type, or a body small enough that the header
    outweighs the saving.

    The streaming exclusion is explicit rather than trusted to a library
    default because there is exactly one SSE endpoint in the product — Ask
    Cavnar's progress stream — and buffering it to compress it would turn
    live tool progress back into the silent spinner the streaming exists to
    replace.
    """
    try:
        if response.direct_passthrough:
            return response
        if response.mimetype == "text/event-stream":
            return response
        if response.headers.get("Content-Encoding"):
            return response
        if response.status_code < 200 or response.status_code >= 300:
            return response
        if response.mimetype not in _COMPRESSIBLE:
            return response

        accept = (request.headers.get("Accept-Encoding") or "").lower()
        if "gzip" not in accept:
            # Still tell caches the response varies by encoding, or a proxy
            # can serve a gzipped body to a client that never asked for one.
            _add_vary(response)
            return response

        body = response.get_data()
        if len(body) < COMPRESS_MIN_BYTES:
            _add_vary(response)
            return response

        packed = gzip.compress(body, compresslevel=COMPRESS_LEVEL)
        # A body that grew is a body that should not have been compressed.
        if len(packed) >= len(body):
            _add_vary(response)
            return response

        response.set_data(packed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(packed))
        _add_vary(response)
    except Exception:
        # Compression must never be the reason a page fails to render.
        return response
    return response


def _add_vary(response):
    vary = response.headers.get("Vary")
    if not vary:
        response.headers["Vary"] = "Accept-Encoding"
    elif "accept-encoding" not in vary.lower():
        response.headers["Vary"] = f"{vary}, Accept-Encoding"


def add_cache_headers(response):
    """Say explicitly what may be cached, because nothing did.

    One rule, and deliberately only one: anything under /api or /mobile/api is
    per-tenant and may change on the next write, so it is no-store. That is a
    correctness and privacy statement, not a performance one.

    Static assets are NOT given a long max-age, even though they are the
    obvious candidate. They are referenced by bare path — /static/cavnar-orb.js,
    no hash and no version query — so a far-future expiry would strand a
    JavaScript fix in browser caches with no way to bust it. Flask's own
    `no-cache` keeps the revalidation round trip but returns a bodiless 304,
    which is most of the saving without the hazard. Worth revisiting only
    alongside cache-busted asset URLs.

    HTML pages are left alone too: caching them would risk serving a stale
    dashboard after an action, and the win there came from compression.
    """
    try:
        path = request.path or ""
        if response.headers.get("Cache-Control"):
            return response
        if path.startswith("/api/") or path.startswith("/mobile/api/"):
            response.headers["Cache-Control"] = "no-store"
    except Exception:
        return response
    return response


def register(app):
    """Attach both handlers. Order matters: cache headers are set on the
    response object, compression rewrites its body — running compression last
    means it sees the final headers."""
    app.after_request(add_cache_headers)
    app.after_request(compress_response)
    return app
