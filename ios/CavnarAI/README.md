# Cavnar AI — iOS app

Native SwiftUI companion app for owners/managers (v1: Home, Reviews, Ask Cavnar, Food Cost). Talks to the Flask backend's `/mobile/api/...` routes (see `mobile_api.py` in the repo root).

## Setup

The `.xcodeproj` is generated, not committed — regenerate it after cloning or after editing `project.yml`:

```bash
brew install xcodegen   # one-time
cd ios/CavnarAI
xcodegen generate
open CavnarAI.xcodeproj
```

## Running against a local backend

By default, Debug builds point at `http://localhost:5000`. If that port is taken (e.g. by macOS's AirPlay Receiver), run the Flask app on another port and override the app's target at launch:

```bash
PORT=5050 python3 hosted_dashboard.py
```

Then in Xcode: Product ▸ Scheme ▸ Edit Scheme… ▸ Run ▸ Arguments ▸ Environment Variables, add `CAVNAR_API_BASE_URL` = `http://localhost:5050`.

## Tests

```bash
xcodebuild -project CavnarAI.xcodeproj -scheme CavnarAI \
  -destination 'platform=iOS Simulator,name=iPhone 17' test
```
