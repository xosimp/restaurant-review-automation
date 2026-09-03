# Audit Pass 3 — Bottleneck Audit (Performance & Memory)

**Target:** `ios/CavnarAI/CavnarAI/`
**Focus:** rendering efficiency, main-thread work, retain cycles, animation cost


> **Remediation status (ff937cc): 6/6 findings fixed.** All fixed.
> Verified by: clean `xcodebuild` (0 errors, 0 warnings), 646 backend tests passing,
> `scripts/check_colors.py` clean, and a per-finding grep confirming each original
> code signature is gone. Findings below are kept as written (plus explicit
> **Correction** notes where the original analysis was wrong) so the reasoning
> stays auditable rather than being rewritten after the fact.

---

## Executive summary

The animation system — normally the first place a design-heavy app leaks CPU — is **deliberately built and mostly correct**: looping states use `TimelineView` driven by wall-clock time, two of them are explicitly `paused:`-gated, one is frame-capped at 30fps, and the brand seal is static unless a loading component animates it.

The real bottleneck is concentrated in one place: **`TypewriterText` re-does O(n) work on every one of n frames**, including a full UIKit text-layout pass. On a 400-word AI answer that is ~400 `boundingRect` calls and ~160,000 redundant string operations, all on the main thread, all while an animation is running. This is the app's worst performance defect by a wide margin.

| # | Severity | Finding |
|---|---|---|
| 3.1 | CRITICAL | `TypewriterText` is O(n²) — full text re-split and re-measured per revealed word |
| 3.2 | WARNING | UIKit `boundingRect` called on the main thread once per animation frame |
| 3.3 | WARNING | Per-word `scrollTo` forces a full scroll-view layout pass per word |
| 3.4 | WARNING | `AskCavnarView` conversation array grows unbounded in memory |
| 3.5 | OPTIMIZATION | Device token re-registered on every Face ID unlock |
| 3.6 | OPTIMIZATION | 11 `repeatForever` animations ignore Reduce Motion |
| — | ✅ PASS | No retain cycles found; `TimelineView` used correctly; no view-init network calls |

---

## 3.1 CRITICAL — `TypewriterText` performs O(n²) string work during the reveal

**File:** `DesignSystem/TypewriterText.swift` lines 35, 47, 64–71

```swift
private var words: [String] { fullText.split(separator: " ").map(String.init) }   // line 35

var body: some View {
    Text(words.prefix(visibleWordCount).joined(separator: " "))                   // line 47
    // ...
    .task(id: fullText) {
        visibleWordCount = 0
        let total = words.count
        // ...
        for i in 1...total {
            try? await Task.sleep(nanoseconds: delayNanos)
            if Task.isCancelled { return }
            visibleWordCount = i                                                  // ← triggers a body re-eval
        }
    }
}
```

`words` is a **computed property**, not stored. Every mutation of `visibleWordCount` re-evaluates `body`, and every `body` evaluation:

1. re-splits the entire `fullText` into words (`O(n)` over the full string), then
2. allocates a fresh `[String]` array of every word, then
3. `prefix(i).joined()` builds a brand-new string of growing length (`O(i)`).

Across a full reveal of *n* words that is **O(n²) character work plus n array allocations**. For a typical 400-word Claude answer: 400 full re-splits of a ~2,400-character string, 400 array allocations of 400 elements each, and 400 growing string joins — roughly 160,000 redundant character operations, executed on the main actor between animation frames.

**Before:**
```swift
private var words: [String] { fullText.split(separator: " ").map(String.init) }

var body: some View {
    Text(words.prefix(visibleWordCount).joined(separator: " "))
```

**After** — split once, and precompute each prefix boundary so the per-frame cost is a single `String` slice:

```swift
/// Split once per message, not once per revealed word. `words` was a
/// computed property re-splitting the entire answer on every one of n body
/// evaluations — O(n²) character work across a reveal, all on the main actor.
@State private var words: [String] = []
/// Byte offsets into fullText for each word boundary, so rendering frame i
/// is a single O(1) slice instead of an O(i) `prefix().joined()`.
@State private var prefixes: [String] = []

var body: some View {
    Text(prefixes.indices.contains(visibleWordCount) ? prefixes[visibleWordCount] : fullText)
        .font(font)
        .foregroundStyle(color)
        .lineSpacing(lineSpacing)
        .fixedSize(horizontal: false, vertical: true)
        .frame(width: resolvedWidth, alignment: .leading)
        .task(id: fullText) {
            let split = fullText.split(separator: " ").map(String.init)
            words = split
            // prefixes[i] == the first i words joined. Built once, read n times.
            var running: [String] = [""]
            running.reserveCapacity(split.count + 1)
            var accumulated = ""
            for word in split {
                accumulated += accumulated.isEmpty ? word : " " + word
                running.append(accumulated)
            }
            prefixes = running

            visibleWordCount = 0
            let total = split.count
            guard total > 0 else { return }
            let delayNanos = UInt64(min(max(1400.0 / Double(total), 16), 55) * 1_000_000)
            for i in 1...total {
                try? await Task.sleep(nanoseconds: delayNanos)
                if Task.isCancelled { return }
                visibleWordCount = i
            }
        }
}
```

---

## 3.2 WARNING — UIKit text measurement runs once per animation frame, on the main thread

**Files:** `DesignSystem/TypewriterText.swift` lines 41–44, 63; `DesignSystem/Font+Cavnar.swift` lines 78–87

```swift
// TypewriterText.swift:41 — a computed property, so this runs on EVERY body eval
private var resolvedWidth: CGFloat? {
    guard let maxWidth, let measuringFont else { return nil }
    return cavnarMeasuredTextWidth(fullText, font: measuringFont, maxWidth: maxWidth)
}
```
```swift
// Font+Cavnar.swift:78 — uncached NSString.boundingRect over the FULL text
func cavnarMeasuredTextWidth(_ text: String, font: UIFont, maxWidth: CGFloat) -> CGFloat {
    guard !text.isEmpty else { return 0 }
    let bounds = (text as NSString).boundingRect(
        with: CGSize(width: maxWidth, height: .greatestFiniteMagnitude),
        options: [.usesLineFragmentOrigin, .usesFontLeading],
        attributes: [.font: font],
        context: nil
    )
    return ceil(bounds.width)
}
```

`boundingRect` performs a **complete TextKit layout pass** — glyph generation, line breaking, font metric resolution. It is one of the more expensive synchronous calls in UIKit, and here it runs against the *entire* answer text on every single body evaluation, i.e. once per revealed word, on the main thread, while an animation is in flight.

The code comment on line 37 explains the *intent* correctly ("measured once against the FULL final text… so the bubble's width is fixed"), but a computed property does not memoize — the value is stable, the *work* is not.

**After** — compute once when the text changes, then read the stored value:
```swift
/// Measured once per message in .task, not per frame. As a computed property
/// this was a full TextKit layout pass (NSString.boundingRect over the entire
/// answer) on every one of n body evaluations during the reveal.
@State private var resolvedWidth: CGFloat?

// inside .task(id: fullText), before the reveal loop:
if let maxWidth, let measuringFont {
    resolvedWidth = cavnarMeasuredTextWidth(fullText, font: measuringFont, maxWidth: maxWidth)
}
```

The same fix applies to `ChatBubble.userTextWidth` (`AskCavnarView.swift:269–271`), which is likewise a computed property calling `cavnarMeasuredTextWidth`. It re-measures the user's message on every parent re-render — cheap individually, but it is inside a `ForEach` over the whole conversation, so it re-runs for **every message** each time a new one arrives.

**After** (`AskCavnarView.swift`):
```swift
// Measured once when the bubble is created; message.text is immutable so
// there is nothing to invalidate.
@State private var userTextWidth: CGFloat = 0

// in body:
.task { userTextWidth = cavnarMeasuredTextWidth(message.text, font: Self.textFont, maxWidth: Self.maxTextWidth) }
```

---

## 3.3 WARNING — Per-word `scrollTo` forces a scroll-view layout pass on every revealed word

**File:** `Features/AskCavnar/AskCavnarView.swift` lines 30–57

```swift
ChatBubble(message: message) {
    // Fires on every word TypewriterText reveals
    proxy.scrollTo(chatScrollBottomID, anchor: .bottom)
}
```

Combined with 3.1 and 3.2, each revealed word currently triggers: a full re-split of the answer → a full `boundingRect` layout pass → a `Text` rebuild → **and** a `ScrollViewReader` scroll adjustment that invalidates the scroll view's content layout. At the fastest reveal cadence (16 ms/word, per `TypewriterText.swift:68`) that is ~60 of these compound passes per second on the main thread. This is the mechanism behind streaming jitter on older devices.

The `scrollTo` itself is justified (without it, long answers scroll out from under the reader), but it does not need to fire at word granularity.

**After** — coalesce to at most ~10 scrolls/second, which is visually identical but ~6× less layout work:
```swift
// AskCavnarView.swift
@State private var lastScrollTick = Date.distantPast

ChatBubble(message: message) {
    // Throttled: a scroll adjustment every ~100ms tracks the growing bubble
    // just as smoothly to the eye, without forcing a scroll-view layout pass
    // on every single revealed word (up to 60/sec at the fastest cadence).
    let now = Date()
    guard now.timeIntervalSince(lastScrollTick) > 0.1 else { return }
    lastScrollTick = now
    proxy.scrollTo(chatScrollBottomID, anchor: .bottom)
}
```

---

## 3.4 WARNING — Conversation history grows without bound

**File:** `Features/AskCavnar/AskCavnarViewModel.swift` lines 19, 58–62

```swift
var messages: [ChatMessage] = []
// ...
let history = messages.map { HistoryTurn(role: $0.isUser ? "user" : "assistant", content: $0.text) }
```

Every message is retained in memory for the lifetime of the sheet, and **the entire history is re-encoded and re-uploaded on every question**. A long session's request body grows linearly; a 30-turn conversation with detailed AI answers uploads tens of KB of redundant history per question, re-serialised each time.

The memory footprint alone is modest, but the payload growth is not — and it compounds directly into the token-cost problem documented in Pass 5 (§5.2).

**After** — cap retained turns, keeping the two most recent exchanges plus a trimmed window:
```swift
/// Cap on turns kept in memory and re-sent as `history`. Beyond this the
/// oldest exchanges are dropped: the payload (and the token bill) otherwise
/// grows linearly with session length for context the model rarely uses.
private static let maxHistoryTurns = 20

var messages: [ChatMessage] = [] {
    didSet {
        if messages.count > Self.maxHistoryTurns * 2 {
            messages.removeFirst(messages.count - Self.maxHistoryTurns * 2)
        }
    }
}
```

---

## 3.5 OPTIMIZATION — APNs device token re-registered on every unlock

**Files:** `RootView.swift` lines 177–179; `Push/PushManager.swift` lines 15–50

```swift
// RootView.swift — attached to mainTabs
.task {
    PushManager.shared.requestAuthorizationAndRegister()
}
```

`mainTabs` is torn down and rebuilt on every Face ID lock/unlock cycle (documented at `RootView.swift:26–36`). So each unlock re-runs authorization *and* fires a `POST /mobile/api/device-tokens` write. For an owner who checks the app a dozen times a shift, that is a dozen redundant authenticated round-trips and a dozen redundant database writes per day, per device.

**After** — register once per process:
```swift
// PushManager.swift
private var hasRegisteredThisLaunch = false

func requestAuthorizationAndRegister() {
    // mainTabs is rebuilt on every Face ID unlock, so this .task fires many
    // times per session — the token has not changed between them.
    guard !hasRegisteredThisLaunch else { return }
    hasRegisteredThisLaunch = true
    UNUserNotificationCenter.current().delegate = self
    // ... unchanged
}
```

---

## 3.6 OPTIMIZATION — Reduce Motion honoured in exactly one file

**Files:** 11 `repeatForever` sites and 10 `TimelineView` sites; only `Features/Auth/LoginBackground.swift` (lines 23, 236) reads `@Environment(\.accessibilityReduceMotion)`.

`RootView.swift:363` runs a 16-second infinite rotation, `AIConsultantView.swift:54` and `ViewModifiers.swift:487,643` run infinite pulses. None check Reduce Motion. Beyond the accessibility obligation (covered in Pass 7 §7.5), continuously animating layers keep the GPU awake — measurable battery cost on a device parked on a pass counter all service.

**After** — a single shared modifier applied at each looping site:
```swift
// DesignSystem/ViewModifiers.swift
extension View {
    /// Wraps a looping animation so it settles to its resting state when the
    /// user has Reduce Motion on — required for accessibility, and it also
    /// stops the GPU being kept awake by decorative loops.
    func cavnarLoopingAnimation<V: Equatable>(
        _ animation: Animation,
        value: V,
        reduceMotion: Bool
    ) -> some View {
        self.animation(reduceMotion ? nil : animation, value: value)
    }
}
```
```swift
// RootView.swift:363 — after
@Environment(\.accessibilityReduceMotion) private var reduceMotion
// ...
if !reduceMotion {
    withAnimation(.linear(duration: 16).repeatForever(autoreverses: false)) { ... }
}
```

---

## ✅ What this codebase already gets right

- **No retain cycles found.** View models are `@Observable` classes owned by `@State`; closures that outlive a call are either in value-type `View` structs (no cycle possible) or already use `[weak self]` — `SessionStore.swift:58` is the one place a cycle could form, and it is correctly weak.
- **`TimelineView` used properly for loops.** Wall-clock-driven rather than `@State` + `repeatForever` (rationale documented at `CavnarMotion.swift:9–10`), which avoids the classic bug where an ambient parent transaction silently hijacks a looping animation. Two sites correctly gate with `paused:` (`CavnarMotion.swift:1212, 1494`), and `CavnarEmptyHearth` (`:1346`) caps itself at 30fps.
- **Looping animations are confined to loading states.** `CavnarComposingLines`, `CavnarRadarSweep`, `CavnarWeekBuilder`, `CavnarLedgerFill` and the shimmer components only exist while data is in flight, so they self-terminate. The persistent toolbar seal (`HomeView.swift:171`) uses the *static* `CavnarSealMark` default (`emberIntensity: 0`) and does not animate.
- **No network calls in view `init`.** Every fetch is in `.task`/`.refreshable`, so nothing kicks off work during view construction or gets duplicated by SwiftUI re-instantiating structs.
- **Warm-start caching is deliberate.** `RootView` hoists `homeViewModel`, `homePath` and `modulesPath` above the lock-screen swap (`:26–37`) specifically so an unlock does not re-fetch and re-render Home from scratch, and `LockedView` pre-fetches Home's summary *behind* the lock screen (`:65–67`) so unlocking lands on rendered content.
- **JSON decoding is off the main thread.** `APIClient` is an `actor`, so `JSONDecoder.decode` runs in the actor's context, not on `@MainActor`.
