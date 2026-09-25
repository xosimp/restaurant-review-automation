import Foundation

// MARK: - The Restaurant Data Health Score (data_health.py)
//
// GET /mobile/api/data-health, the `data_health` summary Home carries, and
// the one-line status the server writes for a connection ("Last sync
// 3:02am · Sales through 9/19/26"). Every field is optional and every
// decoder lenient: an odd value is empty, never a screen that fails to
// load, and an older server that sends none of it keeps the old behaviour.

/// Decoding helpers shared by the types below.
private extension KeyedDecodingContainer {
    func text(_ key: Key) -> String? {
        guard let s = (try? decodeIfPresent(String.self, forKey: key)) ?? nil else { return nil }
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }

    /// An integer the server may send as 71 or 71.0 — never a failed decode.
    func int(_ key: Key) -> Int? {
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return i }
        if let d = (try? decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite { return Int(d.rounded()) }
        return nil
    }

    func bool(_ key: Key) -> Bool? { (try? decodeIfPresent(Bool.self, forKey: key)) ?? nil }

    /// A list read element by element: an element that doesn't decode is
    /// skipped, a value that isn't a list is empty.
    func list<T: Decodable>(_ type: T.Type, _ key: Key) -> [T] {
        (try? decodeIfPresent(DataHealthList<T>.self, forKey: key))?.items ?? []
    }
}

/// `[T]` decoded one element at a time — the odd element is stepped over.
struct DataHealthList<T: Decodable>: Decodable {
    let items: [T]

    init(from decoder: Decoder) throws {
        guard var list = try? decoder.unkeyedContainer() else { items = []; return }
        var out: [T] = []
        while !list.isAtEnd {
            let before = list.currentIndex
            if let v = try? list.decode(T.self) {
                out.append(v)
            } else {
                _ = try? list.decode(JSONValue.self)
            }
            if list.currentIndex == before { break }
        }
        items = out
    }
}

// MARK: - Overall

/// `overall: {pct, state, label, caps_applied, reason}` — the score, its
/// state (current | aging | stale | pending | not_connected), the owner's
/// label ("71% data health") and why it sits where it does.
struct DataHealthOverall: Codable, Hashable {
    var pct: Int? = nil
    var state: String? = nil
    var label: String? = nil
    var capsApplied: [String] = []
    var reason: String? = nil

    enum CodingKeys: String, CodingKey {
        case pct, state, label, reason
        case capsApplied = "caps_applied"
    }

    init(pct: Int? = nil, state: String? = nil, label: String? = nil, capsApplied: [String] = [], reason: String? = nil) {
        self.pct = pct; self.state = state; self.label = label; self.capsApplied = capsApplied; self.reason = reason
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        pct = c.int(.pct).map { max(0, min(100, $0)) }
        state = c.text(.state)?.lowercased()
        label = c.text(.label)
        capsApplied = c.list(String.self, .capsApplied)
        reason = c.text(.reason)
    }

    /// Nothing has synced yet — the kicker says so instead of a score.
    var isPending: Bool { state == "pending" }
}

/// Home's `data_health: {overall, worst_line}` (may be null).
struct HomeDataHealth: Codable, Hashable {
    var overall: DataHealthOverall? = nil
    var worstLine: String? = nil

    enum CodingKeys: String, CodingKey {
        case overall
        case worstLine = "worst_line"
    }

    init(overall: DataHealthOverall? = nil, worstLine: String? = nil) {
        self.overall = overall; self.worstLine = worstLine
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        overall = (try? c.decodeIfPresent(DataHealthOverall.self, forKey: .overall)) ?? nil
        worstLine = c.text(.worstLine)
    }
}

// MARK: - The full payload

struct DataHealthSnapshot: Decodable {
    var ok: Bool = false
    var error: String? = nil
    var generatedAt: String? = nil
    var overall: DataHealthOverall? = nil
    var worstLine: String? = nil
    var countCurrent: Int? = nil
    var sources: [DataHealthSource] = []
    var notConnected: [DataHealthNotConnected] = []
    var modules: [DataHealthModule] = []

    enum CodingKeys: String, CodingKey {
        case ok, error, overall, sources, modules
        case generatedAt = "generated_at"
        case worstLine = "worst_line"
        case countCurrent = "count_current"
        case notConnected = "not_connected"
    }

    init(ok: Bool = true, overall: DataHealthOverall? = nil, sources: [DataHealthSource] = [],
         notConnected: [DataHealthNotConnected] = [], modules: [DataHealthModule] = []) {
        self.ok = ok; self.overall = overall; self.sources = sources
        self.notConnected = notConnected; self.modules = modules
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        ok = c.bool(.ok) ?? false
        error = c.text(.error)
        generatedAt = c.text(.generatedAt)
        overall = (try? c.decodeIfPresent(DataHealthOverall.self, forKey: .overall)) ?? nil
        worstLine = c.text(.worstLine)
        countCurrent = c.int(.countCurrent)
        sources = c.list(DataHealthSource.self, .sources)
        notConnected = c.list(DataHealthNotConnected.self, .notConnected)
        modules = c.list(DataHealthModule.self, .modules)
    }

    /// Whether "Sync now" has anything to start.
    var canSyncNow: Bool { sources.contains { $0.canSyncNow } }

    /// The modules whose recommendations the data's age moves, each with
    /// the server's sentence ("Food cost recommendations 81% → 94% once
    /// inventory counts are current").
    var impactLines: [String] {
        modules.compactMap { $0.confidenceImpact?.line }
    }

    /// The sources behind one module (labor, food_cost, intel…), weakest
    /// first — for the one-line badge a module screen carries.
    func sources(forModule module: String) -> [DataHealthSource] {
        guard let m = modules.first(where: { $0.module == module }) else { return [] }
        let keys = Set(m.sources)
        return sources.filter { keys.contains($0.key) }
            .sorted { ($0.healthPct ?? $0.pct ?? 101) < ($1.healthPct ?? $1.pct ?? 101) }
    }
}

struct DataHealthSource: Decodable, Identifiable, Hashable {
    struct Reliability: Decodable, Hashable {
        var pct: Int? = nil
        var ok: Int? = nil
        var attempts: Int? = nil
        var consecutiveFailures: Int? = nil
        /// "6 of the last 7 syncs succeeded".
        var basis: String? = nil

        enum CodingKeys: String, CodingKey {
            case pct, ok, attempts, basis
            case consecutiveFailures = "consecutive_failures"
        }

        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            pct = c.int(.pct); ok = c.int(.ok); attempts = c.int(.attempts)
            consecutiveFailures = c.int(.consecutiveFailures)
            basis = c.text(.basis)
        }
    }

    var key: String = ""
    var label: String? = nil
    var state: String? = nil
    /// ok | warn | bad | off.
    var tone: String? = nil
    var pct: Int? = nil
    var healthPct: Int? = nil
    /// "POS sales: Toast sales through 9/23/26 · synced 9/24/26 (synced 5
    /// hours ago)" — already in the restaurant's clock and M/D/YY.
    var line: String? = nil
    var synced: String? = nil
    var cadence: String? = nil
    var reliability: Reliability? = nil
    var error: String? = nil
    var pending: Bool = false
    /// "POS sync runs tonight 3am".
    var expectedLine: String? = nil
    var lastSyncLocal: String? = nil
    var countsCurrent: Bool = false
    var canSyncNow: Bool = false

    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, label, state, tone, pct, line, synced, cadence, reliability, error, pending
        case healthPct = "health_pct"
        case expectedLine = "expected_line"
        case lastSyncLocal = "last_sync_local"
        case countsCurrent = "counts_current"
        case canSyncNow = "can_sync_now"
    }

    init(key: String, label: String? = nil, tone: String? = nil, healthPct: Int? = nil, line: String? = nil,
         canSyncNow: Bool = false) {
        self.key = key; self.label = label; self.tone = tone; self.healthPct = healthPct
        self.line = line; self.canSyncNow = canSyncNow
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = c.text(.key) ?? ""
        label = c.text(.label)
        state = c.text(.state)?.lowercased()
        tone = c.text(.tone)?.lowercased()
        pct = c.int(.pct).map { max(0, min(100, $0)) }
        healthPct = c.int(.healthPct).map { max(0, min(100, $0)) }
        line = c.text(.line)
        synced = c.text(.synced)
        cadence = c.text(.cadence)
        reliability = (try? c.decodeIfPresent(Reliability.self, forKey: .reliability)) ?? nil
        error = c.text(.error)
        pending = c.bool(.pending) ?? false
        expectedLine = c.text(.expectedLine)
        lastSyncLocal = c.text(.lastSyncLocal)
        countsCurrent = c.bool(.countsCurrent) ?? false
        canSyncNow = c.bool(.canSyncNow) ?? false
    }

    /// What the row prints: the server's line, else its label.
    var displayLine: String { line ?? label ?? key }
    /// The row's figure: its health %, else its freshness %.
    var displayPct: Int? { healthPct ?? pct }
}

/// `{key, label, next}` — a source that isn't connected, and the one
/// action that would connect it ("Connect Google in Account → Connections").
struct DataHealthNotConnected: Decodable, Identifiable, Hashable {
    var key: String = ""
    var label: String? = nil
    var next: String? = nil
    var id: String { key }

    enum CodingKeys: String, CodingKey { case key, label, next }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = c.text(.key) ?? ""
        label = c.text(.label)
        next = c.text(.next)
    }
}

/// One module's readiness: proceed | caveat | wait | refuse, why, the
/// sources it reads, and what current data would do to its confidence.
struct DataHealthModule: Decodable, Identifiable, Hashable {
    struct Impact: Decodable, Hashable {
        var now: Int? = nil
        var whenCurrent: Int? = nil
        var delta: Int? = nil
        var line: String? = nil

        enum CodingKeys: String, CodingKey {
            case now, delta, line
            case whenCurrent = "when_current"
        }

        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            now = c.int(.now); whenCurrent = c.int(.whenCurrent); delta = c.int(.delta)
            line = c.text(.line)
        }
    }

    var module: String = ""
    var title: String? = nil
    var decision: String? = nil
    var reason: String? = nil
    var sources: [String] = []
    var confidenceImpact: Impact? = nil
    var id: String { module }

    enum CodingKeys: String, CodingKey {
        case module, title, decision, reason, sources
        case confidenceImpact = "confidence_impact"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        module = c.text(.module) ?? ""
        title = c.text(.title)
        decision = c.text(.decision)?.lowercased()
        reason = c.text(.reason)
        sources = c.list(String.self, .sources)
        confidenceImpact = (try? c.decodeIfPresent(Impact.self, forKey: .confidenceImpact)) ?? nil
    }
}

/// POST /mobile/api/data-health/sync/pos.
struct DataHealthSyncResult: Decodable {
    var ok: Bool = false
    var started: Bool = false
    var alreadySyncing: Bool = false
    var message: String? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, started, message, error
        case alreadySyncing = "already_syncing"
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        ok = c.bool(.ok) ?? false
        started = c.bool(.started) ?? false
        alreadySyncing = c.bool(.alreadySyncing) ?? false
        message = c.text(.message)
        error = c.text(.error)
    }
}

// MARK: - One status line

/// `{line, tone, state, …}` — a sentence the server wrote about one
/// connection or sync, in the restaurant's own clock: the POS row's
/// "Last sync 3:02am · Sales through 9/19/26", Google's "Checked 11:02am ·
/// next check 4pm", Marketing's "Metrics synced 9/21/26". Rendered
/// verbatim — never re-dated on the phone. Nil when there is no line.
struct ServerStatusLine: Codable, Hashable {
    let line: String
    /// ok | warn | bad | off.
    var tone: String? = nil
    var state: String? = nil
    var provider: String? = nil

    enum CodingKeys: String, CodingKey { case line, tone, state, provider }

    init(line: String, tone: String? = nil, state: String? = nil, provider: String? = nil) {
        self.line = line; self.tone = tone; self.state = state; self.provider = provider
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let l = c.text(.line) else {
            throw DecodingError.dataCorruptedError(forKey: .line, in: c, debugDescription: "no line")
        }
        line = l
        tone = c.text(.tone)?.lowercased()
        state = c.text(.state)?.lowercased()
        provider = c.text(.provider)?.lowercased()
    }

    var isWarning: Bool { tone == "warn" || tone == "bad" }
}

/// Decodes an optional `ServerStatusLine` without ever failing the payload
/// around it: a null, a missing line or an odd shape is nil.
extension KeyedDecodingContainer {
    func statusLine(_ key: Key) -> ServerStatusLine? {
        (try? decodeIfPresent(ServerStatusLine.self, forKey: key)) ?? nil
    }
}

/// A `ServerStatusLine` for a type whose Decodable is synthesized: an odd
/// shape reads as no line instead of failing the whole payload.
struct LenientStatusLine: Codable, Hashable {
    let value: ServerStatusLine?

    init(_ value: ServerStatusLine?) { self.value = value }

    init(from decoder: Decoder) throws {
        value = try? ServerStatusLine(from: decoder)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        if let value { try c.encode(value) } else { try c.encodeNil() }
    }
}

/// A Bool for a type whose Decodable is synthesized: anything that isn't
/// a JSON boolean reads as nil rather than failing the payload.
struct LenientFlag: Codable, Hashable {
    let value: Bool?

    init(_ value: Bool?) { self.value = value }

    init(from decoder: Decoder) throws {
        value = try? decoder.singleValueContainer().decode(Bool.self)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        if let value { try c.encode(value) } else { try c.encodeNil() }
    }
}
