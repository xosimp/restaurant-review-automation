import Foundation

struct AccountProfile: Decodable {
    let restaurantName: String
    let locationName: String?
    let ownerName: String?
    let ownerEmail: String?
    let ownerPhone: String?
    let neighborhood: String?
    let vibe: String?
    let knownFor: String?
    let voiceNotes: String?
    let neverSay: String?
    let menuNotes: String?
    let timezone: String
    let signOffName: String?
    let responseLanguage: String?
    let openTimesJson: String?
    let closeTimesJson: String?
    let skipHolidays: String?
    /// The scheduler's closed dates (schedule_rules.closures, Friction #6) —
    /// what "Hours & closures" edits. `skipHolidays` is the marketing list.
    let closures: [String]?
    /// Set the moment "Close my account" is tapped — an ISO-ish UTC
    /// timestamp string, or nil if no request is on file. Lets the sheet
    /// show "request received" instead of the button again on a later
    /// visit, without a separate round trip.
    let deletionRequestedAt: String?
    /// False for a login the server won't take hours or closures from —
    /// the Hours & closures sheet is then read only. Nil on an older server.
    var hoursCanEdit: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case hoursCanEdit = "hours_can_edit"
        case deletionRequestedAt = "deletion_requested_at"
        case restaurantName = "restaurant_name"
        case signOffName = "sign_off_name"
        case responseLanguage = "response_language"
        case openTimesJson = "open_times_json"
        case closeTimesJson = "close_times_json"
        case skipHolidays = "skip_holidays"
        case closures
        case locationName = "location_name"
        case ownerName = "owner_name"
        case ownerEmail = "owner_email"
        case ownerPhone = "owner_phone"
        case neighborhood, vibe
        case knownFor = "known_for"
        case voiceNotes = "voice_notes"
        case neverSay = "never_say"
        case menuNotes = "menu_notes"
        case timezone
    }
}

struct AccountInfo: Decodable {
    let username: String
    let email: String
    let twoFAEnabled: Bool
    // "email" or "sms", plus the masked destination ("•••-0142" /
    // "ma***@giamia.com") — for the Security sheet's status tile.
    let twoFAMethod: String?
    let twoFAContactMasked: String?
    var loginNotify: Bool
    var marketingEmailsOptOut: Bool
    /// The monthly business review email's switch; nil from an older
    /// server (read as on, the server's default).
    var monthlyReviewEnabled: Bool?
    // users.last_login, "YYYY-MM-DD HH:MM:SS" UTC or nil.
    let lastLogin: String?
    // "weak"/"good"/"strong", scored once at set-time (auth.py can't score
    // a stored hash later) — and when it was last changed. Both nil for an
    // account whose password has never been changed since creation.
    let passwordStrength: String?
    let passwordChangedAt: String?
    let recoveryEmail: String?
    let recoveryEmailPending: String?

    enum CodingKeys: String, CodingKey {
        case username, email
        case recoveryEmail = "recovery_email"
        case recoveryEmailPending = "recovery_email_pending"
        case twoFAEnabled = "two_fa_enabled"
        case twoFAMethod = "two_fa_method"
        case twoFAContactMasked = "two_fa_contact_masked"
        case loginNotify = "login_notify"
        case marketingEmailsOptOut = "marketing_emails_opt_out"
        case monthlyReviewEnabled = "monthly_review_enabled"
        case lastLogin = "last_login"
        case passwordStrength = "password_strength"
        case passwordChangedAt = "password_changed_at"
    }
}

struct ConnectionStatus: Decodable {
    let connected: Bool
    let lastSynced: String?
    /// G3 (pos_health.provider_state): every POS row's one provider-agnostic
    /// reading — "current" | "aging" | "stale" | "error" | "unknown" |
    /// "not_connected" — its age in days, and the last sync error. G9: the
    /// Google row's review `source` ("gbp" | "places_sampled" | "none") and
    /// its `label` ("Google reviews (sampled — Places returns 5 at a time)").
    /// All absent on an older server.
    var syncState: String? = nil
    var ageDays: Double? = nil
    var error: String? = nil
    var source: String? = nil
    var label: String? = nil
    /// DH4: the sentence the server wrote for this row, in the restaurant's
    /// clock — the chosen POS provider's `sync_line` / `sync_tone` ("Last
    /// sync 3:02am · Sales through 9/19/26", tone ok | warn | bad | off),
    /// and Google's `fetch_line` ("Checked 11:02am · next check 4pm").
    /// Absent on an older server, which keeps `syncLine`'s wording.
    var serverSyncLine: String? = nil
    var serverSyncTone: String? = nil
    var fetchLine: ServerStatusLine? = nil

    enum CodingKeys: String, CodingKey {
        case connected, error, source, label
        case lastSynced = "last_synced"
        case syncState = "sync_state"
        case ageDays = "age_days"
        case serverSyncLine = "sync_line"
        case serverSyncTone = "sync_tone"
        case fetchLine = "fetch_line"
    }

    init(connected: Bool, lastSynced: String? = nil, syncState: String? = nil, ageDays: Double? = nil,
         error: String? = nil, source: String? = nil, label: String? = nil) {
        self.connected = connected; self.lastSynced = lastSynced; self.syncState = syncState
        self.ageDays = ageDays; self.error = error; self.source = source; self.label = label
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        connected = (try? c.decodeIfPresent(Bool.self, forKey: .connected)) ?? false
        lastSynced = try? c.decodeIfPresent(String.self, forKey: .lastSynced)
        syncState = (try? c.decodeIfPresent(String.self, forKey: .syncState))?.lowercased()
        ageDays = try? c.decodeIfPresent(Double.self, forKey: .ageDays)
        let e = (try? c.decodeIfPresent(String.self, forKey: .error)) ?? nil
        error = (e?.trimmingCharacters(in: .whitespaces).isEmpty ?? true) ? nil : e
        source = try? c.decodeIfPresent(String.self, forKey: .source)
        let l = (try? c.decodeIfPresent(String.self, forKey: .label)) ?? nil
        label = (l?.trimmingCharacters(in: .whitespaces).isEmpty ?? true) ? nil : l
        let sl = ((try? c.decodeIfPresent(String.self, forKey: .serverSyncLine)) ?? nil)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        serverSyncLine = (sl?.isEmpty ?? true) ? nil : sl
        serverSyncTone = ((try? c.decodeIfPresent(String.self, forKey: .serverSyncTone)) ?? nil)?.lowercased()
        fetchLine = c.statusLine(.fetchLine)
    }

    /// The line a POS row prints: the server's own sentence for this
    /// provider when it sent one (its `sync_line`, else the connections'
    /// `pos_line` when that names this provider), coloured by its tone;
    /// otherwise the older state-derived wording. Nil when there is none.
    func posStatusLine(provider: String, posLine: ServerStatusLine?) -> (text: String, tone: SyncTone)? {
        if let text = serverSyncLine {
            return (text, Self.tone(serverSyncTone))
        }
        if let p = posLine, p.provider == provider {
            return (p.line, Self.tone(p.tone))
        }
        return syncLine
    }

    /// The server's tone words onto the row's tones: ok good, warn warn,
    /// bad bad, off (or nothing) neutral.
    static func tone(_ raw: String?) -> SyncTone {
        switch raw {
        case "ok": return .good
        case "warn": return .warn
        case "bad": return .bad
        default: return .neutral
        }
    }

    /// "Last synced 9/21/26" — M/D/YY, never the raw stamp.
    var lastSyncedText: String? {
        guard let raw = lastSynced?.trimmingCharacters(in: .whitespaces), !raw.isEmpty else { return nil }
        return "Last synced " + CavnarDate.mdyLocal(raw)
    }

    enum SyncTone: Equatable { case good, warn, bad, neutral }

    /// What the sync state says, in words, and its tone: current is good,
    /// aging and stale are a warning (a sync that simply stopped), an error
    /// is bad; unknown says so. Nil when the server sent no state or the row
    /// isn't connected.
    var syncLine: (text: String, tone: SyncTone)? {
        guard let s = syncState, s != "not_connected" else { return nil }
        let days: String? = ageDays.map { d in
            let n = Int(d.rounded(.down))
            return n <= 0 ? "today" : "\(n) day\(n == 1 ? "" : "s") ago"
        }
        switch s {
        case "current": return ("Syncing \u{00B7} last data " + (days ?? "recently"), .good)
        case "aging": return ("Last sync " + (days ?? "a while ago") + " \u{2014} running late", .warn)
        case "stale": return ("No sync since " + (days ?? "a while") + " \u{2014} figures from this POS are out of date", .warn)
        case "error": return ("Sync failing" + (error.map { ": \($0)" } ?? ""), .bad)
        case "unknown": return ("Sync state unknown", .neutral)
        case "disconnected": return ("Disconnected", .warn)
        default: return nil
        }
    }
}

struct AccountConnections: Decodable {
    let googleBusiness: ConnectionStatus
    let instagram: ConnectionStatus
    let toast: ConnectionStatus
    let square: ConnectionStatus
    let clover: ConnectionStatus
    /// G3: RPOWER (connected by Cavnar, not from the phone) and the one POS
    /// reading the rest of the product uses (pos_health.pos_sync_state).
    /// Absent on an older server.
    var rpower: ConnectionStatus? = nil
    var pos: POSSyncState? = nil
    /// DH4: the POS row's sentence from the registry, in the restaurant's
    /// clock (`pos_line`: {line, tone, state, provider, …}). Lenient; absent
    /// on an older server.
    var posLineField: LenientStatusLine? = nil

    var posLine: ServerStatusLine? { posLineField?.value }

    enum CodingKeys: String, CodingKey {
        case googleBusiness = "google_business"
        case instagram, toast, square, clover, rpower, pos
        case posLineField = "pos_line"
    }

    /// `{provider, connected, last_synced, age_days, error, state}`.
    struct POSSyncState: Decodable {
        var provider: String?
        var connected: Bool?
        var state: String?
        var ageDays: Double?
        var error: String?
        enum CodingKeys: String, CodingKey {
            case provider, connected, state, error
            case ageDays = "age_days"
        }
        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            provider = try? c.decodeIfPresent(String.self, forKey: .provider)
            connected = try? c.decodeIfPresent(Bool.self, forKey: .connected)
            state = try? c.decodeIfPresent(String.self, forKey: .state)
            ageDays = try? c.decodeIfPresent(Double.self, forKey: .ageDays)
            error = try? c.decodeIfPresent(String.self, forKey: .error)
        }
    }
}

struct AlertContact: Codable, Identifiable {
    let id: Int
    var name: String
    var phone: String
    let smsConsent: Bool

    enum CodingKeys: String, CodingKey {
        case id, name, phone
        case smsConsent = "sms_consent"
    }
}

struct AlertSettings: Codable {
    var alert1star: Bool
    var alert2star: Bool
    var alertHealth: Bool
    var alertNegSpike: Bool
    var alertNegativeTrend: Bool
    var alertNoResponse: Bool
    var alert5star: Bool
    var alertLaborOver: Bool
    var urgentViaSms: Bool
    var urgentViaEmail: Bool
    var digestEnabled: Bool
    var digestDay: String
    // "HH:MM" 24h strings, or nil when quiet hours are off — backend has
    // supported this since notify.py's own is_in_quiet_hours(), but
    // neither client ever exposed a way to actually set it.
    var alertQuietStart: String?
    var alertQuietEnd: String?
    // Per-category push — independent of urgentViaSms/urgentViaEmail,
    // which only gate SMS/email (push has no per-owner cost, so there's
    // no matching global kill switch; see notify.py's blast()).
    var al1starPush: Bool
    var al2starPush: Bool
    var al5starPush: Bool
    var alHealthPush: Bool
    var alSpikePush: Bool
    var alUnresPush: Bool
    // Settings audit additions
    var alertHealthBypassQuiet: Bool
    var alertFoodWaste: Bool
    var alertAiVisibilityDrop: Bool
    // Weekly: a tracked competitor's rating moved, or a new one appeared.
    var alertCompetitorMove: Bool
    var alertExtraEmails: String
    var pushSound: Bool

    enum CodingKeys: String, CodingKey {
        case alertHealthBypassQuiet = "alert_health_bypass_quiet"
        case alertFoodWaste = "alert_food_waste"
        case alertAiVisibilityDrop = "alert_ai_visibility_drop"
        case alertCompetitorMove = "alert_competitor_move"
        case alertExtraEmails = "alert_extra_emails"
        case pushSound = "push_sound"
        case alert1star = "alert_1star"
        case alert2star = "alert_2star"
        case alertHealth = "alert_health"
        case alertNegSpike = "alert_neg_spike"
        case alertNegativeTrend = "alert_negative_trend"
        case alertNoResponse = "alert_no_response"
        case alert5star = "alert_5star"
        case alertLaborOver = "alert_labor_over"
        case urgentViaSms = "urgent_via_sms"
        case urgentViaEmail = "urgent_via_email"
        case digestEnabled = "digest_enabled"
        case digestDay = "digest_day"
        case alertQuietStart = "alert_quiet_start"
        case alertQuietEnd = "alert_quiet_end"
        case al1starPush = "al_1star_push"
        case al2starPush = "al_2star_push"
        case al5starPush = "al_5star_push"
        case alHealthPush = "al_health_push"
        case alSpikePush = "al_spike_push"
        case alUnresPush = "al_unres_push"
    }
}

struct AccountAlerts: Decodable {
    let contacts: [AlertContact]
    let settings: AlertSettings
}

struct AutoApproveSettings: Decodable {
    let enabled: Bool
    let dailyCap: Int
    let paused: Bool
    let approvedToday: Int
    /// Extend the rule to 3★/4★ once the owner's own edit rate has earned
    /// it — measured server-side, never lower than 3★.
    let earned: Bool?
    enum CodingKeys: String, CodingKey {
        case earned = "auto_approve_earned"
        case enabled = "auto_approve_5star"
        case dailyCap = "auto_approve_daily_cap"
        case paused = "auto_approve_paused"
        case approvedToday = "auto_approved_today"
    }
}

struct DataSettings: Decodable {
    let retentionMonths: Int
    /// The Export my data scopes this login may pick (mobile_api
    /// .export_scopes_for): labor and food cost only when those modules are
    /// on and this login may read them. Nil from an older server.
    let exportScopes: [String]?
    enum CodingKeys: String, CodingKey {
        case retentionMonths = "data_retention_months"
        case exportScopes = "export_scopes"
    }
}

struct AccountSummary: Decodable {
    let ok: Bool
    let profile: AccountProfile
    var account: AccountInfo
    let connections: AccountConnections
    let alerts: AccountAlerts
    let reviews: AutoApproveSettings
    let data: DataSettings
}

struct TrustedDevice: Decodable, Identifiable {
    let id: Int
    let label: String?
    let createdAt: String
    let lastUsedAt: String?
    let expiresAt: String
    enum CodingKeys: String, CodingKey {
        case id, label
        case createdAt = "created_at"
        case lastUsedAt = "last_used_at"
        case expiresAt = "expires_at"
    }
}

struct AccountActivityEvent: Decodable, Identifiable {
    let type: String
    let label: String
    let detail: String?
    let actor: String?
    let createdAt: String
    var id: String { createdAt + type + (detail ?? "") }
    enum CodingKeys: String, CodingKey {
        case type, label, detail, actor
        case createdAt = "created_at"
    }
}

struct AccountSession: Decodable, Identifiable {
    let tokenHint: String
    let isCurrent: Bool
    let createdAt: String
    let lastActive: String
    let ipAddress: String
    let deviceType: String
    let label: String

    var id: String { tokenHint }

    enum CodingKeys: String, CodingKey {
        case tokenHint = "token_hint"
        case isCurrent = "is_current"
        case createdAt = "created_at"
        case lastActive = "last_active"
        case ipAddress = "ip_address"
        case deviceType = "device_type"
        case label
    }
}

// Distinct from AccountSession above: that's only currently-live sessions,
// this is every past login — see auth.get_login_history()'s doc comment.
struct LoginHistoryEntry: Decodable, Identifiable {
    let event: String
    let ipAddress: String?
    let userAgent: String?
    let deviceType: String?
    let createdAt: String
    let label: String

    var id: String { createdAt + (ipAddress ?? "") }

    enum CodingKeys: String, CodingKey {
        case event
        case ipAddress = "ip_address"
        case userAgent = "user_agent"
        case deviceType = "device_type"
        case createdAt = "created_at"
        case label
    }
}

struct TeamMember: Decodable, Identifiable {
    let id: Int
    let username: String
    let email: String
    let role: String
    let createdAt: String
    let lastLogin: String?
    let isYou: Bool
    /// Owner-granted extras beyond the role (permissions.GRANTABLE keys).
    var access: [String]?
    /// Whether this login's role can take grants (managers and teammates).
    let accessGrantable: Bool?
    var morningBrief: Bool?
    /// What the owner sees this login called ("Co-owner", "Manager", …).
    let serverRoleLabel: String?
    /// Whether an owner may change this login's role here.
    let roleEditable: Bool?

    enum CodingKeys: String, CodingKey {
        case id, username, email, role, access
        case createdAt = "created_at"
        case lastLogin = "last_login"
        case isYou = "is_you"
        case accessGrantable = "access_grantable"
        case morningBrief = "morning_brief"
        case serverRoleLabel = "role_label"
        case roleEditable = "role_editable"
    }

    var roleLabel: String {
        if let serverRoleLabel { return serverRoleLabel }
        switch role {
        case "manager": return "Manager"
        case "member": return "Teammate"
        default: return "Owner"
        }
    }

    var isOwnerRole: Bool { role == "client" || role == "owner" }
}

/// A role an owner can give a login: key is what's stored ("client" is a
/// co-owner), label is what's shown.
struct TeamRoleOption: Decodable, Identifiable, Hashable {
    let key: String
    let label: String
    var id: String { key }
}

/// One thing an owner can open to a manager (Account → Team).
struct TeamAccessOption: Decodable, Identifiable {
    let key: String
    let label: String
    var id: String { key }
}

struct BillingInvoice: Decodable, Identifiable {
    let date: String
    let amount: String
    let status: String
    let pdfURL: String?

    var id: String { date + amount }

    enum CodingKeys: String, CodingKey {
        case date, amount, status
        case pdfURL = "pdf_url"
    }
}

struct BillingSummary: Decodable {
    let ok: Bool
    let reason: String?
    let status: String?
    let nextDate: String?
    let amount: String?
    let paymentMethod: String?
    let portalURL: String?
    let message: String?
    let invoices: [BillingInvoice]?

    enum CodingKeys: String, CodingKey {
        case ok, reason, status, message, invoices
        case nextDate = "next_date"
        case amount
        case paymentMethod = "payment_method"
        case portalURL = "portal_url"
    }
}
