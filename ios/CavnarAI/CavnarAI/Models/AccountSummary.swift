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
    let tonePreset: String?
    let openTimesJson: String?
    let closeTimesJson: String?
    let skipHolidays: String?

    enum CodingKeys: String, CodingKey {
        case restaurantName = "restaurant_name"
        case signOffName = "sign_off_name"
        case responseLanguage = "response_language"
        case tonePreset = "tone_preset"
        case openTimesJson = "open_times_json"
        case closeTimesJson = "close_times_json"
        case skipHolidays = "skip_holidays"
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
        case lastLogin = "last_login"
        case passwordStrength = "password_strength"
        case passwordChangedAt = "password_changed_at"
    }
}

struct ConnectionStatus: Decodable {
    let connected: Bool
    let lastSynced: String?

    enum CodingKeys: String, CodingKey {
        case connected
        case lastSynced = "last_synced"
    }
}

struct AccountConnections: Decodable {
    let googleBusiness: ConnectionStatus
    let instagram: ConnectionStatus
    let toast: ConnectionStatus
    let square: ConnectionStatus
    let clover: ConnectionStatus

    enum CodingKeys: String, CodingKey {
        case googleBusiness = "google_business"
        case instagram, toast, square, clover
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
    var alertExtraEmails: String
    var pushSound: Bool

    enum CodingKeys: String, CodingKey {
        case alertHealthBypassQuiet = "alert_health_bypass_quiet"
        case alertFoodWaste = "alert_food_waste"
        case alertAiVisibilityDrop = "alert_ai_visibility_drop"
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
    enum CodingKeys: String, CodingKey {
        case enabled = "auto_approve_5star"
        case dailyCap = "auto_approve_daily_cap"
        case paused = "auto_approve_paused"
        case approvedToday = "auto_approved_today"
    }
}

struct DataSettings: Decodable {
    let retentionMonths: Int
    enum CodingKeys: String, CodingKey { case retentionMonths = "data_retention_months" }
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

    enum CodingKeys: String, CodingKey {
        case id, username, email, role
        case createdAt = "created_at"
        case lastLogin = "last_login"
        case isYou = "is_you"
    }
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
