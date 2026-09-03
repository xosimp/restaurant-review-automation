# Audit Pass 1 — Security & API Audit

**Target:** `ios/CavnarAI/CavnarAI/` — 121 Swift files, 25,590 LOC
**Date:** 2026-09-03
**Scope:** credential handling, data-at-rest, transport security, log leakage, URL/deep-link surface


> **Remediation status (ff937cc): 9/9 findings fixed.** All fixed.
> Verified by: clean `xcodebuild` (0 errors, 0 warnings), 646 backend tests passing,
> `scripts/check_colors.py` clean, and a per-finding grep confirming each original
> code signature is gone. Findings below are kept as written (plus explicit
> **Correction** notes where the original analysis was wrong) so the reasoning
> stays auditable rather than being rewritten after the fact.

---

## Executive summary

The credential-handling core of this app is **genuinely well built** — there are no hardcoded secrets in the Swift target, the session token and 2FA device token go to Keychain (not UserDefaults), and ATS is not globally disabled. Those are the mistakes that sink most indie iOS apps, and they were avoided.

The real exposure is in three places the code never considered: **transport trust** (no pinning, and a server-supplied URL opened blindly), **data at rest** (employee PII cached in plaintext plists that survive logout), and **a public dev tunnel committed to the repo**.

| # | Severity | Finding |
|---|---|---|
| 1.1 | CRITICAL | Public ngrok tunnel to the dev Mac committed into `project.yml` |
| 1.2 | CRITICAL | Employee PII cached in plaintext UserDefaults, survives logout |
| 1.3 | WARNING | No TLS certificate pinning on any request |
| 1.4 | WARNING | Server-supplied billing URL opened without scheme/host validation |
| 1.5 | WARNING | API responses persisted to disk by the default `URLCache` |
| 1.6 | WARNING | No app-switcher snapshot protection (lock fires too late) |
| 1.7 | WARNING | Biometric gate fails open when no passcode is enrolled |
| 1.8 | WARNING | `aps-environment: development` shipped in entitlements |
| 1.9 | OPTIMIZATION | Push payload fields trusted without validation |
| — | ✅ PASS | No hardcoded secrets; Keychain used correctly; only 2 `print()` calls |

---

## 1.1 CRITICAL — Public ngrok tunnel committed into the build scheme

**File:** `ios/CavnarAI/project.yml` line 108
**Also:** `ios/CavnarAI/CavnarAI/Core/AppEnvironment.swift` lines 9–19

Every Debug build compiled from this repo points at a **public HTTPS tunnel to the developer's Mac**, and that address is checked into version control:

```yaml
environmentVariables:
  CAVNAR_API_BASE_URL: "https://4f87-208-44-38-158.ngrok-free.app"
```

That tunnel currently fronts a Flask dev server holding **live client data** (real restaurants, real reviews, real session tokens). ngrok free-tier URLs are unauthenticated at the tunnel layer — anyone who reads this repo, or a fork, or a leaked laptop, has the address. The tunnel has been running continuously since Aug 8, so this is not a short-lived accident; it is effectively a persistent, publicly-addressable production-data endpoint whose address lives in git.

**Before** (`project.yml`):
```yaml
    run:
      config: Debug
      environmentVariables:
        CAVNAR_API_BASE_URL: "https://4f87-208-44-38-158.ngrok-free.app"
```

**After** — read the tunnel from an untracked local file so the address never enters git:
```yaml
    run:
      config: Debug
      # Set CAVNAR_API_BASE_URL in Xcode's scheme editor, or export it in
      # a local .xcconfig that is gitignored. Never commit a tunnel URL:
      # ngrok free-tier tunnels are unauthenticated and this one fronts a
      # server holding live client data.
      environmentVariables: {}
```

Add to `.gitignore` and create `Local.xcconfig` (untracked):
```
// Local.xcconfig — gitignored
CAVNAR_API_BASE_URL = https://your-current-tunnel.ngrok-free.app
```

**Additionally**, require auth at the tunnel edge so the URL alone is not access. ngrok supports this natively:
```bash
ngrok http 5050 --basic-auth "dev:$(openssl rand -base64 24)"
```

---

## 1.2 CRITICAL — Employee PII cached in plaintext UserDefaults, and it survives logout

**Files:**
- `Features/Labor/LaborViewModel.swift` lines 288–299, 350–360
- `Features/Labor/LaborAnalyticsViewModel.swift` lines 58–83
- `Features/FoodCost/FoodCostAnalyticsViewModel.swift` lines 33–41
- `Core/SessionStore.swift` lines 277–287 (`clearLocalSession`)

Generated staff schedules are cached to UserDefaults. A `GeneratedSchedule` contains **employee names, assigned shifts, and labor cost figures** — that is staff PII plus restaurant financials. UserDefaults is an unencrypted `.plist` in the app container: readable from an unencrypted device backup, and readable directly on a jailbroken device. It carries no data-protection class, so it is also readable while the device is locked.

```swift
// LaborViewModel.swift:350
func cacheSchedule(_ schedule: GeneratedSchedule) {
    guard let restaurantId, let data = try? Self.cacheEncoder.encode(schedule) else { return }
    UserDefaults.standard.set(data, forKey: Self.scheduleCacheKey(restaurantId))
}
```

**The compounding bug:** `clearLocalSession()` clears the Keychain token but never touches these caches:

```swift
// SessionStore.swift:277 — current
private func clearLocalSession() {
    Keychain.delete(Keychain.Key.sessionToken)
    Task { await client.setToken(nil) }
    token = nil
    currentUser = nil
    isLocked = false
    hasShownHomeIntro = false
    pendingTwoFactorSetupEmail = nil
    pendingTwoFactorSetupMethod = "email"
}
```

So after a manager signs out on a shared back-office iPad, the previous restaurant's staff schedule, wage data and AI insights are **still on disk**, and `configureCaching(restaurantId:)` will happily restore them.

**After** — store cached PII in a file-protected container, and purge on logout:

```swift
// New: Core/SecureCache.swift
import Foundation

/// Disk cache for anything containing staff PII or financials. Unlike
/// UserDefaults (an unencrypted plist with no data-protection class), these
/// files are encrypted at rest and unreadable while the device is locked.
enum SecureCache {
    private static var directory: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = base.appendingPathComponent("SecureCache", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    static func write(_ data: Data, key: String) {
        try? data.write(to: directory.appendingPathComponent(key), options: .completeFileProtection)
    }

    static func read(key: String) -> Data? {
        try? Data(contentsOf: directory.appendingPathComponent(key))
    }

    /// Called from SessionStore.clearLocalSession() — a signed-out device
    /// must not keep the previous account's staff or financial data.
    static func purgeAll() {
        guard let files = try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil) else { return }
        for file in files { try? FileManager.default.removeItem(at: file) }
    }
}
```

```swift
// LaborViewModel.swift:350 — after
func cacheSchedule(_ schedule: GeneratedSchedule) {
    guard let restaurantId, let data = try? Self.cacheEncoder.encode(schedule) else { return }
    SecureCache.write(data, key: Self.scheduleCacheKey(restaurantId))
}
```

```swift
// SessionStore.swift:277 — after
private func clearLocalSession() {
    Keychain.delete(Keychain.Key.sessionToken)
    SecureCache.purgeAll()          // staff schedules, labor stats, AI insights
    Task { await client.setToken(nil) }
    token = nil
    currentUser = nil
    isLocked = false
    hasShownHomeIntro = false
    pendingTwoFactorSetupEmail = nil
    pendingTwoFactorSetupMethod = "email"
}
```

---

## 1.3 WARNING — No TLS certificate pinning

**File:** `Core/APIClient.swift` lines 33–36, 88

```swift
init(baseURL: URL = AppEnvironment.baseURL, session: URLSession = .shared) {
    self.baseURL = baseURL
    self.session = session
}
```

Every request — login credentials, bearer tokens, staff wage data — goes through stock `URLSession.shared` with default trust evaluation. Any CA the device trusts is accepted, so a corporate MDM profile, a rogue root, or a proxy on the restaurant's own Wi-Fi can transparently read and rewrite all traffic. For an app whose responses drive **payment-status UI and an externally-opened billing URL** (see 1.4), that MITM position is directly monetizable.

**After** — pin the leaf public key for the production host:

```swift
// New: Core/PinnedSessionDelegate.swift
import Foundation
import CryptoKit

final class PinnedSessionDelegate: NSObject, URLSessionDelegate {
    /// SHA-256 of the server's SubjectPublicKeyInfo, base64. Generate with:
    ///   openssl s_client -connect dashboard.cavnar.ai:443 </dev/null 2>/dev/null \
    ///     | openssl x509 -pubkey -noout \
    ///     | openssl pkey -pubin -outform der \
    ///     | openssl dgst -sha256 -binary | base64
    /// Pin BOTH the current key and the next rotation key so a cert renewal
    /// does not brick every installed build.
    private let pinnedKeys: Set<String> = ["REPLACE_WITH_CURRENT_SPKI", "REPLACE_WITH_BACKUP_SPKI"]

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge
    ) async -> (URLSession.AuthChallengeDisposition, URLCredential?) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              SecTrustEvaluateWithError(trust, nil),
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let leaf = chain.first,
              let key = SecCertificateCopyKey(leaf),
              let der = SecKeyCopyExternalRepresentation(key, nil) as Data?
        else { return (.cancelAuthenticationChallenge, nil) }

        let digest = Data(SHA256.hash(data: der)).base64EncodedString()
        guard pinnedKeys.contains(digest) else { return (.cancelAuthenticationChallenge, nil) }
        return (.useCredential, URLCredential(trust: trust))
    }
}
```

```swift
// APIClient.swift:33 — after
init(baseURL: URL = AppEnvironment.baseURL, session: URLSession? = nil) {
    self.baseURL = baseURL
    if let session {
        self.session = session
    } else {
        let config = URLSessionConfiguration.ephemeral   // also fixes 1.5
        config.timeoutIntervalForRequest = 20            // also fixes 6.2
        config.waitsForConnectivity = false
        // Pinning applies to the real deployment only — a local/tunnel dev
        // host legitimately presents a different chain.
        let pinned = baseURL.host?.hasSuffix("cavnar.ai") == true
        self.session = URLSession(
            configuration: config,
            delegate: pinned ? PinnedSessionDelegate() : nil,
            delegateQueue: nil
        )
    }
}
```

---

## 1.4 WARNING — Server-supplied billing URL opened without validation

**File:** `Features/Account/AccountBillingDetailView.swift` lines 32–42

```swift
if let urlString = billing.portalURL, let url = URL(string: urlString) {
    divider()
    Link(destination: url) { ... }
}
```

`portalURL` is whatever the backend put in the JSON. `URL(string:)` accepts far more than https — `javascript:`, `itms-apps:`, arbitrary custom schemes registered by other installed apps, or an attacker-controlled https host that renders a convincing Stripe login page. Combined with 1.3 (no pinning), a network attacker can swap this field and phish the owner's Stripe credentials from inside the trusted app UI.

**Before:**
```swift
if let urlString = billing.portalURL, let url = URL(string: urlString) {
```

**After** — allowlist scheme and host at the point of use:
```swift
// Stripe's billing portal only ever lives on these hosts. Anything else in
// this field is either a backend bug or a tampered response — in both cases
// the right move is to not offer the link at all.
private func validatedPortalURL(_ raw: String?) -> URL? {
    guard let raw, let url = URL(string: raw),
          url.scheme?.lowercased() == "https",
          let host = url.host?.lowercased(),
          host == "billing.stripe.com" || host.hasSuffix(".stripe.com")
    else { return nil }
    return url
}

// call site
if let url = validatedPortalURL(billing.portalURL) {
    divider()
    Link(destination: url) { ... }
}
```

---

## 1.5 WARNING — API responses persisted to disk by the default URLCache

**File:** `Core/APIClient.swift` line 33 (`session: URLSession = .shared`)

`URLSession.shared` uses the shared `URLCache`, which writes eligible responses to `Library/Caches/` **unencrypted**. Every GET in this app returns business data — reviews, labor costs, billing summaries, staff schedules — and those response bodies land on disk without the app ever deciding to store them.

**Fix:** the `URLSessionConfiguration.ephemeral` change already shown in 1.3 removes the on-disk cache entirely. If a non-ephemeral session is ever needed, set `config.urlCache = nil` and `config.requestCachePolicy = .reloadIgnoringLocalCacheData` explicitly.

---

## 1.6 WARNING — No app-switcher snapshot protection

**File:** `RootView.swift` lines 130–134

```swift
.onChange(of: scenePhase) { _, newPhase in
    if newPhase == .background {
        sessionStore.lockIfNeeded()
    }
}
```

iOS captures the app-switcher thumbnail during the `.inactive` → `.background` transition. Locking only on `.background` means the snapshot is taken **while the dashboard is still fully rendered**, so revenue figures, review content and staff data sit in the app-switcher card — visible to anyone who picks the phone up, with no authentication.

**After** — cover the screen at `.inactive`, before the snapshot is taken:

```swift
@State private var privacyShieldUp = false

// ... in body, as the outermost overlay:
.overlay {
    if privacyShieldUp {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            CavnarSealMark(size: 64)
        }
        .transition(.opacity)
    }
}
.onChange(of: scenePhase) { _, newPhase in
    switch newPhase {
    case .inactive:
        // Raised BEFORE iOS takes the app-switcher snapshot, so the
        // thumbnail shows the seal rather than the dashboard.
        if sessionStore.isAuthenticated { privacyShieldUp = true }
    case .background:
        sessionStore.lockIfNeeded()
    case .active:
        privacyShieldUp = false
    @unknown default:
        break
    }
}
```

---

## 1.7 WARNING — Biometric gate fails open with no passcode enrolled

**File:** `Core/SessionStore.swift` lines 316–321

```swift
guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &evaluationError) else {
    // No biometrics/passcode configured on this device — don't lock
    // the owner out of their own data over a device limitation.
    isLocked = false
    return true
}
```

The trade-off is deliberate and documented, and locking the owner out permanently would be worse. But as written, the app **silently downgrades to no protection** and never tells anyone. A device with no passcode is exactly the device where an unattended phone on a pass counter exposes everything.

**After** — still fail open (correct), but surface it and give the owner an informed choice:

```swift
/// True when the lock is on but the device cannot enforce it — surfaced in
/// Security settings rather than silently ignored, so an owner running a
/// passcode-less device knows the gate is not actually protecting anything.
private(set) var biometricsUnavailable = false

func unlockWithBiometrics() async -> Bool {
    let context = LAContext()
    var evaluationError: NSError?
    guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &evaluationError) else {
        biometricsUnavailable = true
        isLocked = false
        return true
    }
    biometricsUnavailable = false
    // ... unchanged
}
```

```swift
// AccountSecurityDetailView.swift — deviceLockSection
if sessionStore.biometricsUnavailable {
    Text("This device has no passcode or Face ID set up, so the app can't lock itself. Set a device passcode in Settings to turn this on.")
        .font(.cavnarBody(14))
        .foregroundStyle(Color.cavnarAmber)
}
```

---

## 1.8 WARNING — `aps-environment: development` in shipped entitlements

**File:** `ios/CavnarAI/project.yml` lines 51–53 → `Generated/CavnarAI.entitlements`

```yaml
entitlements:
  properties:
    aps-environment: development
```

The entitlement is hardcoded to `development` for every configuration. A Release/TestFlight/App Store build therefore registers device tokens against the **sandbox APNs namespace** while `PushManager.didRegister` correctly reports `environment = "production"` (it branches on `#if DEBUG`, `PushManager.swift:27–31`). The two disagree, and **every production push silently fails** — no error, no delivery.

**After** — make the entitlement track the configuration:

```yaml
    entitlements:
      path: Generated/CavnarAI.entitlements
      properties:
        aps-environment: $(APS_ENVIRONMENT)
        com.apple.developer.applesignin:
          - Default
    settings:
      configs:
        Debug:
          APS_ENVIRONMENT: development
        Release:
          APS_ENVIRONMENT: production
```

---

## 1.9 OPTIMIZATION — Push payload fields trusted without validation

**File:** `Push/PushManager.swift` lines 70–75

```swift
let userInfo = response.notification.request.content.userInfo
guard let cavnar = userInfo["cavnar"] as? [String: Any] else { return }
let alertType = cavnar["alert_type"] as? String ?? ""
let reviewId = cavnar["review_id"] as? Int
router?.handleNotificationTap(alertType: alertType, reviewId: reviewId)
```

`review_id` is taken from the payload and used to deep-link into a review. The payload is APNs-authenticated so the practical risk is low, but the client performs **no bounds or ownership check** — it relies entirely on the backend scoping `/mobile/api/reviews/<id>` to the caller's restaurant. That server-side scoping is the actual control; it should be asserted in a test rather than assumed.

**After** — validate shape client-side and keep the server check as the real gate:
```swift
let alertType = cavnar["alert_type"] as? String ?? ""
// Reject nonsense ids before they become a URL path component; the backend
// is still the authority on whether this review belongs to this account.
let reviewId = (cavnar["review_id"] as? Int).flatMap { $0 > 0 ? $0 : nil }
router?.handleNotificationTap(alertType: alertType, reviewId: reviewId)
```

---

## ✅ What this codebase already gets right

These were checked and came back clean — worth recording so they are not "fixed" into regressions later:

- **No hardcoded secrets.** A scan for `sk_live`/`sk_test`/`pk_live`/`re_`/`AIza`/`GOCSPX`/bearer literals across all 121 Swift files returned only `CodingKeys` false positives. Stripe, Resend and Google secrets live server-side only, which is correct.
- **Keychain used for the right things.** `Core/Keychain.swift` stores the session token, the 2FA "remember this device" token and the device identity, with `kSecAttrAccessibleAfterFirstUnlock`. No auth material in UserDefaults.
- **ATS not disabled.** Only `NSAllowsLocalNetworking` is set — arbitrary-loads is *not* enabled, so release traffic is TLS-enforced.
- **Almost no logging.** Exactly two `print()` calls in the entire target (`PushManager.swift:47,53`), and neither logs a token or credential. Worth wrapping in `#if DEBUG` for completeness, but this is not a leak today.
- **URL scheme has no handler.** `cavnarai://` is registered for `ASWebAuthenticationSession` callbacks only; there is no `onOpenURL`, so there is no deep-link injection surface and **no way to fake a payment status via URL** — the specific risk raised in the audit brief does not exist here.
