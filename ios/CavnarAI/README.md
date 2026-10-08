# Cavnar AI — iOS app

Native SwiftUI companion app for owners, managers and staff: Home, Reviews, Labor, Food Cost, Marketing, Intel, Ask Cavnar, Account and the Staff portal (one folder per module under `CavnarAI/Features/`). Talks to the Flask backend's `/mobile/api/...` routes (see `mobile_api.py` in the repo root).

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

## Targets

- `CavnarAI` — the app.
- `CavnarWidgets` — the Home and Lock Screen widgets (waiting · last night, labor and food cost, the staff "Next shift"), the Live Activities (the queued-send countdown, "Building next week", "Tonight's service") and, on iOS 18, the Control Center buttons. Reads only what the app writes into the `group.ai.cavnar.CavnarAI` app group; never signs in.
- `CavnarShare` — "Send to Cavnar AI" from Mail, Files or Photos: a PDF or photo of a supplier invoice goes to Food Cost → Invoices. It reads the owner session from the `$(AppIdentifierPrefix)ai.cavnar.CavnarAI.shared` keychain group, which the app writes (`Keychain.mirrorSessionForExtensions`); the widgets are not in that group. Automatic signing adds the Keychain Sharing capability to both App IDs on the next device build.

## iPad

The app is universal (`TARGETED_DEVICE_FAMILY "1,2"` on every target): the
iPad runs in every orientation, in Split View and Slide Over
(`UIRequiresFullScreen` false); the iPhone stays portrait-only. On a regular
horizontal size class `RootView` (and `StaffPortalView`) swaps the tab bar
for a `NavigationSplitView` sidebar (`Core/AppSidebar.swift`) over the same
state, so deep links and pushes land the same way; a compact width — every
iPhone, and a narrowed iPad — keeps the tab bar exactly. Readable widths,
two-up layouts, form sheets, hover and the keyboard shortcuts (⌘1–⌘4, ⌘K,
⌘N, ⌘R) live in `DesignSystem/CavnarAdaptiveLayout.swift`; the rules are in
`DESIGN_SYSTEM.md` → iPad. Build for both from the generic destination:

```bash
xcodebuild -project CavnarAI.xcodeproj -scheme CavnarAI \
  -destination 'generic/platform=iOS Simulator' build
```

The app refreshes the widgets in the background (`BackgroundRefresh.swift`: a `BGAppRefreshTask`, `ai.cavnar.CavnarAI.refresh`, and the server's silent pushes) — `fetch` and `remote-notification` are both in `UIBackgroundModes`.

## Tests

```bash
xcodebuild -project CavnarAI.xcodeproj -scheme CavnarAI \
  -destination 'platform=iOS Simulator,name=iPhone 17' test
```
