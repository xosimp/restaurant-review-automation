# Audit Pass 4 — Stack-Specific Features & Logic Gaps

**Target:** iOS client against the Flask/Railway backend, Stripe billing, Resend email
**Focus:** payment-flow completeness, network-state UI coverage, local↔backend synchronisation


> **Remediation status (ff937cc): 5/5 findings fixed.** All fixed.
> Verified by: clean `xcodebuild` (0 errors, 0 warnings), 646 backend tests passing,
> `scripts/check_colors.py` clean, and a per-finding grep confirming each original
> code signature is gone. Findings below are kept as written (plus explicit
> **Correction** notes where the original analysis was wrong) so the reasoning
> stays auditable rather than being rewritten after the fact.

---

## Executive summary

An important scoping correction up front: **this app contains no in-app payment flow.** There is no Stripe SDK, no `PaymentSheet`, no card entry, no checkout. `AccountBillingDetailView` is a read-only summary plus an outbound `Link` to a Stripe-hosted customer portal. The audit brief's "app crashes mid-Stripe-checkout" and "token generated but never reaches the backend" scenarios therefore **do not exist as risks here** — which is itself the correct architecture, since it keeps the app entirely out of PCI scope.

What does exist is a **round-trip gap**: the app hands the user off to Safari for billing changes and then never learns what happened. Combined with the fact that only one screen in the app refreshes when it returns to the foreground, the dominant failure mode across this codebase is *silently stale data*, not lost transactions.

| # | Severity | Finding |
|---|---|---|
| 4.1 | CRITICAL | Billing never refreshes after the Stripe portal round-trip |
| 4.2 | WARNING | Only 1 of 8 screens refreshes on foreground return — stale data everywhere else |
| 4.3 | WARNING | Failed push-token registration is never retried |
| 4.4 | WARNING | An abandoned OAuth sheet leaves the connect flow with no timeout |
| 4.5 | OPTIMIZATION | No "you are offline" distinction — every failure reads as a server fault |
| — | ✅ PASS | No optimistic writes; no PCI exposure; loading + empty + error states well covered |

---

## 4.1 CRITICAL — Billing state is never refreshed after the Stripe portal round-trip

**Files:** `Features/Account/AccountBillingDetailView.swift` lines 32–42 (whole file has **no** `.task`, `.onAppear`, `.refreshable` or `.onChange`); `Features/Account/AccountViewModel.swift` lines 93–104 (`loadBilling`)

The flow today:

1. Owner opens Billing → sees data fetched once, whenever `AccountView.task` last ran.
2. Taps "Manage payment method" → `Link` hands off to Safari → Stripe portal.
3. Owner updates their card / fixes a failed payment / cancels.
4. Returns to the app → **the Billing sheet still shows the pre-change state**, indefinitely.

There is no `.onChange(of: scenePhase)`, no refresh on re-appear, and `loadBilling()` is only called from `AccountView`'s own `.task`. The owner who just fixed a declined card comes back to a screen still saying "Payment past due." The most likely next action is that they pay again, or contact support about a bug that isn't one.

**Before** (`AccountBillingDetailView.swift`, body's modifier chain):
```swift
.accountSheetChrome("Billing")
```

**After** — refresh on every foreground return and on appear, since a change may have happened entirely outside the app:
```swift
@Environment(\.scenePhase) private var scenePhase

// ...
.accountSheetChrome("Billing")
// Billing is the one screen whose source of truth can change while the app
// is backgrounded — the owner leaves for Stripe's portal, updates a card,
// and comes back. Without these the sheet keeps showing pre-handoff state
// forever, which reads as "my payment didn't go through."
.task { await viewModel.loadBilling() }
.onChange(of: scenePhase) { _, phase in
    if phase == .active { Task { await viewModel.loadBilling() } }
}
.refreshable { await viewModel.loadBilling() }
```

Note the view currently takes `billing` as an immutable `let` snapshot (`AccountBillingDetailView.swift:8`), so it must also read live state for the refresh to be visible:
```swift
// Before
let billing: BillingSummary?

// After — snapshot as fallback, live value preferred, same pattern
// AccountSecurityDetailView already uses for `live`.
let viewModel: AccountViewModel
let billing: BillingSummary?
private var live: BillingSummary? { viewModel.billing ?? billing }
```

---

## 4.2 WARNING — Only one screen refreshes when the app returns to the foreground

**Files:** `Features/Labor/LaborView.swift` lines 12, 178 (the only screen that does this); `RootView.swift` lines 130–134 (scenePhase used solely for locking)

`LaborView` correctly re-fetches on `scenePhase == .active`. Home, Reviews, Intel, Marketing, Food Cost, Account and Billing do not. An owner who opens the app at 9am, backgrounds it, and reopens at 5pm sees **8-hour-old review counts and labor figures** with no indication they are stale — and because `RootView` deliberately hoists `homeViewModel` above the lock swap (`:32–37`) to avoid a reload flash, even a Face ID unlock will not refresh it.

That hoisting decision is right for perceived performance; it just needs a freshness policy alongside it.

**After** — add a shared age-aware refresh so warm-start speed is kept but stale data is not shown indefinitely:
```swift
// New: Core/FreshnessPolicy.swift
import SwiftUI

/// Re-fetches when the app returns to the foreground, but only if the data
/// on screen is older than `maxAge`. Keeps RootView's deliberate warm-start
/// caching (no reload flash on unlock) while preventing a screen from
/// silently showing hours-old numbers.
struct RefreshOnForeground: ViewModifier {
    let maxAge: TimeInterval
    let lastLoaded: Date?
    let reload: () async -> Void
    @Environment(\.scenePhase) private var scenePhase

    func body(content: Content) -> some View {
        content.onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            guard let lastLoaded, Date().timeIntervalSince(lastLoaded) > maxAge else { return }
            Task { await reload() }
        }
    }
}

extension View {
    func refreshOnForeground(olderThan maxAge: TimeInterval = 300, lastLoaded: Date?, reload: @escaping () async -> Void) -> some View {
        modifier(RefreshOnForeground(maxAge: maxAge, lastLoaded: lastLoaded, reload: reload))
    }
}
```
```swift
// HomeView.swift — after (same one-liner for Reviews/Intel/Marketing/FoodCost)
.refreshOnForeground(olderThan: 300, lastLoaded: viewModel.lastLoadedAt) {
    await viewModel.load()
}
```
with each view model recording `private(set) var lastLoadedAt: Date?` at the end of a successful `load()`.

---

## 4.3 WARNING — A failed push-token registration is never retried

**File:** `Push/PushManager.swift` lines 40–49

```swift
Task {
    do {
        let _: APIClient.EmptyResponse = try await APIClient.shared.send(
            "/mobile/api/device-tokens", method: .post,
            body: Body(apnsToken: tokenString, environment: environment)
        )
    } catch {
        print("[push] device token registration failed: \(error)")
    }
}
```

APNs hands the token to the app at launch, which is precisely when the network is least reliable and — on a cold launch behind the Face ID gate — when the user may not be authenticated yet, producing a 401. The failure is logged to a `print` nobody reads and **never retried**. The owner then simply never receives 1-star-review alerts, with no symptom anywhere in the UI to explain why.

Because §3.5 recommends registering only once per launch, the retry becomes essential rather than incidental.

**After** — persist the token and retry on the next authenticated opportunity:
```swift
private var pendingToken: (token: String, environment: String)?

func didRegister(deviceToken: Data) {
    let tokenString = deviceToken.map { String(format: "%02x", $0) }.joined()
    #if DEBUG
    let environment = "sandbox"
    #else
    let environment = "production"
    #endif
    pendingToken = (tokenString, environment)
    Task { await flushPendingToken() }
}

/// Retried after login and on foreground: the token arrives at launch, which
/// is exactly when the request is most likely to fail (no session yet, or no
/// network). A silently dropped registration means the owner never receives
/// an urgent-review alert again, with nothing in the UI to explain it.
func flushPendingToken() async {
    guard let pending = pendingToken else { return }
    struct Body: Encodable {
        let apnsToken: String
        let environment: String
        enum CodingKeys: String, CodingKey {
            case apnsToken = "apns_token"
            case environment
        }
    }
    do {
        let _: APIClient.EmptyResponse = try await APIClient.shared.send(
            "/mobile/api/device-tokens", method: .post,
            body: Body(apnsToken: pending.token, environment: pending.environment)
        )
        pendingToken = nil
    } catch {
        // Keep it queued for the next attempt.
    }
}
```
```swift
// SessionStore.completeLogin() — after a session exists, flush any queued token
private func completeLogin(token: String, user: User) async throws {
    Keychain.set(token, for: Keychain.Key.sessionToken)
    await client.setToken(token)
    self.token = token
    self.currentUser = user
    self.isLocked = false
    await PushManager.shared.flushPendingToken()
}
```

---

## 4.4 WARNING — An abandoned OAuth sheet has no timeout

**Files:** `Features/Account/GMBConnectCoordinator.swift` lines 23–56; `Features/Auth/GoogleSignInCoordinator.swift` lines 30–65

Both coordinators suspend on `withCheckedThrowingContinuation` until `ASWebAuthenticationSession` calls back. `ASWebAuthenticationSession` reliably reports user cancellation, so the common path is covered — but if the browser sheet is dismissed by an OS-level interruption (a call, a memory-pressure kill of the auth service, a backgrounded app whose sheet is torn down), the completion may never fire and the continuation **never resumes**. The awaiting task then hangs for the lifetime of the process, leaving `isConnectingGoogle == true` and the spinner running forever with no way to retry short of force-quitting.

**After** — race the continuation against a wall-clock timeout:
```swift
func connect(authorizeURL: URL) async throws {
    try await withThrowingTaskGroup(of: Void.self) { group in
        group.addTask { try await self.runSession(authorizeURL: authorizeURL) }
        group.addTask {
            // An OAuth consent screen that has not resolved in 3 minutes is
            // not going to: the sheet was torn down by the OS and the
            // completion handler will never fire, leaving the caller's
            // spinner spinning for the life of the process.
            try await Task.sleep(nanoseconds: 180 * 1_000_000_000)
            throw GMBConnectError.server("Connection timed out — try again.")
        }
        // First to finish wins; cancel the loser.
        try await group.next()
        group.cancelAll()
    }
}

/// The existing body, extracted unchanged.
private func runSession(authorizeURL: URL) async throws { /* ...as before... */ }
```

---

## 4.5 OPTIMIZATION — No distinction between "you are offline" and "the server failed"

**File:** `Core/APIClient.swift` lines 103–105

```swift
if hapticOnError { await Haptic.error() }
throw APIError(message: "Couldn't reach the server — check your connection and try again.")
```

Every transport-layer failure collapses into one string. A DNS failure, an airplane-mode device, a dead Wi-Fi captive portal in the dining room, and a genuinely down backend all produce identical copy. The user cannot tell whether to move nearer the router or call support — and the app cannot decide whether retrying is even worth attempting. This is the client-side half of the offline problem covered in Pass 6.

**After** — classify the `URLError` and let callers react:
```swift
struct APIError: Error, LocalizedError, Equatable {
    enum Kind: Equatable { case offline, timedOut, server, decoding }
    let kind: Kind
    let message: String
    var errorDescription: String? { message }
}

// in the catch block around session.data(for:)
if let urlError = error as? URLError {
    switch urlError.code {
    case .notConnectedToInternet, .dataNotAllowed:
        throw APIError(kind: .offline,
                       message: "You're offline — this'll update as soon as you're back on Wi-Fi or cell.")
    case .timedOut, .networkConnectionLost:
        throw APIError(kind: .timedOut,
                       message: "The connection dropped mid-request. Tap to retry.")
    default:
        break
    }
}
```

---

## ✅ What this codebase already gets right

- **No PCI exposure.** No card data, no Stripe SDK, no payment tokens ever touch the device. Billing is display-only plus a hosted-portal handoff — the correct architecture for a small SaaS, and it removes an entire class of the risks this pass was asked to look for.
- **No optimistic writes.** Both local mutations that could have been optimistic — `toggleLoginNotify` (`AccountViewModel.swift:252`) and `toggleMarketingOptOut` (`:272`) — apply **after** the server confirms, and fall back to `await load()` on failure to resync. The `.cavnarPostedOverlay` success confirmation is likewise documented as firing only on a real success, never optimistically.
- **Loading, empty and error states are genuinely well covered.** 42 of the feature files handle `isEmpty` explicitly, with 76 distinct empty-state branches, and every major screen has a "Retry" affordance (Home, Intel, Marketing, Account, Modules, Changelog, Schedule History, Notifications).
- **Debounced edits flush before commit.** `ReviewDetailViewModel.approve()` (`:100–106`) cancels and awaits the pending draft save first, so an approve can never post a stale draft — a real sync gap that was already anticipated.
- **2FA setup survives an app relock mid-flow.** `SessionStore.pendingTwoFactorSetupEmail`/`pendingTwoFactorSetupMethod` (`:28–45`) deliberately persist across the `LockedView` swap that destroys the sheet, so backgrounding to read the emailed code does not lose the flow. That is a genuinely subtle state-continuity gap, already closed.
