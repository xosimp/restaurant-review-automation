import Foundation

/// Read-through cache for a decodable payload, backed by SecureCache.
///
/// Generalised from LaborViewModel's own caching, which exists because of a
/// real reported bug ("the schedule keeps disappearing after I come back into
/// the app"). Home, Reviews, Intel and Marketing had the identical exposure
/// with no cache at all — an offline launch showed them a bare error screen
/// instead of the numbers the owner opened the app to check (audit 6.4).
struct CachedResource<T: Codable> {
    let key: String

    func load() -> T? {
        guard let data = SecureCache.read(key: key) else { return nil }
        return try? Self.decoder.decode(T.self, from: data)
    }

    /// Same as load(), with the file read on a background thread. Home's
    /// first load runs in the sign-in frame; a synchronous disk read there
    /// was part of that frame's stall (DebugFrameWatchdog).
    func loadOffMain() async -> T? {
        let key = self.key
        let data = await Task.detached(priority: .userInitiated) { SecureCache.read(key: key) }.value
        guard let data else { return nil }
        return try? Self.decoder.decode(T.self, from: data)
    }

    func save(_ value: T) {
        guard let data = try? Self.encoder.encode(value) else { return }
        SecureCache.write(data, key: key)
    }

    /// When this cache was last written — drives the "showing data from 2h
    /// ago" notice rather than passing stale numbers off as live.
    var cachedAt: Date? { SecureCache.modifiedAt(key: key) }

    // Non-finite tolerance carried over from LaborViewModel's own encoder: a
    // single NaN anywhere in a payload otherwise makes encode() throw, and
    // under a `try?` that means the write silently never happens at all.
    private static var encoder: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.nonConformingFloatEncodingStrategy = .convertToString(
            positiveInfinity: "inf", negativeInfinity: "-inf", nan: "nan"
        )
        return encoder
    }

    private static var decoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.nonConformingFloatDecodingStrategy = .convertFromString(
            positiveInfinity: "inf", negativeInfinity: "-inf", nan: "nan"
        )
        return decoder
    }
}

/// A screen's last good answer, kept as the server's own bytes.
///
/// CachedResource needs its payload Codable, and Reviews, Intel, Marketing,
/// Food Cost and the Daily Report decode into Decodable-only models (custom
/// init(from:), lenient fields) that would each need a hand-written encode
/// to round-trip. So these keep the raw body `APIClient.sendKeepingBody`
/// hands back, and decode it again with the very decoder the live answer
/// went through — a warm start can never read differently from the fetch
/// that stored it.
///
/// Same rules as Home's cache: in SecureCache (complete file protection,
/// purged on sign-out and on a location switch), keyed by user and
/// restaurant (SessionScope.key), and never written by a fetch that
/// finished after the session it started in had ended.
@MainActor
struct ResponseCache<T: Decodable> {
    let base: String

    init(_ base: String) { self.base = base }

    struct Hit {
        let value: T
        /// When the copy was stored — what "Showing data from 2h ago" counts.
        let savedAt: Date
    }

    /// The stored copy for the current session, read off the main thread.
    func load() async -> Hit? {
        let key = SessionScope.key(base)
        let read = await Task.detached(priority: .userInitiated) { () -> (Data, Date?)? in
            guard let data = SecureCache.read(key: key) else { return nil }
            return (data, SecureCache.modifiedAt(key: key))
        }.value
        guard let read, let value = try? JSONDecoder.cavnar.decode(T.self, from: read.0) else {
            return nil
        }
        return Hit(value: value, savedAt: read.1 ?? .distantPast)
    }

    /// Stores a live answer's body — unless the session moved on (sign-out,
    /// a location switch) while it was in flight: the purge has already run,
    /// and writing now would put the old account's data back (CLIENT-26).
    func save(_ body: Data, generation: Int) {
        guard generation == SessionScope.generation else { return }
        SecureCache.write(body, key: SessionScope.key(base))
    }
}

/// Several answers kept as one ResponseCache entry: each server body under
/// its own name, `{"n":30,"a":<body>,"b":<body>}`. Built from bytes the
/// server already sent as JSON, so the envelope is JSON too; a part that
/// did not answer is left out and decodes as nil.
enum CacheEnvelope {
    static func make(_ parts: [(String, Data?)], numbers: [(String, Int)] = []) -> Data {
        var fields: [Data] = numbers.map { Data("\"\($0.0)\":\($0.1)".utf8) }
        for (name, body) in parts {
            guard let body, !body.isEmpty else { continue }
            fields.append(Data("\"\(name)\":".utf8) + body)
        }
        var out = Data("{".utf8)
        for (i, field) in fields.enumerated() {
            if i > 0 { out.append(Data(",".utf8)) }
            out.append(field)
        }
        out.append(Data("}".utf8))
        return out
    }
}

/// The line a screen shows while its figures came from the device cache —
/// Home's wording (HomeViewModel.stalenessNotice), for every cached screen.
enum CacheFreshness {
    /// "Showing data from 12m ago" / "… 3h ago"; nil when nothing on screen
    /// came from the cache, or it is under five minutes old (not worth a
    /// line).
    static func notice(savedAt: Date?, now: Date = Date()) -> String? {
        guard let savedAt else { return nil }
        let minutes = Int(now.timeIntervalSince(savedAt) / 60)
        if minutes < 5 { return nil }
        if minutes < 60 { return "Showing data from \(minutes)m ago" }
        return "Showing data from \(minutes / 60)h ago"
    }
}
