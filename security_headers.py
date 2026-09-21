"""The response headers every page carries. Factored out of
hosted_dashboard so they can be tested without importing the whole app."""

CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://static.cloudflareinsights.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.gstatic.com; "
    "font-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com; "
    "img-src 'self' data: https:; "
    "connect-src 'self' https://api.anthropic.com; "
    "frame-ancestors 'none';"
)


def apply(response, secure=None):
    """Add the security headers. `secure` (whether cookies require the
    Secure flag, i.e. TLS terminates in front of us) decides HSTS; when
    None it is read from auth.cookies_require_secure()."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = CSP
    if secure is None:
        try:
            from auth import cookies_require_secure
            secure = cookies_require_secure()
        except Exception:
            secure = False
    if secure:
        # A year, subdomains included: the first-visit downgrade window
        # was the one transport gap the security audit found.
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response
