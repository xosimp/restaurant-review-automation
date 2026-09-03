# Audit Pass 2 — Bug & Error Audit

**Target:** `ios/CavnarAI/CavnarAI/`
**Method:** static analysis + a full `xcodebuild` compile with `SWIFT_STRICT_CONCURRENCY: complete`, using the compiler's own diagnostics as evidence rather than pattern-matching alone.

---

## Executive summary

Force-unwrapping — normally the top crash source in a Swift audit — is **nearly absent here**: 8 total across 25,590 lines, and 6 of those are provably safe. That is unusually disciplined.

The real stability risk is different and largely invisible to eyeballing: **13 compiler warnings that are hard errors in Swift 6**, including four non-`Sendable` date formatters shared as global statics and reachable from concurrent decode paths. Those are genuine data races today, not just future-compiler complaints.

| # | Severity | Finding |
|---|---|---|
| 2.1 | CRITICAL | Force-unwrapped custom font crashes Ask Cavnar if the font fails to load |
| 2.2 | CRITICAL | Non-`Sendable` static date formatters — real data race, 4 sites |
| 2.3 | WARNING | 13 warnings that block the Swift 6 language mode entirely |
| 2.4 | WARNING | `disable2FA` fails silently — user believes 2FA is off when it isn't |
| 2.5 | WARNING | Ask Cavnar loses the user's question on any network failure |
| 2.6 | WARNING | `PreferenceKey.defaultValue` as mutable global state |
| 2.7 | OPTIMIZATION | Force unwrap on env-var-derived URL in the Google sign-in path |
| 2.8 | OPTIMIZATION | Redundant force unwrap immediately after assignment |
| — | ✅ PASS | 8 force unwraps total; correct `@Observable` + `@State` usage throughout |

---

## 2.1 CRITICAL — Force-unwrapped custom font will crash Ask Cavnar

**File:** `Features/AskCavnar/AskCavnarView.swift` line 252

```swift
private static let textFont = UIFont(name: "ApfelGrotezk-Regular", size: 16)!
```

`UIFont(name:size:)` returns `nil` whenever the PostScript name does not resolve — a font file dropped from the bundle, a renamed `.ttf`, a mismatch between the filename in `UIAppFonts` and the internal PostScript name, or (observed in the wild) iOS failing to register a custom font on a device under memory pressure. Any of those turns opening Ask Cavnar into an immediate crash, because this is a `static let` evaluated on first access to `ChatBubble`.

This is the single highest-probability crash in the app: it is on the main user-facing AI surface, and this project has already had one font-naming incident (the Typography v2 migration, where PostScript names differed from filenames).

**Before:**
```swift
private static let textFont = UIFont(name: "ApfelGrotezk-Regular", size: 16)!
```

**After** — degrade to the system font instead of terminating:
```swift
/// Measurement font for cavnarMeasuredTextWidth. Falls back to the system
/// font at the same size rather than force-unwrapping: a missing/unregistered
/// custom font should cost slightly imprecise bubble widths, not a crash on
/// the app's main AI screen.
private static let textFont: UIFont =
    UIFont(name: "ApfelGrotezk-Regular", size: 16) ?? .systemFont(ofSize: 16)
```

Then add a launch-time assertion so the real problem surfaces in development instead of silently degrading in production:
```swift
// CavnarAIApp.swift, in init()
#if DEBUG
assert(UIFont(name: "ApfelGrotezk-Regular", size: 16) != nil,
       "ApfelGrotezk-Regular failed to register — check UIAppFonts in project.yml "
       + "and that the .ttf's internal PostScript name matches exactly.")
#endif
```

---

## 2.2 CRITICAL — Non-`Sendable` static date formatters shared across concurrency domains

**Files:**
- `Models/Review.swift` lines 53, 59 (and the `sourceDateFormatters` / `displayDateFormatter` statics above them)
- `Models/NotificationItem.swift` line 30

Compiler output (verbatim):
```
Models/Review.swift:53:24: warning: static property 'isoFormatterWithFractionalSeconds' is not
  concurrency-safe because non-'Sendable' type 'ISO8601DateFormatter' may have shared mutable state
Models/Review.swift:59:24: warning: static property 'isoFormatter' is not concurrency-safe ...
Models/NotificationItem.swift:30:24: warning: static property 'relativeFormatter' is not
  concurrency-safe because non-'Sendable' type 'RelativeDateTimeFormatter' may have shared mutable state
```

These are not theoretical. `formattedDate` (`Review.swift:66–73`) is a computed property on a `Decodable` model, called from SwiftUI view bodies **and** reachable from decode paths inside `APIClient` — which is an `actor`, i.e. a different isolation domain from `@MainActor`. `Foundation`'s formatters carry internal mutable caching state; concurrent `date(from:)` calls on the same instance from two domains is exactly the documented unsafe pattern, and it manifests as rare, unreproducible garbage dates or a crash inside CoreFoundation — the kind of bug that gets written off as "a weird one-off" for months.

**Before** (`Review.swift:53–59`):
```swift
private static let isoFormatterWithFractionalSeconds: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter
}()

private static let isoFormatter = ISO8601DateFormatter()
```

**After** — isolate the shared instances behind a single lock-free actor-free wrapper. The simplest correct fix that keeps the existing call sites intact is to make the formatters thread-local rather than global-shared:

```swift
/// ISO8601DateFormatter is not Sendable and carries internal mutable state,
/// so a single shared instance touched from both the APIClient actor's decode
/// path and MainActor view bodies is a real data race (compiler-confirmed
/// under SWIFT_STRICT_CONCURRENCY: complete). Thread-local instances keep the
/// "build it once, not per-call" performance intent without the sharing.
private static let isoFormatterWithFractionalSeconds = ThreadLocalFormatter {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter
}

private static let isoFormatter = ThreadLocalFormatter { ISO8601DateFormatter() }
```

```swift
// New: Core/ThreadLocalFormatter.swift
import Foundation

/// One formatter instance per thread. Formatters are expensive to construct
/// (hence the original statics) but unsafe to share across isolation domains
/// — this keeps both properties.
final class ThreadLocalFormatter<T: AnyObject>: @unchecked Sendable {
    private let key: String
    private let make: @Sendable () -> T

    init(_ make: @escaping @Sendable () -> T) {
        self.key = "cavnar.formatter.\(UUID().uuidString)"
        self.make = make
    }

    var value: T {
        if let existing = Thread.current.threadDictionary[key] as? T { return existing }
        let fresh = make()
        Thread.current.threadDictionary[key] = fresh
        return fresh
    }
}
```

Call sites change from `Self.isoFormatter.date(...)` to `Self.isoFormatter.value.date(...)`.

*Alternative, if you would rather not add a type:* mark each formatter `nonisolated(unsafe)` **only** after moving every call site onto `@MainActor` — but that is a larger refactor and does not actually remove the race if any decode path stays off the main actor.

---

## 2.3 WARNING — 13 warnings block the Swift 6 language mode

A full compile emits 13 distinct warnings, every one of them tagged *"this is an error in the Swift 6 language mode."* The project is on `SWIFT_VERSION: 5.0` with `SWIFT_STRICT_CONCURRENCY: complete`, so these are visible today and will become build failures the moment the language mode moves.

Breakdown:

| Count | Site | Warning |
|---|---|---|
| 5 | `CavnarSealMark.swift:15`, `ViewModifiers.swift:539`, `ValueChartCard.swift:208`, `IntelView.swift:393`, `LaborAnalyticsSection.swift:211` | `Animatable` conformance crosses into main-actor-isolated code |
| 4 | `Review.swift:53,59`, `NotificationItem.swift:30`, `ViewModifiers.swift:554` | non-concurrency-safe global statics (see 2.2, 2.6) |
| 4 | `PushManager.swift:59,66` | non-`Sendable` `UNNotification` / `UNUserNotificationCenter` crossing into `@MainActor` |

The `Animatable` cluster is the same root cause five times: `animatableData` is a protocol requirement that SwiftUI may touch off the main actor, but the conforming types are `@MainActor`-isolated by inheritance from `View`.

**Before** (`ValueChartCard.swift:208`, representative of all five):
```swift
private struct AnimatableNumberText: View, Animatable {
    var value: Double
    var animatableData: Double {
        get { value }
        set { value = newValue }
    }
    // ...
}
```

**After** — mark the requirement `nonisolated`, which is both correct (it only touches a stored `Double`) and silences the warning properly rather than suppressing it:
```swift
private struct AnimatableNumberText: View, Animatable {
    var value: Double
    /// nonisolated: SwiftUI drives animatableData from its own animation
    /// machinery, which is not guaranteed to be the main actor. The property
    /// only reads/writes a stored Double, so it is safe to leave unisolated —
    /// and doing so is required for the conformance to be valid in Swift 6.
    nonisolated var animatableData: Double {
        get { value }
        set { value = newValue }
    }
    // ...
}
```

For `PushManager` (lines 59, 66), the delegate methods are already `async` on a `@MainActor` class; the fix is to accept the non-`Sendable` parameters in a `nonisolated` method and hop explicitly:
```swift
nonisolated func userNotificationCenter(
    _ center: UNUserNotificationCenter,
    didReceive response: UNNotificationResponse
) async {
    // Extract only Sendable values before crossing to the main actor.
    let userInfo = response.notification.request.content.userInfo
    let cavnar = userInfo["cavnar"] as? [String: Any]
    let alertType = cavnar?["alert_type"] as? String ?? ""
    let reviewId = (cavnar?["review_id"] as? Int).flatMap { $0 > 0 ? $0 : nil }
    await MainActor.run {
        router?.handleNotificationTap(alertType: alertType, reviewId: reviewId)
    }
}
```

---

## 2.4 WARNING — Turning 2FA off can fail silently, leaving the user misinformed about their own security state

**Files:** `Features/Account/AccountViewModel.swift` lines 225–234; `Features/Account/AccountSecurityDetailView.swift` lines 155–166

```swift
// AccountViewModel.swift:225
func disable2FA() async -> Bool {
    do {
        _ = try await client.send("/mobile/api/account/2fa/disable", method: .post) as APIClient.EmptyResponse
        await load()
        return true
    } catch {
        // Left as-is — user can retry from the toggle.
        return false
    }
}
```

The caller only reacts to `true`:
```swift
AccountLink(title: "Turn off", tone: .cavnarRed) {
    Task {
        if await viewModel.disable2FA() {
            Haptic.success()
            disabledLabel = "Two-factor disabled"
        }
    }
}
```

On failure the user gets **no error, no haptic, no state change, nothing** — the row simply still says "Turn off". The comment assumes they will infer that and retry. In practice the far more likely reading is "I tapped it, it's off now." For a security control, a silent no-op that leaves the user with a **wrong belief about whether 2FA is protecting their account** is worse than a visible error.

The same silent-failure shape applies to `revokeOtherSessions()` (line 108–113) — "Sign out all other devices" can quietly do nothing.

**After:**
```swift
// AccountViewModel.swift
var securityActionError: String?

@discardableResult
func disable2FA() async -> Bool {
    securityActionError = nil
    do {
        _ = try await client.send("/mobile/api/account/2fa/disable", method: .post) as APIClient.EmptyResponse
        await load()
        return true
    } catch let error as APIClient.APIError {
        // A security control that silently no-ops leaves the user believing
        // 2FA is off when it is still on — always surface this.
        securityActionError = error.message
        return false
    } catch {
        securityActionError = "Couldn't turn two-factor off — check your connection and try again."
        return false
    }
}
```
```swift
// AccountSecurityDetailView.swift — in signInSection, above the rows
if let error = viewModel.securityActionError {
    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
}
```

---

## 2.5 WARNING — Ask Cavnar discards the user's question on any failure

**File:** `Features/AskCavnar/AskCavnarViewModel.swift` lines 58–75

```swift
let asked = question
let history = messages.map { ... }
messages.append(ChatMessage(text: asked, isUser: true))
question = ""          // ← cleared before the request is known to succeed
isLoading = true
defer { isLoading = false }
do {
    let response: AskResponse = try await client.send(...)
    // ...
} catch {
    messages.append(ChatMessage(text: "Something went wrong. Try again.", isUser: false))
}
```

`question` is cleared immediately. If the request fails — which in a restaurant basement is the *expected* case, not the edge case — the user's typed question is gone from the input field and they must retype it from scratch. "Try again" is advice the UI has actively made harder to follow.

There is a second-order bug here too: the failure message is appended as an **assistant** message, so it becomes part of `history` on the next turn and gets sent to Claude as if the assistant had said it.

**After** — restore the input and keep failures out of the conversation history:
```swift
func submit() async {
    guard canSubmit else { return }
    let asked = question
    let history = messages.map { HistoryTurn(role: $0.isUser ? "user" : "assistant", content: $0.text) }
    messages.append(ChatMessage(text: asked, isUser: true))
    question = ""
    isLoading = true
    defer { isLoading = false }
    do {
        let response: AskResponse = try await client.send(
            "/mobile/api/ask-cavnar", method: .post, body: AskBody(question: asked, history: history)
        )
        let text = response.ok ? (response.answer ?? "") : (response.error ?? "Something went wrong.")
        messages.append(ChatMessage(text: text, isUser: false))
    } catch {
        // Roll the turn back rather than leaving a dead-end: the question
        // returns to the input box ready to resend, and the failure never
        // enters `history` (it would otherwise be replayed to Claude as
        // something the assistant actually said).
        messages.removeLast()
        question = asked
        errorBanner = (error as? APIClient.APIError)?.message
            ?? "Couldn't reach Cavnar AI — check your connection and try again."
    }
}

/// Transient, shown above the input bar; not part of the conversation.
var errorBanner: String?
```

---

## 2.6 WARNING — `PreferenceKey.defaultValue` declared as mutable global state

**File:** `DesignSystem/ViewModifiers.swift` line 554

```
warning: static property 'defaultValue' is not concurrency-safe because it is
  nonisolated global shared mutable state
```

```swift
private struct CavnarWidthKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}
```

`static var` makes this writable global state that any thread can mutate. The value is a constant in practice, so the fix is trivial and removes a real (if unlikely) corruption vector.

**After:**
```swift
private struct CavnarWidthKey: PreferenceKey {
    // `let`, not `var` — this is a constant, and as a `static var` it was
    // nonisolated mutable global state (an error in Swift 6).
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}
```

---

## 2.7 OPTIMIZATION — Force unwrap on a URL derived from an environment variable

**File:** `Features/Auth/GoogleSignInCoordinator.swift` lines 21–24, 32

```swift
var components = URLComponents(
    url: baseURL.appendingPathComponent("auth/google-sso"),
    resolvingAgainstBaseURL: false
)!
// ...
url: components.url!,
```

`baseURL` comes from `AppEnvironment.baseURL`, which reads `CAVNAR_API_BASE_URL` from the process environment (`AppEnvironment.swift:10`). That is externally supplied, so these two force unwraps are the only ones in the app operating on **non-literal** input. A malformed override crashes the app at sign-in rather than showing an error.

**After:**
```swift
func signIn(baseURL: URL) async throws -> String {
    guard var components = URLComponents(
        url: baseURL.appendingPathComponent("auth/google-sso"),
        resolvingAgainstBaseURL: false
    ) else {
        throw GoogleSignInError.serverError("Sign-in isn't configured correctly on this build.")
    }
    components.queryItems = [
        URLQueryItem(name: "mobile", value: "1"),
        URLQueryItem(name: "device_id", value: Keychain.deviceIdentity()),
    ]
    guard let authorizeURL = components.url else {
        throw GoogleSignInError.serverError("Sign-in isn't configured correctly on this build.")
    }
    // ... use authorizeURL instead of components.url!
}
```

---

## 2.8 OPTIMIZATION — Redundant force unwrap immediately after assignment

**File:** `Features/Reviews/ReviewDetailViewModel.swift` lines 119–120

```swift
finalStatus = (response.autoPosted == true) ? "posted" : "approved"
currentStatus = finalStatus!
```

Provably safe (assigned on the line above), so this is a style issue rather than a crash risk — but it trains the eye to accept `!`, and a later edit that moves the assignment turns it into a real crash.

**After:**
```swift
let status = (response.autoPosted == true) ? "posted" : "approved"
finalStatus = status
currentStatus = status
```

---

## ✅ What this codebase already gets right

- **Force-unwrapping is essentially absent.** 8 occurrences in 25,590 lines. Six are on string literals or `Calendar` arithmetic that cannot realistically fail. Most codebases this size have hundreds.
- **Modern state management, used correctly.** Every view model is `@Observable` + `@State private var viewModel = ...`, which is the correct iOS 17 pattern — SwiftUI initialises it once per view identity. There is no `@ObservedObject`-recreated-every-render bug anywhere, and no `@StateObject`/`@ObservedObject` mixing.
- **Task cancellation is handled deliberately.** `APIClient.send` (lines 100–102) explicitly distinguishes `URLError.cancelled` from real failures and rethrows it silently — a genuinely subtle bug (spurious error haptics when navigating away mid-fetch) that was already found and fixed.
- **Debounced writes flush before commits.** `ReviewDetailViewModel.approve()` (lines 100–106) cancels the pending debounce and awaits `saveDraft()` first, so what gets approved matches what is on screen. That race is usually missed.
- **`defer` used correctly for loading flags** throughout the view models, so `isLoading` cannot get stuck on an early return or thrown error.
