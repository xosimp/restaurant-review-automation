# Audit Pass 6 — Location, Geocontext & Offline Resiliency Audit

**Target:** `ios/CavnarAI/CavnarAI/` + the caching layer in `Features/Labor/`, `Features/FoodCost/`
**Focus:** CoreLocation cost/compliance, background execution, in-building network loss, offline write conflicts


> **Remediation status (ff937cc): 5/5 findings fixed.** All fixed.
> Verified by: clean `xcodebuild` (0 errors, 0 warnings), 646 backend tests passing,
> `scripts/check_colors.py` clean, and a per-finding grep confirming each original
> code signature is gone. Findings below are kept as written (plus explicit
> **Correction** notes where the original analysis was wrong) so the reasoning
> stays auditable rather than being rewritten after the fact.

---

## Executive summary — location

**This app uses no location services whatsoever.** Verified by exhaustive search: zero occurrences of `import CoreLocation`, `CLLocationManager`, `requestWhenInUseAuthorization`, significant-change monitoring, geofencing, or any latitude/longitude handling anywhere in the 121 Swift files. `Info.plist` correspondingly declares **no** `NSLocationWhenInUseUsageDescription` or `NSLocationAlwaysAndWhenInUseUsageDescription`, and `UIBackgroundModes` contains only `remote-notification`.

Every risk this pass was asked to look for on the location side is therefore **absent by construction**:

- ❌ No battery drain from continuous high-accuracy tracking — no tracking at all.
- ❌ No App Store rejection risk from background-location justification — the entitlement isn't requested.
- ❌ No missing purpose strings — none are needed.

Geocoding *does* happen, but entirely server-side and once per restaurant: `models.py:266–267` stores `latitude`/`longitude` "geocoded once from google_place_id, cached," used for the weather feature. The device is never involved. **For a fixed-premises product this is the correct architecture** — a restaurant does not move, so deriving its position from its Google Place ID rather than the owner's phone is both cheaper and more private. No change recommended.

## Executive summary — offline

Offline resiliency is where this pass finds real gaps. There is **no network-state awareness anywhere in the app** — no `NWPathMonitor`, no reachability check, no connectivity UI. There is also **no request timeout configuration**, so a phone in a basement walk-in hangs on `URLSession`'s 60-second default before showing an error. Read caching exists but covers only 3 of 8 screens, and there is no write queue at all: all 51 write endpoints simply fail.

| # | Severity | Finding |
|---|---|---|
| 6.1 | CRITICAL | No write queue — 51 mutating endpoints fail permanently offline, silently losing work |
| 6.2 | WARNING | 60-second default timeout means a dead-zone request hangs for a full minute |
| 6.3 | WARNING | No connectivity awareness — the app cannot say "you're offline" |
| 6.4 | WARNING | Read caching covers Labor and Food Cost only; Home/Reviews/Intel show error screens |
| 6.5 | OPTIMIZATION | Cached data has no staleness indicator — old numbers look live |
| — | ✅ PASS | No location usage; correct server-side geocoding; sound cache-staleness logic where caching exists |

---

## 6.1 CRITICAL — No offline write queue: work is lost, not deferred

**Files:** 51 `method: .post` / `.delete` call sites across `Features/`; representative: `Features/Reviews/ReviewDetailViewModel.swift` lines 100–127 (`approve`), `Features/FoodCost/FoodCostQuickEntryView.swift`, `Features/Labor/LaborViewModel.swift`

Every mutation in the app is fire-and-forget-on-failure. `approve()` is typical:

```swift
do {
    let response: ApproveResponse = try await client.send(
        "/mobile/api/reviews/\(review.id)/approve", method: .post
    )
    // ...
} catch let error as APIClient.APIError {
    errorMessage = error.message          // shown, then gone
}
```

Consider the actual environment this product is used in: a manager standing at the pass, on a phone with one bar, working through the morning's reviews. They edit a response — the debounced autosave (`ReviewDetailViewModel.swift:80–88`) fires into a dead connection and fails silently — then tap Approve, which also fails. **Their edit is gone.** There is no retry queue, no draft persistence, no "will send when you're back online." The same applies to Food Cost quick-entry (a screen explicitly designed for rapid on-the-floor data entry, which is exactly where signal is worst).

This is the single most consequential gap in the app for its stated deployment environment.

**After** — a durable outbox that survives relaunch and drains on reconnect:

```swift
// New: Core/PendingWriteQueue.swift
import Foundation

/// Durable outbox for mutations made while offline. A restaurant's worst
/// signal is exactly where the work happens (walk-in, basement prep, back
/// office), so a failed write must be deferred, not discarded — today every
/// one of the app's 51 write endpoints simply loses the user's work.
actor PendingWriteQueue {
    static let shared = PendingWriteQueue()

    struct PendingWrite: Codable, Identifiable {
        let id: UUID
        let path: String
        let method: String
        let bodyJSON: Data?
        let createdAt: Date
        /// Human-readable, for the "3 changes waiting to sync" UI.
        let label: String
    }

    private var queue: [PendingWrite] = []
    private let storeURL: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("pending-writes.json")
    }()

    init() { queue = (try? JSONDecoder().decode([PendingWrite].self, from: Data(contentsOf: storeURL))) ?? [] }

    var pendingCount: Int { queue.count }

    func enqueue(_ write: PendingWrite) {
        queue.append(write)
        persist()
    }

    /// Drains oldest-first, stopping at the first failure so ordering is
    /// preserved (an approve must not overtake the draft save it depends on).
    func drain(using client: APIClient) async {
        while let next = queue.first {
            do {
                _ = try await client.sendRaw(path: next.path, method: next.method, body: next.bodyJSON)
                queue.removeFirst()
                persist()
            } catch {
                return
            }
        }
    }

    private func persist() {
        try? JSONEncoder().encode(queue).write(to: storeURL, options: .completeFileProtection)
    }
}
```

```swift
// ReviewDetailViewModel.swift — after
} catch let error as APIClient.APIError where error.kind == .offline {
    await PendingWriteQueue.shared.enqueue(.init(
        id: UUID(),
        path: "/mobile/api/reviews/\(review.id)/approve",
        method: "POST",
        bodyJSON: nil,
        createdAt: Date(),
        label: "Approve response for \(review.author ?? "review")"
    ))
    // Optimistic locally, but honestly labelled — the row shows a "waiting
    // to sync" chip rather than claiming it posted.
    currentStatus = "pending-sync"
    didComplete = true
}
```

Drain on reconnect (see 6.3) and on `scenePhase == .active`.

---

## 6.2 WARNING — 60-second default timeout in an environment built for dead zones

**File:** `Core/APIClient.swift` lines 33–36

```swift
init(baseURL: URL = AppEnvironment.baseURL, session: URLSession = .shared) {
    self.baseURL = baseURL
    self.session = session
}
```

No `URLSessionConfiguration` is created, so every request inherits `timeoutIntervalForRequest = 60`. A manager who walks into the walk-in cooler mid-tap sits watching a shimmer skeleton for a **full minute** before any error appears. Most will conclude the app is broken and force-quit well before then — which, per 6.1, discards the write entirely.

**After** — 20 seconds is generous for these payloads and keeps the failure inside a user's patience window:
```swift
init(baseURL: URL = AppEnvironment.baseURL, session: URLSession? = nil) {
    self.baseURL = baseURL
    if let session {
        self.session = session
    } else {
        let config = URLSessionConfiguration.ephemeral
        // 60s (the default) is far too long for a phone that just walked into
        // a walk-in cooler — the user force-quits long before the error lands.
        config.timeoutIntervalForRequest = 20
        config.timeoutIntervalForResource = 45
        // False on purpose: the queue in PendingWriteQueue handles deferral
        // explicitly, with UI. A silently-waiting URLSession task gives the
        // user no feedback at all.
        config.waitsForConnectivity = false
        self.session = URLSession(configuration: config)
    }
}
```

*(This is the same initialiser change recommended in Pass 1 §1.3/§1.5 — apply once, it addresses pinning, disk caching and timeouts together.)*

---

## 6.3 WARNING — The app has no idea whether it is online

**Files:** entire target — no `NWPathMonitor`, `NWPath`, or reachability implementation exists

Every failure produces the same string regardless of cause (`APIClient.swift:104`): *"Couldn't reach the server — check your connection and try again."* The app cannot distinguish a genuinely offline device from a backend outage, cannot proactively tell the user "you're offline, here's cached data from 20 minutes ago," and — critically for 6.1 — has **no signal on which to drain a write queue**.

**After** — one observable monitor, injected at the root:
```swift
// New: Core/NetworkMonitor.swift
import Network
import Observation

/// The app's only source of connectivity truth. Beyond messaging ("you're
/// offline" vs "the server is down"), this is the trigger that drains
/// PendingWriteQueue — without it, deferred writes have nothing to wake them.
@Observable
@MainActor
final class NetworkMonitor {
    private(set) var isOnline = true
    private(set) var isConstrained = false      // Low Data Mode / hotspot

    private let monitor = NWPathMonitor()

    init() {
        monitor.pathUpdateHandler = { [weak self] path in
            Task { @MainActor in
                let wasOffline = self?.isOnline == false
                self?.isOnline = path.status == .satisfied
                self?.isConstrained = path.isConstrained
                if wasOffline, path.status == .satisfied {
                    await PendingWriteQueue.shared.drain(using: .shared)
                }
            }
        }
        monitor.start(queue: DispatchQueue(label: "cavnar.network-monitor"))
    }
}
```
```swift
// RootView.swift
@State private var network = NetworkMonitor()
// ...
.environment(network)
.overlay(alignment: .top) {
    if !network.isOnline {
        Text("Offline — showing your last update")
            .font(.cavnarBody(13.5, weight: 600))
            .foregroundStyle(Color.cavnarInk)
            .padding(.horizontal, 14).padding(.vertical, 8)
            .background(Color.cavnarAmber.opacity(0.9), in: Capsule())
            .padding(.top, 8)
            .transition(.move(edge: .top).combined(with: .opacity))
    }
}
.animation(.easeOut(duration: 0.25), value: network.isOnline)
```

---

## 6.4 WARNING — Read caching covers 3 screens; the other 5 show error states

**Files with caching:** `Features/Labor/LaborViewModel.swift` (286–360), `Features/Labor/LaborAnalyticsViewModel.swift` (58–83), `Features/FoodCost/FoodCostAnalyticsViewModel.swift` (33–41)
**Files without:** `HomeViewModel`, `ReviewsListViewModel`, `IntelViewModel`, `MarketingViewModel`, `AccountViewModel`

Labor's caching exists because a real bug forced it — the doc comment at `LaborViewModel.swift:270–285` recounts "the schedule keeps disappearing after I come back into the app" and the two-part fix (schedule *and* stats both needed caching). That fix was correct, but it was applied narrowly to the screen that reported the bug. Home, Reviews and Intel have the identical exposure: no cache, so an offline launch shows a full-screen error with a Retry button and **nothing else** — even though the owner opened the app specifically to check numbers they saw an hour ago.

**After** — generalise Labor's proven pattern rather than reimplementing it per screen:
```swift
// New: Core/CachedResource.swift
import Foundation

/// Extracted from LaborViewModel's caching, which exists because of a real
/// reported bug ("the schedule keeps disappearing"). Home/Reviews/Intel have
/// the same exposure and no cache — an offline launch shows them a bare error
/// screen instead of the numbers the owner opened the app to see.
struct CachedResource<T: Codable> {
    let key: String

    func load() -> T? {
        guard let data = SecureCache.read(key: key) else { return nil }   // see Pass 1 §1.2
        return try? Self.decoder.decode(T.self, from: data)
    }

    func save(_ value: T) {
        guard let data = try? Self.encoder.encode(value) else { return }
        SecureCache.write(data, key: key)
    }

    // Same non-finite tolerance LaborViewModel needed: a NaN anywhere in a
    // payload otherwise makes the whole encode fail silently and the write
    // never happens.
    private static var encoder: JSONEncoder {
        let e = JSONEncoder()
        e.nonConformingFloatEncodingStrategy = .convertToString(positiveInfinity: "inf", negativeInfinity: "-inf", nan: "nan")
        return e
    }
    private static var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.nonConformingFloatDecodingStrategy = .convertFromString(positiveInfinity: "inf", negativeInfinity: "-inf", nan: "nan")
        return d
    }
}
```
```swift
// HomeViewModel.swift — after
private let cache = CachedResource<HomeSummary>(key: "home.summary")

func load() async {
    if summary == nil { summary = cache.load() }     // instant warm start, offline-safe
    do {
        let fetched: HomeSummary = try await client.send("/mobile/api/home")
        summary = fetched
        cache.save(fetched)
        lastLoadedAt = Date()
    } catch {
        // Only surface an error if there is genuinely nothing to show.
        if summary == nil { errorMessage = "Couldn't load your dashboard." }
    }
}
```

---

## 6.5 OPTIMIZATION — Cached data is presented as if it were live

**Files:** `Features/Labor/LaborViewModel.swift` lines 286–296; `Features/Labor/LaborAnalyticsViewModel.swift` lines 58–71

`configureCaching` restores cached stats and schedules straight into the same properties a live fetch populates, with no visual distinction. The schedule cache has a genuinely well-reasoned staleness rule (`isStale`, lines 327–342 — a schedule whose week has already ended is not resurrected, with a day of slack to absorb timezone skew), but `stats` is explicitly cached with **no staleness check at all** (documented at line 283). So an owner can be looking at labor percentages from days ago, rendered identically to today's.

For a decision-support product, an undated number is worse than no number.

**After** — timestamp the cache and surface the age when it is not fresh:
```swift
// LaborViewModel.swift
private(set) var statsCachedAt: Date?

func cacheStats(_ stats: LaborStats) {
    guard let restaurantId, let data = try? Self.cacheEncoder.encode(stats) else { return }
    SecureCache.write(data, key: Self.statsCacheKey(restaurantId))
    UserDefaults.standard.set(Date(), forKey: Self.statsCacheKey(restaurantId) + ".at")
    statsCachedAt = Date()
}

/// Non-nil when what's on screen came from cache and is old enough that the
/// owner should know before acting on it.
var stalenessNotice: String? {
    guard let statsCachedAt, Date().timeIntervalSince(statsCachedAt) > 3600 else { return nil }
    return "Last updated \(AccountRelativeTime.describe(ISO8601DateFormatter().string(from: statsCachedAt)))"
}
```
```swift
// LaborView.swift — beneath the stats header
if let notice = viewModel.stalenessNotice {
    Label(notice, systemImage: "clock.arrow.circlepath")
        .font(.cavnarBody(13))
        .foregroundStyle(Color.cavnarAmber)
}
```

---

## On "offline-first cache conflicts" specifically

The audit brief asked about local edits to menus/layouts/intel conflicting with Railway once connectivity returns. **That conflict class does not currently exist**, because there are no offline edits — writes fail rather than queue (6.1). It becomes a live concern the moment the write queue in 6.1 is built, so the design guidance belongs here:

- **Server-authoritative, last-write-wins is correct for this data.** Reviews, labor settings and alert preferences are single-editor-per-restaurant in practice; full CRDT/OT machinery would be unjustified complexity.
- **The one genuine multi-writer surface is review responses**, now that Pass 4's team-invite feature allows a second login. Two managers drafting a response to the same review offline will silently clobber each other on reconnect. Mitigate with an `If-Unmodified-Since`-style guard: include the review's `updated_at` in the queued write and have the backend reject on mismatch, surfacing "someone else responded to this review while you were offline."
- **Never replay a queued write blindly after a long gap.** Stamp `createdAt` (already in the `PendingWrite` struct above) and drop or confirm writes older than ~24h — approving a review response drafted two days ago against data that has since changed is worse than failing.
