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
