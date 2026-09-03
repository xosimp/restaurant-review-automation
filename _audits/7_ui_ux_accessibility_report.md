# Audit Pass 7 — UI/UX, Typography & Accessibility Audit

**Target:** `ios/CavnarAI/CavnarAI/` — 121 Swift files
**Standard:** Apple Human Interface Guidelines; WCAG 2.1 AA where applicable
**Deployment context:** restaurant floor, kitchen pass, dim dining rooms, wet/greasy hands

---

## Executive summary

The visual craft here is high — a coherent design system, a considered three-typeface hierarchy, deliberate motion language. But it is built almost entirely for one user: **someone with good eyesight, using default text size, who is not using VoiceOver.**

Two findings dominate everything else, and both have unusually cheap fixes relative to their impact:

1. **Dynamic Type is completely unsupported.** All three font helpers use `.custom(_:size:)` without `relativeTo:`, so text does not scale *at all* — an owner in their 50s who has set Large Text system-wide sees the identical 14pt labels as everyone else. Because every one of the ~400 call sites routes through three helper functions, this is a **three-line fix**.
2. **VoiceOver is effectively unsupported.** Two `accessibility*` references exist in 25,590 lines, and both are `reduceMotion` checks in one file. Five data-visualisation charts, every icon-only button, and every status pill are unlabelled.

| # | Severity | Finding |
|---|---|---|
| 7.1 | CRITICAL | Zero Dynamic Type support across the entire app |
| 7.2 | CRITICAL | Fixed row heights will clip text the moment Dynamic Type is enabled |
| 7.3 | CRITICAL | 22×22pt destructive tap target on the primary on-the-floor entry screen |
| 7.4 | WARNING | Five charts with no VoiceOver representation at all |
| 7.5 | WARNING | Icon-only controls unlabelled for VoiceOver |
| 7.6 | WARNING | Reduce Motion honoured in 1 file out of 21 animating ones |
| 7.7 | WARNING | Dark-only UI with no high-contrast adaptation for bright kitchens |
| 7.8 | OPTIMIZATION | Cumulative layout shift as async content replaces skeletons |
| — | ✅ PASS | Strong colour-token system; disciplined 44pt row heights in list UI |

---

## 7.1 CRITICAL — Dynamic Type is not supported anywhere

**File:** `DesignSystem/Font+Cavnar.swift` lines 15–17, 36–38, 42–44

All typography in the app flows through exactly three functions:

```swift
static func cavnarHeadline(_ size: CGFloat, weight: ClashWeight = .semibold) -> Font {
    .custom(weight.postScriptName, size: size)
}

static func cavnarBody(_ size: CGFloat, weight: CGFloat = 400) -> Font {
    .custom(weight >= 550 ? "ApfelGrotezk-Fett" : "ApfelGrotezk-Regular", size: size)
}

static func cavnarNumber(_ size: CGFloat, weight: CGFloat = 500) -> Font {
    Font(cavnarUIFont(family: "Space Grotesk", weight: weight, size: size))
}
```

`Font.custom(_:size:)` — the two-argument form — produces a **fixed-size font that ignores the user's text-size setting entirely**. The three-argument form `Font.custom(_:size:relativeTo:)` is the one that scales. `cavnarNumber` is worse still: it wraps a raw `UIFont`, which bypasses SwiftUI's text-scaling pipeline completely.

The practical result: an owner who has set Accessibility → Larger Text to its maximum sees **exactly the same 13.5pt secondary labels** as a user on the default. There is no way for them to make this app readable. Given the target user — restaurant owners, frequently 40+, reading small financial figures in dim light — this is the most impactful accessibility defect in the codebase.

The fix is genuinely three lines, and it propagates to every call site automatically:

**Before:**
```swift
static func cavnarBody(_ size: CGFloat, weight: CGFloat = 400) -> Font {
    .custom(weight >= 550 ? "ApfelGrotezk-Fett" : "ApfelGrotezk-Regular", size: size)
}
```

**After:**
```swift
/// `relativeTo:` is what makes a custom font participate in Dynamic Type.
/// Without it, .custom(_:size:) is frozen at `size` regardless of the user's
/// text-size setting — the app was previously unreadable-by-design for anyone
/// using Larger Text. The `relativeTo` bucket is chosen per call so scaling
/// stays proportional to the role the size implies.
static func cavnarBody(_ size: CGFloat, weight: CGFloat = 400) -> Font {
    .custom(
        weight >= 550 ? "ApfelGrotezk-Fett" : "ApfelGrotezk-Regular",
        size: size,
        relativeTo: Self.textStyle(for: size)
    )
}

static func cavnarHeadline(_ size: CGFloat, weight: ClashWeight = .semibold) -> Font {
    .custom(weight.postScriptName, size: size, relativeTo: size >= 24 ? .largeTitle : .title2)
}

static func cavnarNumber(_ size: CGFloat, weight: CGFloat = 500) -> Font {
    // UIFontMetrics is the UIKit-side equivalent of relativeTo: — required
    // here because this path builds a raw UIFont for the variable-weight axis.
    let base = cavnarUIFont(family: "Space Grotesk", weight: weight, size: size)
    let metrics = UIFontMetrics(forTextStyle: Self.uiTextStyle(for: size))
    return Font(metrics.scaledFont(for: base))
}

/// Maps a literal point size onto the closest system text style, so scaling
/// behaves proportionally (captions grow faster than titles, as iOS intends).
private static func textStyle(for size: CGFloat) -> Font.TextStyle {
    switch size {
    case ..<13:   return .caption
    case ..<15:   return .footnote
    case ..<17:   return .subheadline
    case ..<20:   return .body
    case ..<24:   return .title3
    default:      return .title2
    }
}

private static func uiTextStyle(for size: CGFloat) -> UIFont.TextStyle {
    switch size {
    case ..<13:   return .caption1
    case ..<15:   return .footnote
    case ..<17:   return .subheadline
    case ..<20:   return .body
    case ..<24:   return .title3
    default:      return .title2
    }
}
```

Then cap the extremes so the densest screens stay usable, rather than disabling scaling outright:
```swift
// RootView.swift — alongside .preferredColorScheme(.dark)
.dynamicTypeSize(...DynamicTypeSize.accessibility2)
```

---

## 7.2 CRITICAL — Fixed row heights will clip text as soon as Dynamic Type works

**Files:** `Features/Account/AccountSheetKit.swift` lines 242, 255–256; `Features/Account/AccountView.swift` lines 356–357; `DesignSystem/CavnarSegmentedControl.swift` lines 28, 71; `DesignSystem/CavnarInteractions.swift` lines 214, 250

```swift
// AccountSheetKit.swift:242,255
static var rowHeight: CGFloat { 38 }
// ...
.frame(height: Self.rowHeight)
.padding(.vertical, 13)
```
```swift
// AccountView.swift:356
.padding(.horizontal, 16)
.frame(height: 54)
```

`.frame(height:)` is a **hard constraint**, not a floor. A row containing a 16pt label plus a trailing control is pinned to 38pt regardless of what the text inside actually needs. At default text size this is fine — the value was chosen deliberately to fix a row-alignment bug. But the moment 7.1 is fixed and text begins to scale, **every one of these rows clips**: labels truncate mid-word or get vertically cropped.

This is why 7.1 and 7.2 must ship together. Fixing Dynamic Type alone would visibly break the Account, Security and Profile sheets.

**Before:**
```swift
.frame(height: Self.rowHeight)
.padding(.vertical, 13)
```

**After** — a floor that still guarantees consistent alignment at default sizes, but grows rather than clips:
```swift
// A floor, not a fixed height: the exact-height version kept rows aligned at
// the default text size but clips outright once Dynamic Type scaling is on
// (see Font+Cavnar's relativeTo: change). minHeight preserves the alignment
// intent while letting a scaled label grow the row instead of truncating.
.frame(minHeight: Self.rowHeight)
.padding(.vertical, 13)
```
```swift
// AccountView.swift:357 — same change
.frame(minHeight: 54)
```
```swift
// CavnarSegmentedControl.swift:28,71
.frame(minHeight: 34)
```

Where an exact height is genuinely required for a shape (the 56pt buttons in `CavnarInteractions.swift:214,250`), scale the constant instead of freezing it:
```swift
@ScaledMetric(relativeTo: .body) private var buttonHeight: CGFloat = 56
// ...
.frame(height: buttonHeight)
```

---

## 7.3 CRITICAL — 22×22pt destructive button on the on-the-floor entry screen

**File:** `Features/FoodCost/FoodCostQuickEntryView.swift` lines 605–611

```swift
Button(action: onDelete) {
    Image(systemName: "xmark")
        .font(.system(size: 9, weight: .bold))
        .foregroundStyle(Color.cavnarInk.opacity(0.6))
        .frame(width: 22, height: 22)
        .background(Color.black.opacity(0.3), in: Circle())
}
```

22×22pt is **exactly half** the HIG minimum of 44×44. The glyph itself is 9pt. And this is a **delete** action, sitting on the Food Cost quick-entry carousel — the screen explicitly built for rapid data entry during service, by someone whose hands are wet, greasy, or gloved.

Two failure modes, both bad: the manager cannot hit it and gets frustrated, or they hit it *by accident* while aiming for an adjacent control and silently lose an entry. There is no undo.

**After** — keep the 22pt visual, expand the hit region to 44pt:
```swift
Button(action: onDelete) {
    Image(systemName: "xmark")
        .font(.system(size: 9, weight: .bold))
        .foregroundStyle(Color.cavnarInk.opacity(0.6))
        .frame(width: 22, height: 22)
        .background(Color.black.opacity(0.3), in: Circle())
        // The visual stays 22pt (the carousel's density depends on it), but
        // the tap target meets the 44pt HIG minimum. This is a DELETE on the
        // screen used mid-service with wet hands — an accidental miss either
        // frustrates or destroys an entry with no undo.
        .frame(width: 44, height: 44)
        .contentShape(Rectangle())
}
.accessibilityLabel("Delete entry")
```

Other sub-44pt interactive controls to correct the same way:

| File:line | Size | Control |
|---|---|---|
| `DesignSystem/CavnarSplitButton.swift:98` | 27×27 | split-button secondary action |
| `DesignSystem/AIConsultantView.swift:217` | 30×30 | consultant action affordance |
| `Features/Labor/AvailabilityManagerSection.swift:203` | 38×30 | availability toggle |
| `Features/AskCavnar/AskCavnarView.swift:210` | 38×38 | **send** button — the primary action of the AI screen |

`DesignSystem/GlassCard.swift:170` (26×26 `GlassChevronButton`) is **not** a defect — its own doc comment notes the enclosing row is the tap target and it is decorative unless independently wrapped. Verify each call site honours that, but no change is needed to the component.

---

## 7.4 WARNING — Five charts with no VoiceOver representation

**Files:** `Features/Labor/RoleDonutChart.swift`, `Features/Labor/LaborPerformanceChart.swift`, `Features/FoodCost/FoodCostDonutChart.swift`, `Features/FoodCost/FoodCostTrendChart.swift`, `Features/Home/ValueChartCard.swift` — grep for `accessibility` across all five returns nothing.

These charts *are* the product's core value: labor cost against target, food cost trend, recoverable waste, value delivered. To a VoiceOver user they are silent, unlabelled drawing. The numbers exist in the view models but are never exposed.

**After** — one summary label per chart makes the data available without rebuilding anything visual:
```swift
// RoleDonutChart.swift
var body: some View {
    ZStack { /* ...existing arcs... */ }
        // A donut chart is pure drawing to VoiceOver. This exposes the same
        // information the sighted user gets from the arcs, as one readable
        // summary rather than an unlabelled image.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Labor cost by role")
        .accessibilityValue(
            slices
                .map { "\($0.role), \(Int($0.percentage)) percent, \(formatCurrency($0.amount))" }
                .joined(separator: ". ")
        )
}
```
```swift
// FoodCostTrendChart.swift — a trend needs direction, not just values
.accessibilityElement(children: .ignore)
.accessibilityLabel("Food cost trend, last \(points.count) weeks")
.accessibilityValue(
    "Currently \(String(format: "%.1f", points.last?.value ?? 0)) percent, "
    + "\(trendDirection) from \(String(format: "%.1f", points.first?.value ?? 0)) percent"
)
```

For richer support, adopt `AXChartDescriptor` so VoiceOver's Audio Graphs feature can play the series — but the labels above are the high-value 20%.

---

## 7.5 WARNING — Icon-only controls are unlabelled

Representative sites:

| File:line | Control | Announced today |
|---|---|---|
| `RootView.swift:192–204` | Ask Cavnar FAB | "Button" |
| `AccountSheetKit.swift:583` | sheet dismiss chevron | "chevron.left" |
| `AskCavnarView.swift:200–212` | send message | "arrow.up" |
| `HomeView.swift` toolbar | notifications bell | "bell" |
| `AccountView.swift:401` / `AccountSheetKit.swift:297` | status dots | nothing |

SF Symbol names are read literally when no label is supplied, so a VoiceOver user hears "chevron dot left" instead of "Back".

**After:**
```swift
// RootView.swift — the FAB
AskCavnarFAB(...) { ... }
    .accessibilityLabel("Ask Cavnar AI")
    .accessibilityHint("Opens a chat with your restaurant intelligence consultant")

// AccountSheetKit.swift:583 — dismiss chevron
Image(systemName: "chevron.left")
    // ...
    .accessibilityLabel("Back")

// AskCavnarView.swift — send
.accessibilityLabel("Send question")
.accessibilityHint(viewModel.canSubmit ? "" : "Type a question first")
```

Status pills that convey meaning through **colour alone** (green/amber dots at `AccountView.swift:401`, `AccountSheetKit.swift:297`) fail WCAG 1.4.1 for colour-blind users as well as VoiceOver users:
```swift
// AccountSheetKit.swift — AccountPill
HStack(spacing: 6) {
    Circle().fill(on ? Color.cavnarGreen : Color.cavnarInk3).frame(width: 6, height: 6)
    Text(text)
}
// Colour alone carries the on/off meaning here — restate it for VoiceOver
// and for anyone who cannot distinguish the green from the grey.
.accessibilityElement(children: .combine)
.accessibilityLabel("\(text), \(on ? "active" : "inactive")")
```

---

## 7.6 WARNING — Reduce Motion honoured in 1 of 21 animating files

**Only file that checks it:** `Features/Auth/LoginBackground.swift` lines 23, 236
**Files that animate without checking:** 11 `repeatForever` sites + 9 unguarded `TimelineView(.animation)` sites, including `RootView.swift:363` (16s infinite rotation), `AIConsultantView.swift:54`, `ViewModifiers.swift:487,643`, and the whole `CavnarMotion.swift` loading vocabulary.

Reduce Motion exists for users who experience nausea, dizziness or migraine from continuous movement. The login background respects it; every screen after login does not.

**After** — one environment read plus a guard at each looping site (also cuts idle GPU cost, per Pass 3 §3.6):
```swift
// RootView.swift:363 — representative
@Environment(\.accessibilityReduceMotion) private var reduceMotion

// ...
if reduceMotion {
    // Settle straight to the resting state rather than looping.
    ringRotation = 0
} else {
    withAnimation(.linear(duration: 16).repeatForever(autoreverses: false)) {
        ringRotation = 360
    }
}
```

For the `TimelineView` loading components, gate the schedule rather than the view:
```swift
// CavnarMotion.swift — CavnarComposingLines etc.
TimelineView(.animation(paused: reduceMotion)) { timeline in
```

---

## 7.7 WARNING — Dark-only UI with no contrast adaptation for bright environments

**File:** `RootView.swift` line 87

```swift
.preferredColorScheme(.dark)
```

The dark-only choice is deliberate and documented (`RootView.swift:84–86`), and for a dim dining room it is the right call. But this app is also used **at the pass under 4000K task lighting, and outdoors on a patio in daylight** — the two environments where a dark UI with low-contrast secondary text is hardest to read.

`Color.cavnarInk3` (the app's secondary text colour, used for virtually every label and caption) on `Color.cavnarPaper` is well below the WCAG AA 4.5:1 threshold for body text. There is also no `@Environment(\.colorSchemeContrast)` or `accessibilityDifferentiateWithoutColor` handling anywhere in the codebase.

**After** — keep the dark identity, but respond to the system's Increase Contrast setting:
```swift
// Color+Cavnar.swift
extension Color {
    /// Secondary text. Increase Contrast is the system signal that the user
    /// is struggling to read — commonly set by people working under bright
    /// task lighting or outdoors, exactly this product's environment. Lift
    /// the value rather than ignoring the request.
    static func cavnarInk3(_ contrast: ColorSchemeContrast) -> Color {
        contrast == .increased ? Color(white: 0.82) : Color("Ink3")
    }
}
```
```swift
// Usage at call sites that carry real information (not decorative chrome)
@Environment(\.colorSchemeContrast) private var contrast
// ...
.foregroundStyle(Color.cavnarInk3(contrast))
```

Verify the resulting pairs with a contrast checker; target 4.5:1 for body text and 3:1 for large text at the default setting, not only the increased one.

---

## 7.8 OPTIMIZATION — Layout shift as async content replaces loading states

**Files:** `Features/Home/HomeView.swift` lines 129+, `Features/Account/AccountView.swift` lines 16–29, `Features/Intel/IntelView.swift` lines 51+

The pattern throughout is a three-branch swap:
```swift
if let summary = viewModel.summary {
    content(summary)
} else if viewModel.isLoading {
    CavnarLoadingSeal()
        .padding(.top, 60)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
} else if let error = viewModel.errorMessage {
    // ...
}
```

The loading seal and the real content have unrelated heights, so when data lands the whole screen jumps — content reflows, the scroll position shifts, and anything the user was about to tap moves. On a slow restaurant connection this window is long enough to cause genuine mis-taps.

The codebase already has the right primitive for this (`CavnarSkeletonLines`, used elsewhere); it just is not applied on the main screens.

**After** — reserve the real shape while loading:
```swift
if let summary = viewModel.summary {
    content(summary)
} else if viewModel.isLoading {
    // Skeleton matching the real hero + tile geometry, so content lands into
    // reserved space instead of shoving the page down when it arrives.
    VStack(alignment: .leading, spacing: 20) {
        CavnarSkeletonLines(widths: [0.6, 0.35], lineHeight: 22, spacing: 10)
            .frame(height: HomeView.heroBackgroundHeight)
        HStack(spacing: 12) {
            ForEach(0..<3, id: \.self) { _ in
                CavnarSkeletonLines(widths: [0.8, 0.5], lineHeight: 14, spacing: 8)
                    .frame(height: 92)
                    .frame(maxWidth: .infinity)
            }
        }
    }
    .padding(20)
    .transition(.opacity)
}
```
and cross-fade rather than hard-cut:
```swift
.animation(.easeOut(duration: 0.25), value: viewModel.summary == nil)
```

---

## ✅ What this codebase already gets right

- **A real, disciplined colour-token system.** 18 named colour sets in `Assets.xcassets/Colors/`, no scattered hex literals — enforced by a CI lint (`scripts/check_colors.py`) that fails the build on theme-unsafe literal colours. That is stronger colour governance than most production apps have, and it makes the contrast fix in 7.7 a token-level change rather than a hunt.
- **Primary list rows already meet the 44pt minimum.** `AccountView`'s settings rows are 54pt and `AccountKVRow` is 38pt-plus-26pt-padding — comfortably above the HIG floor. The tap-target failures in 7.3 are confined to small auxiliary controls, not the main navigation surface.
- **Loading states are informative, not generic.** Skeleton components (`CavnarComposingLines`, `CavnarSkeletonLines`, `CavnarShimmerText`) communicate *what* is loading rather than showing an indeterminate spinner, and the language is consistent app-wide.
- **Every destructive action has considered feedback.** `.cavnarPostedOverlay` with a `.removed` (red) tone for removals versus `.success` (ember) for additions, and `confirmationDialog` on the higher-stakes ones (retract reply, revoke teammate, regenerate backup codes).
- **Keyboard handling is genuinely thorough.** `.keyboardDoneToolbar` / `.keyboardNavToolbar` on every form, `.scrollDismissesKeyboard(.interactively)` in the chat view, and a documented fix for the keyboard-accessory/input-bar collision in `AskCavnarView.swift:113–119`.
- **Motion has an explicit design language.** 15 approved animations with documented rationale in `CavnarMotion.swift`, ember-only accent, no bounce — the problem in 7.6 is the missing Reduce Motion gate, not undisciplined animation.
