# Cavnar AI — iOS app

Native SwiftUI companion app for owners/managers (v1: Home, Reviews, Ask Cavnar, Food Cost). Talks to the Flask backend's `/mobile/api/...` routes (see `mobile_api.py` in the repo root).

## Setup

The `.xcodeproj` is generated, not committed — regenerate it after cloning or after editing `project.yml`:

```bash
brew install xcodegen   # one-time
cd ios/CavnarAI
./generate.sh
open CavnarAI.xcodeproj
```

Use `./generate.sh`, not `xcodegen generate` directly. The scheme carries the
dev API base URL, substituted from `$CAVNAR_DEV_API_BASE_URL` at generate
time — generate from a shell that hasn't exported it and the scheme silently
gets the literal `${CAVNAR_DEV_API_BASE_URL}` placeholder instead, the app
falls back to localhost, and every request fails with nothing on screen saying
why. The script resolves the URL, then checks the substitution landed.

## Running against a local backend

Debug builds fall back to `http://localhost:5050` — the port this project
runs on, deliberately not 5000, which macOS's AirPlay Receiver already owns
(it answers unknown paths with a 4xx, so a build pointed there reports
"Something went wrong (404)" instead of failing as a connection error).

```bash
PORT=5050 python3 hosted_dashboard.py
```

The Simulator can reach that directly. A physical device cannot — put the
address it should use in `ios/CavnarAI/.dev-url` (gitignored, survives
regeneration), either your Mac's LAN address or an ngrok tunnel:

```bash
echo 'http://192.168.1.211:5050' > ios/CavnarAI/.dev-url   # same wifi
./generate.sh
```

## Tests

```bash
xcodebuild -project CavnarAI.xcodeproj -scheme CavnarAI \
  -destination 'platform=iOS Simulator,name=iPhone 17' test
```
