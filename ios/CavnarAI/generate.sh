#!/usr/bin/env bash
# Regenerate CavnarAI.xcodeproj without losing the dev API base URL.
#
# project.yml puts ${CAVNAR_DEV_API_BASE_URL} into the scheme, substituted by
# xcodegen AT GENERATE TIME. Run `xcodegen generate` from any shell that
# hasn't exported it — a tool, a script, an editor's terminal — and the scheme
# silently gets the literal placeholder instead. The app then falls back to
# localhost, every request fails, and nothing on screen says why. That has now
# cost two debugging sessions, so: generate through this, not through xcodegen
# directly.
#
# The URL is resolved from, in order:
#   1. $CAVNAR_DEV_API_BASE_URL, if already exported
#   2. ios/CavnarAI/.dev-url — one line, gitignored, survives regeneration
#   3. BASE_URL in the repo's .env (the ngrok tunnel used for device testing)
# and the script refuses to run rather than writing a placeholder it knows is
# broken.
set -euo pipefail
cd "$(dirname "$0")"

# --lan: point the device at whatever address this Mac currently has, on
# whatever network it is currently on. The saved .dev-url is a fixed address
# from wherever it was last written — take it to a client's wifi, or tether
# to an iPhone hotspot over USB (the Mac lands on 172.20.10.x), and it is
# still the old one, so every request from the device fails with nothing on
# screen explaining why. This resolves it fresh instead.
if [ "${1:-}" = "--lan" ]; then
  iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
  ip="$(ipconfig getifaddr "${iface:-en0}" 2>/dev/null || true)"
  if [ -z "$ip" ]; then
    for i in $(ifconfig -l); do
      ip="$(ipconfig getifaddr "$i" 2>/dev/null || true)"
      [ -n "$ip" ] && iface="$i" && break
    done
  fi
  if [ -z "$ip" ]; then
    echo "ERROR: this Mac has no IPv4 address on any interface — is it online?" >&2
    exit 1
  fi
  echo "http://$ip:5050" > .dev-url
  echo "Using this Mac's current address on $iface: http://$ip:5050"
fi

url="${CAVNAR_DEV_API_BASE_URL:-}"
[ -z "$url" ] && [ -f .dev-url ] && url="$(tr -d '[:space:]' < .dev-url)"
[ -z "$url" ] && [ -f ../../.env ] && url="$(grep -m1 '^BASE_URL=' ../../.env | cut -d= -f2- | tr -d '[:space:]')"

if [ -z "$url" ]; then
  cat >&2 <<'MSG'
CAVNAR_DEV_API_BASE_URL is not set and no fallback was found.

Put the address your device should talk to in ios/CavnarAI/.dev-url — your
ngrok tunnel, or your Mac's LAN address, e.g.

    echo 'http://192.168.1.211:5050' > ios/CavnarAI/.dev-url

then run this again. (The Simulator can use http://localhost:5050.)
MSG
  exit 1
fi

echo "Generating with CAVNAR_DEV_API_BASE_URL=$url"
CAVNAR_DEV_API_BASE_URL="$url" xcodegen generate

# Prove the substitution actually landed — the whole point of this script.
if grep -q '${CAVNAR_DEV_API_BASE_URL}' CavnarAI.xcodeproj/xcshareddata/xcschemes/CavnarAI.xcscheme; then
  echo "ERROR: the scheme still holds the unsubstituted placeholder." >&2
  exit 1
fi
echo "Scheme points at $url"

# A LAN address is only reachable if the server is actually listening on it.
# Bound to 127.0.0.1 the device gets a connection refused, which looks
# identical to "the app is broken".
case "$url" in
  http://127.0.0.1*|http://localhost*)
    echo "NOTE: that address only works in the Simulator. For a real device run:"
    echo "        ./generate.sh --lan" ;;
  http://*)
    host="${url#http://}"; host="${host%%:*}"
    if ! nc -z -G 2 "$host" 5050 >/dev/null 2>&1; then
      echo "WARNING: nothing is answering on $host:5050." >&2
      echo "         Start the server with:  PORT=5050 HOST=0.0.0.0 python3 hosted_dashboard.py" >&2
    else
      echo "Server is up on $host:5050"
    fi ;;
esac
