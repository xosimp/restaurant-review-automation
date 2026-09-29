"""
net_safety.py — outbound HTTP to an address someone else chose.

A customer's webhook URL, a menu page from a Google listing, a link an admin
pasted: the server fetches whatever it is given, so each is a way to make it
read its own internal network (cloud metadata, Railway's private network,
localhost services). The guards that existed resolved the host, checked the
addresses, and then let `requests` resolve it AGAIN to connect — a name whose
DNS answer changes between the two (DNS rebinding) passed the check and
connected somewhere private. Webhook delivery also followed redirects and
re-checked nothing (#156).

Here the host is resolved ONCE, every address it resolves to must be
globally routable, and the connection is made to the vetted address itself —
the original hostname rides along as the Host header and as the TLS server
name, so certificates still verify against the real name. Redirects are
never followed: a caller that wants to follow one (competitor._get_public)
passes each hop back through here, so every hop is vetted. A timeout is
required, as everywhere (scripts/check_timeouts.py).

    safe_get(url, timeout=..., **kw)  /  safe_post(url, timeout=..., **kw)
    vet(url) -> (scheme, host, port, ip)   raises UnsafeURL
"""
import ipaddress
import socket
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

# Names that are internal whatever they resolve to.
_BLOCKED_NAMES = {"localhost", "metadata.google.internal", "metadata", "instance-data"}


class UnsafeURL(ValueError):
    """The URL is malformed, or resolves to an address that is not public."""


def _public(ip) -> bool:
    """Globally routable, and not multicast. is_global excludes private,
    loopback, link-local (169.254.169.254 — cloud metadata), reserved,
    unspecified and the carrier-grade NAT space (100.64/10); an IPv6
    address carrying an IPv4 one is judged by the IPv4 inside it."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_global) and not ip.is_multicast


def vet(url):
    """(scheme, host, port, ip) for a URL that may be fetched, resolving the
    host once. Raises UnsafeURL naming the reason."""
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        raise UnsafeURL("Could not parse URL")
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL("URL must start with http:// or https://")
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise UnsafeURL("URL must include a host")
    if host in _BLOCKED_NAMES or host.endswith(".internal") or host.endswith(".local"):
        raise UnsafeURL("That host isn't allowed")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise UnsafeURL("That port isn't valid")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception:
        raise UnsafeURL("Could not resolve host")
    addrs = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
        except ValueError:
            continue
    if not addrs:
        raise UnsafeURL("Could not resolve host")
    # EVERY address must be public: DNS can return several, and an attacker
    # needs only one to point inward.
    if not all(_public(a) for a in addrs):
        raise UnsafeURL("That URL points to a private or internal address")
    return parsed.scheme, host, port, str(addrs[0])


class _PinnedAdapter(HTTPAdapter):
    """Connects to the vetted IP while TLS verifies the original hostname
    (SNI and certificate checks both use `server_hostname`)."""

    def __init__(self, hostname, **kw):
        self._hostname = hostname
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["server_hostname"] = self._hostname
        pool_kwargs["assert_hostname"] = self._hostname
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def safe_request(method, url, *, timeout=None, headers=None, **kw):
    """One request to `url` over a connection pinned to the address vetted
    for it. Never follows a redirect (the 3xx comes back to the caller).
    Raises UnsafeURL before any connection when the URL is not allowed."""
    if timeout is None:
        raise ValueError("net_safety requires a timeout")
    kw.pop("allow_redirects", None)
    scheme, host, port, ip = vet(url)
    parsed = urlparse(str(url).strip())
    ip_host = f"[{ip}]" if ":" in ip else ip
    default_port = 443 if scheme == "https" else 80
    pinned = parsed._replace(netloc=ip_host if port == default_port else f"{ip_host}:{port}").geturl()
    hdrs = dict(headers or {})
    hdrs["Host"] = host if port == default_port else f"{host}:{port}"
    with requests.Session() as session:
        session.trust_env = False          # no proxy or netrc from the environment
        session.mount("https://", _PinnedAdapter(host))
        return session.request(method, pinned, headers=hdrs, timeout=timeout, allow_redirects=False, **kw)


def safe_get(url, **kw):
    return safe_request("GET", url, **kw)


def safe_post(url, **kw):
    return safe_request("POST", url, **kw)
