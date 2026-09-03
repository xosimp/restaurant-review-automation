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
