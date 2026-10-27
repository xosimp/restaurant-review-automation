"""The network guard for a throwaway app process (blind re-audit 10/27/26,
B1 F7/F12).

Python imports `sitecustomize` at start-up from anywhere on sys.path, so a
process started with PYTHONPATH=scripts/boot_guard and CAVNAR_NET_GUARD=1 —
scripts/boot_check.py's boots, gunicorn's master and every process it forks
or spawns (the web worker, `worker.py --queue`) — can open a connection only
to this machine (loopback, a unix socket) or a host named in
CAVNAR_NET_GUARD_ALLOW (the scratch Postgres). Any other connect fails as an
unreachable network would (ConnectionRefusedError), and the attempt is
appended to CAVNAR_NET_GUARD_LOG so the boot check can fail on it: a keyless
boot has no business reaching a provider.

Inert without CAVNAR_NET_GUARD=1. Imports nothing of the app.
"""
import os
import socket

if os.environ.get("CAVNAR_NET_GUARD") == "1":
    import ipaddress

    _ALLOW = {h.strip().lower() for h in (os.environ.get("CAVNAR_NET_GUARD_ALLOW") or "").split(",") if h.strip()}
    _LOG = os.environ.get("CAVNAR_NET_GUARD_LOG") or ""
    _real_connect = socket.socket.connect
    _real_connect_ex = socket.socket.connect_ex

    def _local(host):
        host = (host.decode() if isinstance(host, bytes) else str(host or "")).strip("[]").lower()
        if host in ("", "localhost") or host.endswith(".localhost") or host in _ALLOW:
            return True
        try:
            ip = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return ip.is_loopback or ip.is_unspecified

    def _allowed(sock, address):
        if getattr(socket, "AF_UNIX", None) is not None and sock.family == socket.AF_UNIX:
            return True
        if not isinstance(address, tuple) or not address:
            return True
        return _local(address[0])

    def _note(address):
        if not _LOG:
            return
        try:
            with open(_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()} {address[0]}:{address[1] if len(address) > 1 else ''}\n")
        except OSError:
            pass

    def _guarded_connect(self, address):
        if not _allowed(self, address):
            _note(address)
            raise ConnectionRefusedError(f"boot guard: no connection to {address!r} from a keyless boot")
        return _real_connect(self, address)

    def _guarded_connect_ex(self, address):
        if not _allowed(self, address):
            _note(address)
            import errno
            return errno.ECONNREFUSED
        return _real_connect_ex(self, address)

    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
