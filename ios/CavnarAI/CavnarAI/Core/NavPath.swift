import Foundation

/// One address for every place and item an owner can be sent to — the
/// same string the server puts on attention items, action-queue items,
/// notifications, push payloads and command results (`nav.py`), and that
/// the web opens with `cavNav(path)` (Friction audit, 9/25/26).
///
/// `<head>[/<rest>][?<key>=<value>&…]` — e.g. "reviews?filter=urgent",
/// "review/412", "labor/schedule", "person/dana-k", "dsr/night/2026-09-24".
/// An unknown head, section or id degrades to its module (or Home).
struct NavPath: Hashable {
    let raw: String
    /// The first segment: a module ("reviews", "labor", …) or an item kind
    /// ("review", "schedule", "person", …).
    let head: String
    /// The segments after the head: a section name or an item id.
    let rest: [String]
    let query: [String: String]

    init?(_ raw: String?) {
        guard let raw = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else { return nil }
        self.raw = raw
        let pieces = raw.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)
        let segs = pieces[0].split(separator: "/").map { String($0).removingPercentEncoding ?? String($0) }
        guard let first = segs.first, !first.isEmpty else { return nil }
        head = first.lowercased()
        rest = Array(segs.dropFirst())
        var q: [String: String] = [:]
        if pieces.count > 1 {
            for pair in pieces[1].split(separator: "&") {
                let kv = pair.split(separator: "=", maxSplits: 1)
                guard let k = kv.first else { continue }
                let v = kv.count > 1 ? String(kv[1]) : ""
                q[String(k).removingPercentEncoding ?? String(k)] =
                    (v.replacingOccurrences(of: "+", with: " ").removingPercentEncoding ?? v)
            }
        }
        query = q
    }

    static let itemModules: [String: String] = [
        "review": "reviews", "schedule": "labor", "person": "labor", "invoice": "inventory",
        "order": "inventory", "issue": "home", "request": "labor", "action": "home", "location": "home",
        "food": "inventory", "proposal": "home",
    ]

    /// The module this path lives in ("review/412" → "reviews").
    var module: String { Self.itemModules[head] ?? head }
    /// The item id or section — the first segment after the head.
    var target: String? { rest.first }
}

extension Notification.Name {
    /// Post with `object: NavPath` to send the app to a place or item; the
    /// deep-link router owns the handling (one path for pushes, cards,
    /// notification rows and the command sheet).
    static let cavnarOpenNav = Notification.Name("cavnarOpenNav")
}
