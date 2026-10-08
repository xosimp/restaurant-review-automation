import Foundation
import Observation

/// GET /mobile/api/intel/website (web_analytics.summary; the Intel-gated
/// twin of /marketing/website, since this card sits under Intel) — the web's
/// "Your website" card: the last 28 days against the 28 before, the daily
/// visits line, where visits came from, clicks out of the site, the top
/// Google searches, and what moved against a typical day of its weekday,
/// each with what else happened that day. Figures are Google's own.
struct WebsiteSummary: Decodable {
    struct Total: Decodable, Identifiable {
        let metric: String
        let label: String
        let value: Double
        let pct: Int?
        var id: String { metric }
    }
    struct Point: Decodable, Identifiable {
        let day: String
        let value: Double?
        var id: String { day }
    }
    struct Share: Decodable, Identifiable {
        let name: String
        let value: Double?
        let share: Int?
        let family: String?
        var id: String { name }
    }
    struct Query: Decodable, Identifiable {
        let query: String
        let clicks: Int
        let impressions: Int?
        let position: Double?
        var id: String { query }
    }
    struct Signal: Decodable, Identifiable {
        let kind: String
        let metric: String?
        let day: String?
        let text: String
        let context: [String]?
        var id: String { "\(metric ?? "")|\(day ?? "")|\(kind)" }
        /// A jump or a rising run reads as up; a dip or a falling run, down.
        var isUp: Bool { kind == "spike" || kind == "trend_up" }
    }

    var ok: Bool = true
    var error: String? = nil
    var connected: Bool = false
    var configured: Bool? = nil
    var available: Bool = false
    var syncedAt: String? = nil
    var totals: [Total] = []
    var line: [Point] = []
    var channels: [Share] = []
    var clicks: [Share] = []
    var queries: [Query] = []
    var queriesThrough: String? = nil
    var signals: [Signal] = []
    var signalsBasis: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, error, connected, configured, available, totals, line, channels, clicks, queries, signals
        case syncedAt = "synced_at"
        case queriesThrough = "queries_through"
        case signalsBasis = "signals_basis"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? true
        error = try? c.decodeIfPresent(String.self, forKey: .error)
        connected = (try? c.decodeIfPresent(Bool.self, forKey: .connected)) ?? false
        configured = try? c.decodeIfPresent(Bool.self, forKey: .configured)
        available = (try? c.decodeIfPresent(Bool.self, forKey: .available)) ?? false
        syncedAt = try? c.decodeIfPresent(String.self, forKey: .syncedAt)
        totals = (try? c.decodeIfPresent([Total].self, forKey: .totals)) ?? []
        line = (try? c.decodeIfPresent([Point].self, forKey: .line)) ?? []
        channels = (try? c.decodeIfPresent([Share].self, forKey: .channels)) ?? []
        clicks = (try? c.decodeIfPresent([Share].self, forKey: .clicks)) ?? []
        queries = (try? c.decodeIfPresent([Query].self, forKey: .queries)) ?? []
        queriesThrough = try? c.decodeIfPresent(String.self, forKey: .queriesThrough)
        signals = (try? c.decodeIfPresent([Signal].self, forKey: .signals)) ?? []
        signalsBasis = try? c.decodeIfPresent(String.self, forKey: .signalsBasis)
    }

    /// "+12% vs the 28 days before", or "last 28 days" with nothing to
    /// compare — never a 0% for a missing period.
    static func changeLine(_ t: Total) -> String {
        guard let pct = t.pct else { return "last 28 days" }
        return "\(pct >= 0 ? "+" : "")\(pct)% vs the 28 days before"
    }

    /// "Book a table · booking" — a click's family, as the web names it.
    static func clickLabel(_ s: Share) -> String {
        switch s.family {
        case "booking_clicks": return "\(s.name) \u{00B7} booking"
        case "ordering_clicks": return "\(s.name) \u{00B7} ordering"
        case "checkout_clicks": return "\(s.name) \u{00B7} checkout"
        default: return s.name
        }
    }

    /// The basis sentence, capitalised and ended — "Each day against the
    /// median of the same weekday…; what else happened that day moved with
    /// it, which is not proof it caused it."
    var basisSentence: String? {
        guard let b = signalsBasis?.trimmingCharacters(in: .whitespaces), let first = b.first else { return nil }
        return first.uppercased() + b.dropFirst() + (b.hasSuffix(".") ? "" : ".")
    }
}

/// GET/POST /mobile/api/web-analytics (client_api._web_analytics_status):
/// the connection itself — Cavnar AI's read-only service account, the GA4
/// property and the Search Console property — and, after a save, what
/// Google let it read.
struct WebsiteConnection: Decodable {
    struct Check: Decodable {
        let ok: Bool?
        let error: String?
    }
    var ok: Bool = true
    var error: String? = nil
    var ownerOnly: Bool? = nil
    var configured: Bool = false
    var serviceEmail: String? = nil
    var ga4PropertyId: String? = nil
    var gscSiteUrl: String? = nil
    var syncedAt: String? = nil
    var canEdit: Bool = false
    /// Read now is this login's to press (the sync route's own gate:
    /// owner or manager). False from an older server.
    var canSync: Bool = false
    var syncing: Bool? = nil
    var checks: [String: Check]? = nil

    enum CodingKeys: String, CodingKey {
        case ok, error, configured, checks, syncing
        case ownerOnly = "owner_only"
        case serviceEmail = "service_email"
        case ga4PropertyId = "ga4_property_id"
        case gscSiteUrl = "gsc_site_url"
        case syncedAt = "synced_at"
        case canEdit = "can_edit"
        case canSync = "can_sync"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? true
        error = try? c.decodeIfPresent(String.self, forKey: .error)
        ownerOnly = try? c.decodeIfPresent(Bool.self, forKey: .ownerOnly)
        configured = (try? c.decodeIfPresent(Bool.self, forKey: .configured)) ?? false
        serviceEmail = try? c.decodeIfPresent(String.self, forKey: .serviceEmail)
        ga4PropertyId = try? c.decodeIfPresent(String.self, forKey: .ga4PropertyId)
        gscSiteUrl = try? c.decodeIfPresent(String.self, forKey: .gscSiteUrl)
        syncedAt = try? c.decodeIfPresent(String.self, forKey: .syncedAt)
        canEdit = (try? c.decodeIfPresent(Bool.self, forKey: .canEdit)) ?? false
        canSync = (try? c.decodeIfPresent(Bool.self, forKey: .canSync)) ?? false
        syncing = try? c.decodeIfPresent(Bool.self, forKey: .syncing)
        checks = try? c.decodeIfPresent([String: Check].self, forKey: .checks)
    }

    var isConnected: Bool { !(ga4PropertyId ?? "").isEmpty || !(gscSiteUrl ?? "").isEmpty }

    /// What a save found, one line per source, a shared refusal said once
    /// (the web's waChecksHtml).
    var checkLines: [(ok: Bool, text: String)] {
        guard let checks else { return [] }
        var out: [(ok: Bool, text: String)] = []
        var said: Set<String> = []
        for (key, name) in [("ga4", "Google Analytics"), ("gsc", "Search Console")] {
            guard let c = checks[key] else { continue }
            if c.ok == true {
                out.append((true, "\(name) can be read. Cavnar AI is reading the last year now \u{2014} this fills in within a few minutes."))
            } else {
                let e = c.error ?? "\(name) couldn\u{2019}t be read yet."
                if said.contains(e) { continue }
                said.insert(e)
                out.append((false, e))
            }
        }
        return out
    }
}

/// POST /mobile/api/web-analytics — exactly the two keys the server reads.
struct WebsiteConnectionBody: Encodable {
    let ga4PropertyId: String
    let gscSiteUrl: String
    enum CodingKeys: String, CodingKey {
        case ga4PropertyId = "ga4_property_id"
        case gscSiteUrl = "gsc_site_url"
    }
}

@Observable
@MainActor
final class WebsiteAnalyticsViewModel {
    private(set) var summary: WebsiteSummary?
    private(set) var connection: WebsiteConnection?
    private(set) var isLoading = false
    private(set) var isSaving = false
    private(set) var isSyncing = false
    var errorMessage: String?
    /// The server's sentence after Read now ("Reading your website
    /// analytics now." / "A read is already running.").
    var syncNote: String?
    private let client: APIClient

    init(client: APIClient = .shared) { self.client = client }

    /// The Intel-gated summary route: the card lives under Intel, and the
    /// Marketing route refused a restaurant without Marketing (re-audit
    /// 10/8/26).
    static let summaryPath = "/mobile/api/intel/website"

    /// Read now is offered only to a login the sync route accepts.
    var canReadNow: Bool { connection?.canSync == true }

    func load() async {
        isLoading = summary == nil
        defer { isLoading = false }
        async let s: WebsiteSummary? = try? client.send(WebsiteAnalyticsViewModel.summaryPath, hapticOnError: false)
        async let c: WebsiteConnection? = try? client.send("/mobile/api/web-analytics", hapticOnError: false)
        let (sum, conn) = await (s, c)
        if let sum { summary = sum }
        if let conn, conn.ok { connection = conn }
    }

    /// Save and check: both fields as typed ("" clears one); the answer
    /// says which parts Google lets Cavnar AI read.
    func save(ga4: String, gsc: String) async -> Bool {
        isSaving = true
        errorMessage = nil
        defer { isSaving = false }
        do {
            let r: WebsiteConnection = try await client.send(
                "/mobile/api/web-analytics", method: .post,
                body: WebsiteConnectionBody(ga4PropertyId: ga4.trimmingCharacters(in: .whitespaces),
                                            gscSiteUrl: gsc.trimmingCharacters(in: .whitespaces)))
            guard r.ok else {
                errorMessage = r.error ?? "Couldn\u{2019}t save that \u{2014} try again."
                return false
            }
            connection = r
            await reloadSummary()
            return true
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t reach Cavnar AI \u{2014} try again."
        }
        return false
    }

    func disconnect() async -> Bool {
        isSaving = true
        errorMessage = nil
        defer { isSaving = false }
        do {
            let r: APIClient.OKResponse = try await client.send("/mobile/api/web-analytics/disconnect", method: .post)
            guard r.ok else {
                errorMessage = r.error ?? "Couldn\u{2019}t disconnect \u{2014} try again."
                return false
            }
            Haptic.success()
            await load()
            return true
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t reach Cavnar AI \u{2014} try again."
        }
        return false
    }

    private struct SyncAnswer: Decodable {
        let ok: Bool
        let error: String?
        let message: String?
    }

    /// Read now — the same read as the 7am job, off the request thread,
    /// once per ten minutes (/web-analytics/sync).
    func readNow() async {
        isSyncing = true
        syncNote = nil
        defer { isSyncing = false }
        do {
            let r: SyncAnswer = try await client.send("/mobile/api/web-analytics/sync", method: .post,
                                                      retryTransient: false)
            if r.ok { Haptic.light() }
            syncNote = r.ok ? (r.message ?? "Reading your website analytics now.")
                            : (r.error ?? "Couldn\u{2019}t start a read \u{2014} try again.")
        } catch let error as APIClient.APIError {
            syncNote = error.message
        } catch {
            syncNote = "Couldn\u{2019}t reach Cavnar AI \u{2014} try again."
        }
    }

    private func reloadSummary() async {
        if let s: WebsiteSummary = try? await client.send(WebsiteAnalyticsViewModel.summaryPath, hapticOnError: false) {
            summary = s
        }
    }
}
