import Foundation

// The Campaigns half of Marketing on the phone (iOS parity round, 10/7/26):
// the guest overview, the Opportunity Feed, the Campaign Studio's drafts and
// sends, the newsletter history and the review-link invite switch. Every
// payload here is additive on the server, so every type decodes leniently —
// a missing field is nil (shown as "—", never 0) and never fails a screen.

extension KeyedDecodingContainer {
    /// Text, trimmed; nil when absent or empty. A number is read as text.
    func mktText(_ key: Key) -> String? {
        if let s = (try? decodeIfPresent(String.self, forKey: key)) ?? nil {
            let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
            return t.isEmpty ? nil : t
        }
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return String(i) }
        return nil
    }

    func mktInt(_ key: Key) -> Int? {
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return i }
        if let d = (try? decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite { return Int(d.rounded()) }
        if let s = (try? decodeIfPresent(String.self, forKey: key)) ?? nil { return Int(s) }
        return nil
    }

    func mktDouble(_ key: Key) -> Double? {
        if let d = (try? decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite { return d }
        if let s = (try? decodeIfPresent(String.self, forKey: key)) ?? nil { return Double(s) }
        return nil
    }

    func mktBool(_ key: Key) -> Bool? {
        if let b = (try? decodeIfPresent(Bool.self, forKey: key)) ?? nil { return b }
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return i != 0 }
        return nil
    }

    func mktStrings(_ key: Key) -> [String] {
        ((try? decodeIfPresent([String].self, forKey: key)) ?? nil) ?? []
    }
}

/// "1 guest" / "3 guests".
func mktPlural(_ n: Int, _ one: String, _ many: String? = nil) -> String {
    "\(n) \(n == 1 ? one : (many ?? one + "s"))"
}

// MARK: - Guest overview (GET /mobile/api/guest-overview)

/// A measured rate over at least `rate_min` campaigns, or absent.
struct CampaignRate: Decodable, Hashable {
    let pct: Double
    let campaigns: Int

    init(pct: Double, campaigns: Int) { self.pct = pct; self.campaigns = campaigns }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let p = c.mktDouble(.pct) else {
            throw DecodingError.dataCorruptedError(forKey: .pct, in: c, debugDescription: "no pct")
        }
        pct = p
        campaigns = c.mktInt(.campaigns) ?? 0
    }

    enum CodingKeys: String, CodingKey { case pct, campaigns }

    /// "12.5%" — one decimal only when it has one.
    var label: String {
        pct == pct.rounded() ? "\(Int(pct))%" : String(format: "%.1f%%", pct)
    }
}

/// The Campaigns page's figures (guest_marketing.campaign_overview), every
/// one measured; nil where the server could not say.
struct GuestOverview: Decodable {
    struct Week: Decodable, Identifiable, Hashable {
        let weekStart: String
        let joined: Int
        var id: String { weekStart }

        init(weekStart: String, joined: Int) { self.weekStart = weekStart; self.joined = joined }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            weekStart = c.mktText(.weekStart) ?? ""
            joined = c.mktInt(.joined) ?? 0
        }
        enum CodingKeys: String, CodingKey { case joined; case weekStart = "week_start" }

        /// "9/14" — the axis label (the year is in the chart's caption).
        var shortLabel: String {
            let full = CavnarDate.mdy(weekStart)
            guard let cut = full.lastIndex(of: "/") else { return full }
            return String(full[..<cut])
        }
    }

    struct LastCampaign: Decodable {
        let date: String?
        let sent: Int
        let segmentLabel: String?
        let channel: String?
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            date = c.mktText(.date)
            sent = c.mktInt(.sent) ?? 0
            segmentLabel = c.mktText(.segmentLabel)
            channel = c.mktText(.channel)
        }
        enum CodingKeys: String, CodingKey { case date, sent, channel; case segmentLabel = "segment_label" }
    }

    /// What the Studio's counter and phone preview need.
    struct SMS: Decodable, Hashable {
        var prefix: String = ""
        var linkChars: Int = 41
        var linkExample: String = ""
        var stopChars: Int = 28
        var max: Int = 300
        var sender: String = ""

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            prefix = ((try? c.decodeIfPresent(String.self, forKey: .prefix)) ?? nil) ?? ""
            linkChars = c.mktInt(.linkChars) ?? 41
            linkExample = c.mktText(.linkExample) ?? ""
            stopChars = c.mktInt(.stopChars) ?? 28
            max = c.mktInt(.max) ?? 300
            sender = c.mktText(.sender) ?? ""
        }
        enum CodingKeys: String, CodingKey {
            case prefix, max, sender
            case linkChars = "link_chars"
            case linkExample = "link_example"
            case stopChars = "stop_chars"
        }
    }

    struct Insight: Decodable, Identifiable, Hashable {
        let figure: String
        let text: String
        let basis: String?
        let tone: String?
        var id: String { text }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            figure = c.mktText(.figure) ?? "\u{2014}"
            text = c.mktText(.text) ?? ""
            basis = c.mktText(.basis)
            tone = c.mktText(.tone)
        }
        enum CodingKeys: String, CodingKey { case figure, text, basis, tone }
    }

    var subscribers: Int?
    var today: Int?
    var last30: Int?
    var weekly: [Week] = []
    var lastCampaign: LastCampaign?
    var tapRate: CampaignRate?
    var backRate: CampaignRate?
    var tapBySegment: [String: CampaignRate] = [:]
    var backBySegment: [String: CampaignRate] = [:]
    var accepted: Double?
    var textsThisMonth: Int?
    var textsFailedThisMonth: Int?
    var sms = SMS()
    /// False outside the sending window — a text then waits for its open.
    var sendingNow: Bool?
    /// "8:00 AM and 9:00 PM" (guest_sms_window_label) — the one source of
    /// the window on the phone; nothing here hard-codes the hours.
    var window: String?
    var minDaysBetween: Int?
    var rateMin: Int?
    var emailSubscribers: Int?
    var mailingAddressSet = false
    var insights: [Insight] = []
    var joinURL: String?
    var receiptHint: String?

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        subscribers = c.mktInt(.subscribers)
        today = c.mktInt(.today)
        last30 = c.mktInt(.last30)
        weekly = ((try? c.decodeIfPresent([Week].self, forKey: .weekly)) ?? nil) ?? []
        lastCampaign = (try? c.decodeIfPresent(LastCampaign.self, forKey: .lastCampaign)) ?? nil
        tapRate = (try? c.decodeIfPresent(CampaignRate.self, forKey: .tapRate)) ?? nil
        backRate = (try? c.decodeIfPresent(CampaignRate.self, forKey: .backRate)) ?? nil
        tapBySegment = ((try? c.decodeIfPresent([String: CampaignRate].self, forKey: .tapBySegment)) ?? nil) ?? [:]
        backBySegment = ((try? c.decodeIfPresent([String: CampaignRate].self, forKey: .backBySegment)) ?? nil) ?? [:]
        accepted = c.mktDouble(.accepted)
        textsThisMonth = c.mktInt(.textsThisMonth)
        textsFailedThisMonth = c.mktInt(.textsFailedThisMonth)
        sms = ((try? c.decodeIfPresent(SMS.self, forKey: .sms)) ?? nil) ?? SMS()
        sendingNow = c.mktBool(.sendingNow)
        window = c.mktText(.window)
        minDaysBetween = c.mktInt(.minDaysBetween)
        rateMin = c.mktInt(.rateMin)
        emailSubscribers = c.mktInt(.emailSubscribers)
        mailingAddressSet = c.mktBool(.mailingAddressSet) ?? false
        insights = ((try? c.decodeIfPresent([Insight].self, forKey: .insights)) ?? nil) ?? []
        joinURL = c.mktText(.joinURL)
        receiptHint = c.mktText(.receiptHint)
    }

    enum CodingKeys: String, CodingKey {
        case subscribers, today, weekly, accepted, sms, window, insights
        case last30 = "last_30"
        case lastCampaign = "last_campaign"
        case tapRate = "tap_rate"
        case backRate = "back_rate"
        case tapBySegment = "tap_by_segment"
        case backBySegment = "back_by_segment"
        case textsThisMonth = "texts_this_month"
        case textsFailedThisMonth = "texts_failed_this_month"
        case sendingNow = "sending_now"
        case minDaysBetween = "min_days_between"
        case rateMin = "rate_min"
        case emailSubscribers = "email_subscribers"
        case mailingAddressSet = "mailing_address_set"
        case joinURL = "join_url"
        case receiptHint = "receipt_hint"
    }
}

/// The sending window as the server words it ("8:00 AM and 9:00 PM").
enum SMSWindow {
    /// When texts start going again — "8:00 AM" out of the label; nil
    /// without one (an older server), and the caller then says no time.
    static func opens(_ label: String?) -> String? {
        guard let label, let first = label.components(separatedBy: " and ").first,
              !first.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        return first.trimmingCharacters(in: .whitespaces)
    }

    /// "8:00 AM – 9:00 PM".
    static func range(_ label: String?) -> String? {
        guard let label, !label.isEmpty else { return nil }
        return label.replacingOccurrences(of: " and ", with: " \u{2013} ")
    }

    /// "between 8:00 AM and 9:00 PM", or the plain rule when unknown.
    static func sentence(_ label: String?) -> String {
        guard let label, !label.isEmpty else { return "during your sending hours" }
        return "between \(label)"
    }
}

// MARK: - The text's counter (the web's cpPaint, CS-12 / CS-14)

/// What a guest reads is "{Name}: " + the words + the tracked link, and all
/// of it counts against the limit; the STOP line counts toward the parts.
/// A character outside the GSM alphabet sends the whole text as Unicode: 70
/// characters a part, not 160. Counted in UTF-16 units, as the carrier and
/// the web counter count.
struct SMSMeter: Equatable {
    let head: String
    let counted: Int
    let max: Int
    let parts: Int
    let unicode: Bool
    var tooLong: Bool { counted > max }
    var over: Int { Swift.max(0, counted - max) }

    private static let gsm: Set<Character> = Set(
        "\n\r !\"#$%&'()*+,-./0123456789:;>=<?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_abcdefghijklmnopqrstuvwxyz{|}~"
        + "£¥èéùìòÇØøÅåÄÖÑÜäöñüàÉ¡¿§ΔΦΓΛΩΠΨΣΘΞÆæß€¤")

    static func measure(message: String, hasLink: Bool, sms: GuestOverview.SMS) -> SMSMeter {
        let prefix = sms.prefix
        let normalized = message.replacingOccurrences(of: "\u{2018}", with: "'")
            .replacingOccurrences(of: "\u{2019}", with: "'").lowercased()
        let bare = prefix.count >= 2 ? String(prefix.dropLast(2)).lowercased() : prefix.lowercased()
        let head = (!prefix.isEmpty && !normalized.hasPrefix(bare)) ? prefix : ""
        let linkN = hasLink ? sms.linkChars : 0
        let counted = head.utf16.count + message.utf16.count + linkN
        let full = counted + sms.stopChars
        let unicode = (head + message).contains { !gsm.contains($0) }
        let one = unicode ? 70 : 160, per = unicode ? 67 : 153
        let parts = full <= one ? 1 : Int((Double(full) / Double(per)).rounded(.up))
        return SMSMeter(head: head, counted: counted, max: sms.max, parts: parts, unicode: unicode)
    }
}

// MARK: - Campaign sends

/// POST /guest-campaign/send's answer and its refusals (a 400 carries the
/// same shape: `blocked` names the gate, `reasons` what Cavnar AI flagged).
struct CampaignSendResult: Decodable, Equatable {
    var ok = false
    var queued = false
    var campaignId: Int?
    var total: Int?
    var sent: Int?
    var skippedRecent: Int?
    var waiting = false
    var waitingUntil: String?
    var segmentLabel: String?
    var blocked: String?
    var reasons: [String] = []
    var error: String?
    var cancelled: Int?

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.mktBool(.ok) ?? false
        queued = c.mktBool(.queued) ?? false
        campaignId = c.mktInt(.campaignId)
        total = c.mktInt(.total)
        sent = c.mktInt(.sent)
        skippedRecent = c.mktInt(.skippedRecent)
        waiting = c.mktBool(.waiting) ?? false
        waitingUntil = c.mktText(.waitingUntil)
        segmentLabel = c.mktText(.segmentLabel)
        blocked = c.mktText(.blocked)
        reasons = c.mktStrings(.reasons)
        error = c.mktText(.error)
        cancelled = c.mktInt(.cancelled)
    }

    enum CodingKeys: String, CodingKey {
        case ok, queued, total, sent, waiting, blocked, reasons, error, cancelled
        case campaignId = "campaign_id"
        case skippedRecent = "skipped_recent"
        case waitingUntil = "waiting_until"
        case segmentLabel = "segment_label"
    }

    /// The send gate held it back: what Cavnar AI flagged, to show and fix.
    var isGateFlag: Bool { blocked == "gate_flagged" }
    var isQuietHours: Bool { blocked == "quiet_hours" }

    /// "Texting 31 guests · 4 left out, texted in the last few days" or
    /// "31 texts wait until 8:00 AM, then go" — queued, never "delivered".
    var acceptedLine: String {
        let n = total ?? sent ?? 0
        var s = waiting
            ? "\(mktPlural(n, "text")) wait until \(waitingUntil ?? "the window opens"), then go"
            : "Texting \(mktPlural(n, "guest"))"
        if let skipped = skippedRecent, skipped > 0 {
            s += " \u{00B7} \(skipped) left out, texted in the last few days"
        }
        return s + " \u{00B7} Campaigns sent shows any that fail"
    }
}

/// What the send gate said about a text or an email (ai_reviewer.gate_send):
/// the reasons Cavnar AI flagged, and the owner's choice — edit it or drop it.
struct SendGateFlag: Identifiable, Equatable {
    let id = UUID()
    /// "text" or "email".
    let channel: String
    let message: String
    let reasons: [String]

    static func == (a: SendGateFlag, b: SendGateFlag) -> Bool { a.id == b.id }
}

// MARK: - Newsletter

/// The Campaign Studio's look for an email (guest_email.clean_design).
struct NewsletterDesign: Codable, Equatable {
    var headline = ""
    var preheader = ""
    var buttonLabel = ""
    var buttonUrl = ""
    var imageMediaId: Int?

    init(headline: String = "", preheader: String = "", buttonLabel: String = "", buttonUrl: String = "",
         imageMediaId: Int? = nil) {
        self.headline = headline; self.preheader = preheader; self.buttonLabel = buttonLabel
        self.buttonUrl = buttonUrl; self.imageMediaId = imageMediaId
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        headline = c.mktText(.headline) ?? ""
        preheader = c.mktText(.preheader) ?? ""
        buttonLabel = c.mktText(.buttonLabel) ?? ""
        buttonUrl = c.mktText(.buttonUrl) ?? ""
        imageMediaId = c.mktInt(.imageMediaId)
    }

    /// Every key the server reads, an absent photo as null.
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(headline, forKey: .headline)
        try c.encode(preheader, forKey: .preheader)
        try c.encode(buttonLabel, forKey: .buttonLabel)
        try c.encode(buttonUrl, forKey: .buttonUrl)
        try c.encode(imageMediaId, forKey: .imageMediaId)
    }

    enum CodingKeys: String, CodingKey {
        case headline, preheader
        case buttonLabel = "button_label"
        case buttonUrl = "button_url"
        case imageMediaId = "image_media_id"
    }
}

/// POST /guest-newsletter's answer — sent, failed (and how many a retry can
/// reach), skipped and queued each by name, never the total as sent (CS-3);
/// and its refusals: the mailing address the law needs, the same email
/// already sent today (409), what the send gate flagged.
struct NewsletterSendResult: Decodable, Equatable {
    var ok = false
    var newsletterId: Int?
    var sent = 0
    var failed = 0
    var retryable = 0
    var skipped = 0
    var total: Int?
    var queued = 0
    var resumed = false
    var added: Int?
    var alreadySent = false
    var sentOn: String?
    var newSubscribers = 0
    var needsMailingAddress = false
    var ownerOnly = false
    var gateFlagged = false
    var notConfigured = false
    var reasons: [String] = []
    var error: String?

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.mktBool(.ok) ?? false
        newsletterId = c.mktInt(.newsletterId)
        sent = c.mktInt(.sent) ?? 0
        failed = c.mktInt(.failed) ?? 0
        retryable = c.mktInt(.retryable) ?? 0
        skipped = c.mktInt(.skipped) ?? 0
        total = c.mktInt(.total)
        queued = c.mktInt(.queued) ?? 0
        resumed = c.mktBool(.resumed) ?? false
        added = c.mktInt(.added)
        alreadySent = c.mktBool(.alreadySent) ?? false
        sentOn = c.mktText(.sentOn)
        newSubscribers = c.mktInt(.newSubscribers) ?? 0
        needsMailingAddress = c.mktBool(.needsMailingAddress) ?? false
        ownerOnly = c.mktBool(.ownerOnly) ?? false
        gateFlagged = c.mktBool(.gateFlagged) ?? false
        notConfigured = c.mktBool(.notConfigured) ?? false
        reasons = c.mktStrings(.reasons)
        error = c.mktText(.error)
    }

    enum CodingKeys: String, CodingKey {
        case ok, sent, failed, retryable, skipped, total, queued, resumed, added, reasons, error
        case newsletterId = "newsletter_id"
        case alreadySent = "already_sent"
        case sentOn = "sent_on"
        case newSubscribers = "new_subscribers"
        case needsMailingAddress = "needs_mailing_address"
        case ownerOnly = "owner_only"
        case gateFlagged = "gate_flagged"
        case notConfigured = "not_configured"
    }

    /// "Emailed 18 guests · 2 failed · 1 skipped (unsubscribed or
    /// suppressed)" — the web's cpMailResult, word for word.
    var summary: String {
        var bits: [String] = []
        if alreadySent {
            bits.append("Email: " + (error ?? "already sent today"))
        } else {
            if sent > 0 || queued == 0 {
                bits.append((resumed ? "Resumed \u{00B7} emailed " : "Emailed ") + mktPlural(sent, "guest"))
            }
            if queued > 0 {
                bits.append("\(queued) " + (sent > 0 ? "more " : "") + "queued, going out in the background")
            }
            if let added, added == 0, queued == 0 { bits.append("nobody new to send to") }
        }
        if failed > 0 { bits.append("\(failed) failed") }
        if skipped > 0 { bits.append("\(skipped) skipped (unsubscribed or suppressed)") }
        return bits.joined(separator: " \u{00B7} ")
    }

    /// Clean when nothing failed and it was not refused.
    var isClean: Bool { ok && failed == 0 }

    /// "Retry 2 failed", when a retry can reach some.
    var retryCount: Int? { retryable > 0 && newsletterId != nil ? retryable : nil }

    /// "Send to the 3 new subscribers", on an email already sent today.
    var newCount: Int? { alreadySent && newSubscribers > 0 && newsletterId != nil ? newSubscribers : nil }
}

/// One newsletter in "Campaigns sent" (guest_email.newsletter_history):
/// opens and clicks as RECORDED — Apple Mail's auto-opens push opens up, so
/// never "at least" — and nil where nothing measured them.
struct GuestNewsletter: Decodable, Identifiable, Equatable {
    let id: Int
    var subject: String = ""
    var body: String = ""
    var design = NewsletterDesign()
    var segment: String?
    var segmentLabel: String?
    var total: Int?
    var createdAt: String?
    var sent = 0
    var failed = 0
    var retryable = 0
    var skipped = 0
    var pending = 0
    var opened: Int?
    var clicked: Int?
    var imageURL: String?
    var resultsAsOf: String?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let i = c.mktInt(.id) else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "no id")
        }
        id = i
        subject = c.mktText(.subject) ?? ""
        body = c.mktText(.body) ?? ""
        design = ((try? c.decodeIfPresent(NewsletterDesign.self, forKey: .design)) ?? nil) ?? NewsletterDesign()
        segment = c.mktText(.segment)
        segmentLabel = c.mktText(.segmentLabel)
        total = c.mktInt(.total)
        createdAt = c.mktText(.createdAt)
        sent = c.mktInt(.sent) ?? 0
        failed = c.mktInt(.failed) ?? 0
        retryable = c.mktInt(.retryable) ?? 0
        skipped = c.mktInt(.skipped) ?? 0
        pending = c.mktInt(.pending) ?? 0
        opened = c.mktInt(.opened)
        clicked = c.mktInt(.clicked)
        imageURL = c.mktText(.imageURL)
        resultsAsOf = c.mktText(.resultsAsOf)
    }

    enum CodingKeys: String, CodingKey {
        case id, subject, body, design, segment, total, sent, failed, retryable, skipped, pending, opened, clicked
        case segmentLabel = "segment_label"
        case createdAt = "created_at"
        case imageURL = "image_url"
        case resultsAsOf = "results_as_of"
    }

    /// M/D/YY on the restaurant's day — never the stored ISO stamp.
    var whenLabel: String { CampaignDates.label(createdAt) }
}

/// Dates on the history rows. created_at is a UTC stamp; the day it went
/// out is the restaurant's day. Anything unreadable is "—", never the raw
/// ISO text (DESIGN_SYSTEM → Dates and times).
enum CampaignDates {
    static func label(_ stamp: String?) -> String {
        guard let stamp, !stamp.isEmpty else { return "\u{2014}" }
        if let date = CavnarDate.timestamp(stamp) {
            return CavnarDate.mdy(date, in: RestaurantClock.timeZone)
        }
        let day = CavnarDate.mdy(stamp)
        return day == stamp ? "\u{2014}" : day
    }
}

// MARK: - Studio drafts

/// POST /guest-campaign/draft (the text): the message, the model draft it
/// is measured against, and — when the owner's goal was planned — the
/// audience, tone and weekday Cavnar AI picked.
struct StudioTextDraft: Decodable {
    var ok = false
    var message: String?
    var error: String?
    var draftRef: Int?
    var type: String?
    var segment: String?
    var goal: String?
    var targetDay: String?
    var returnsBySegment: [String: SegmentReturn]?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.mktBool(.ok) ?? false
        message = c.mktText(.message)
        error = c.mktText(.error)
        draftRef = c.mktInt(.draftRef)
        type = c.mktText(.type)
        segment = c.mktText(.segment)
        goal = c.mktText(.goal)
        targetDay = c.mktText(.targetDay)
        returnsBySegment = (try? c.decodeIfPresent([String: SegmentReturn].self, forKey: .returnsBySegment)) ?? nil
    }

    enum CodingKeys: String, CodingKey {
        case ok, message, error, type, segment, goal
        case draftRef = "draft_ref"
        case targetDay = "target_day"
        case returnsBySegment = "returns_by_segment"
    }
}

/// POST /guest-newsletter/draft (the email): its fields, and the same plan.
struct StudioEmailDraft: Decodable {
    var ok = false
    var error: String?
    var subject = ""
    var preheader = ""
    var headline = ""
    var body = ""
    var buttonLabel = ""
    var buttonUrl = ""
    var draftRef: Int?
    var type: String?
    var segment: String?
    var goal: String?
    var targetDay: String?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.mktBool(.ok) ?? false
        error = c.mktText(.error)
        subject = c.mktText(.subject) ?? ""
        preheader = c.mktText(.preheader) ?? ""
        headline = c.mktText(.headline) ?? ""
        body = ((try? c.decodeIfPresent(String.self, forKey: .body)) ?? nil) ?? ""
        buttonLabel = c.mktText(.buttonLabel) ?? ""
        buttonUrl = c.mktText(.buttonUrl) ?? ""
        draftRef = c.mktInt(.draftRef)
        type = c.mktText(.type)
        segment = c.mktText(.segment)
        goal = c.mktText(.goal)
        targetDay = c.mktText(.targetDay)
    }

    enum CodingKeys: String, CodingKey {
        case ok, error, subject, preheader, headline, body, type, segment, goal
        case buttonLabel = "button_label"
        case buttonUrl = "button_url"
        case draftRef = "draft_ref"
        case targetDay = "target_day"
    }
}

/// POST /marketing/generate-content (the post).
struct StudioSocialDraft: Decodable {
    var ok = false
    var content: String?
    var error: String?
    var contentLogId: Int?
    var draftRef: Int?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.mktBool(.ok) ?? false
        content = c.mktText(.content)
        error = c.mktText(.error)
        contentLogId = c.mktInt(.contentLogId)
        draftRef = c.mktInt(.draftRef)
    }

    enum CodingKeys: String, CodingKey {
        case ok, content, error
        case contentLogId = "content_log_id"
        case draftRef = "draft_ref"
    }
}

/// POST /guest-newsletter/preview — the email exactly as a guest gets it.
struct NewsletterPreview: Decodable {
    var ok = false
    var html: String?
    var subject: String?
    var body: String?
    var mailingAddress = false

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.mktBool(.ok) ?? false
        html = (try? c.decodeIfPresent(String.self, forKey: .html)) ?? nil
        subject = c.mktText(.subject)
        body = (try? c.decodeIfPresent(String.self, forKey: .body)) ?? nil
        mailingAddress = c.mktBool(.mailingAddress) ?? false
    }

    enum CodingKeys: String, CodingKey {
        case ok, html, subject, body
        case mailingAddress = "mailing_address"
    }
}

// MARK: - Send bodies (each carries every key its route reads)

/// POST /guest-campaign/send — the web's cpSnapshot text body: segment,
/// type, target_day, link_url, hold, rec_key and draft_ref, every one sent.
struct CampaignTextSendBody: Encodable, Equatable {
    let message: String
    let segment: String
    let type: String
    var targetDay: String = ""
    var linkUrl: String = ""
    /// Outside the sending window: queued for when it opens, not refused.
    var hold: Bool = false
    var recKey: String = ""
    var draftRef: Int? = nil

    enum CodingKeys: String, CodingKey {
        case message, segment, type, hold
        case targetDay = "target_day"
        case linkUrl = "link_url"
        case recKey = "rec_key"
        case draftRef = "draft_ref"
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(message, forKey: .message)
        try c.encode(segment, forKey: .segment)
        try c.encode(type, forKey: .type)
        try c.encode(targetDay, forKey: .targetDay)
        try c.encode(linkUrl, forKey: .linkUrl)
        try c.encode(hold, forKey: .hold)
        try c.encode(recKey, forKey: .recKey)
        try c.encode(draftRef, forKey: .draftRef)
    }
}

/// POST /guest-winback/<id>/send — the drafted win-back, held like a text.
struct WinbackSendBody: Encodable, Equatable {
    let message: String
    var hold: Bool = false
}

/// POST /guest-newsletter — subject, body, design, segment, rec_key and
/// draft_ref; the mailing address only when the owner typed one.
struct NewsletterSendBody: Encodable, Equatable {
    let subject: String
    let body: String
    let design: NewsletterDesign
    let segment: String
    var recKey: String = ""
    var draftRef: Int? = nil
    var mailingAddress: String? = nil

    enum CodingKeys: String, CodingKey {
        case subject, body, design, segment
        case recKey = "rec_key"
        case draftRef = "draft_ref"
        case mailingAddress = "mailing_address"
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(subject, forKey: .subject)
        try c.encode(body, forKey: .body)
        try c.encode(design, forKey: .design)
        try c.encode(segment, forKey: .segment)
        try c.encode(recKey, forKey: .recKey)
        try c.encode(draftRef, forKey: .draftRef)
        try c.encodeIfPresent(mailingAddress, forKey: .mailingAddress)
    }
}

/// One post from the Studio, shaped per platform as the web's cpSnapshot
/// shapes it: Instagram {caption, topic, image_url}, Facebook {caption,
/// topic, media_id}, Google {summary, cta_type, cta_url, topic, media_id} —
/// each with rec_key, and content_log_id / draft_ref when the post was
/// generated. The photo goes by its library id everywhere; the routes
/// resolve and check it (social_routes.photo_url_from).
struct StudioPostBody: Encodable, Equatable {
    let platform: String
    let caption: String
    let topic: String
    var mediaId: Int? = nil
    var imageURL: String = ""
    var recKey: String = ""
    var contentLogId: Int? = nil
    var draftRef: Int? = nil

    var path: String {
        switch platform {
        case "instagram": return "/mobile/api/marketing/post-to-instagram"
        case "facebook": return "/mobile/api/marketing/post-to-facebook"
        default: return "/mobile/api/marketing/google-post"
        }
    }

    enum CodingKeys: String, CodingKey {
        case caption, topic, summary
        case imageURL = "image_url"
        case mediaId = "media_id"
        case ctaType = "cta_type"
        case ctaUrl = "cta_url"
        case recKey = "rec_key"
        case contentLogId = "content_log_id"
        case draftRef = "draft_ref"
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        if platform == "google" {
            try c.encode(caption, forKey: .summary)
            try c.encode("", forKey: .ctaType)
            try c.encode("", forKey: .ctaUrl)
        } else {
            try c.encode(caption, forKey: .caption)
        }
        try c.encode(topic, forKey: .topic)
        if platform == "instagram" { try c.encode(imageURL, forKey: .imageURL) }
        try c.encodeIfPresent(mediaId, forKey: .mediaId)
        try c.encode(recKey, forKey: .recKey)
        try c.encodeIfPresent(contentLogId, forKey: .contentLogId)
        try c.encodeIfPresent(draftRef, forKey: .draftRef)
    }
}

// MARK: - Opportunity Feed (GET /mobile/api/marketing/opportunities)

/// One card: what the data supports doing now, with the measured gap
/// (`stake` — a gap, never an expected return), ONE measured confidence
/// (none on a fact card) and the goal the Studio drafts from.
struct MarketingOpportunity: Decodable, Identifiable, Equatable {
    struct Stake: Decodable, Equatable {
        let amount: Double?
        let label: String?
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            amount = c.mktDouble(.amount)
            label = c.mktText(.label)
        }
        enum CodingKeys: String, CodingKey { case amount, label }
    }

    struct Action: Decodable, Equatable {
        var prompt: String = ""
        var channels: [String] = []
        init() {}
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            prompt = c.mktText(.prompt) ?? ""
            channels = c.mktStrings(.channels)
        }
        enum CodingKeys: String, CodingKey { case prompt, channels }
    }

    let key: String
    var kind: String = ""
    var title: String = ""
    var why: String?
    var facts: [String] = []
    var stake: Stake?
    var daysAway: Int?
    var action = Action()
    var learnedWeight: Double?
    var recId: Int?
    var confidence: TrustConfidence?
    var conflict: RecConflict?

    var id: String { key }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let k = c.mktText(.key) else {
            throw DecodingError.dataCorruptedError(forKey: .key, in: c, debugDescription: "no key")
        }
        key = k
        kind = c.mktText(.kind) ?? ""
        title = c.mktText(.title) ?? ""
        why = c.mktText(.why)
        facts = c.mktStrings(.facts)
        stake = (try? c.decodeIfPresent(Stake.self, forKey: .stake)) ?? nil
        daysAway = c.mktInt(.daysAway)
        action = ((try? c.decodeIfPresent(Action.self, forKey: .action)) ?? nil) ?? Action()
        learnedWeight = c.mktDouble(.learnedWeight)
        recId = c.mktInt(.recId)
        confidence = (try? c.decodeIfPresent(TrustConfidence.self, forKey: .confidence)) ?? nil
        conflict = (try? c.decodeIfPresent(RecConflict.self, forKey: .conflict)) ?? nil
    }

    enum CodingKeys: String, CodingKey {
        case key, kind, title, why, facts, stake, action, confidence, conflict
        case daysAway = "days_away"
        case learnedWeight = "learned_weight"
        case recId = "rec_id"
    }

    static func == (a: MarketingOpportunity, b: MarketingOpportunity) -> Bool { a.key == b.key && a.recId == b.recId }

    /// The web's MKT_OPP_KIND label.
    var kindLabel: String {
        switch kind {
        case "slow_night": return "Slow night"
        case "holiday": return "Holiday"
        case "category_dip": return "Sales dip"
        case "dish_promote": return "Dish"
        case "dish_praise": return "Guest favorite"
        case "list_idle": return "Your list"
        case "posting": return "Posting"
        case "web_dip": return "Website"
        default: return "Opportunity"
        }
    }

    /// "Tomorrow" / "In 3 days".
    var whenLabel: String? {
        guard let d = daysAway else { return nil }
        return d == 1 ? "Tomorrow" : "In \(d) days"
    }

    /// "$3,500 a Tuesday night under a typical day".
    var stakeLine: String? {
        guard let amount = stake?.amount, amount > 0 else { return nil }
        let money = "$" + amount.commaFormatted
        guard let label = stake?.label, !label.isEmpty else { return money }
        return "\(money) \(label)"
    }

    /// Ranked up or down by what this restaurant's own results measured.
    var rankedByResults: Bool { (learnedWeight ?? 1) != 1 }

    /// The goal the Studio drafts from.
    var goal: String {
        let p = action.prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        return p.isEmpty ? title : p
    }

    /// The channels the card names (all three when it names none).
    var wantedChannels: [String] { action.channels.isEmpty ? ["text", "email", "social"] : action.channels }
}

struct OpportunityFeed: Decodable {
    struct Source: Decodable, Hashable {
        let key: String?
        let label: String
        let state: String
        let note: String?
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            key = c.mktText(.key)
            label = c.mktText(.label) ?? ""
            state = c.mktText(.state) ?? "checked"
            note = c.mktText(.note)
        }
        enum CodingKeys: String, CodingKey { case key, label, state, note }
    }

    var ok = false
    var items: [MarketingOpportunity] = []
    var visible = 3
    var sources: [Source] = []
    var checked: [String] = []
    var error: String?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.mktBool(.ok) ?? false
        items = ((try? c.decodeIfPresent(HomeLenientListDecodable<MarketingOpportunity>.self, forKey: .items)) ?? nil)?
            .items ?? []
        visible = c.mktInt(.visible) ?? 3
        sources = ((try? c.decodeIfPresent([Source].self, forKey: .sources)) ?? nil) ?? []
        checked = c.mktStrings(.checked)
        error = c.mktText(.error)
    }

    enum CodingKeys: String, CodingKey { case ok, items, visible, sources, checked, error }

    /// The empty state in words: what was checked, what had nothing to read
    /// yet (and why), what couldn't be read — and whether any failed.
    static func emptyLine(sources: [Source], checked: [String]) -> (text: String, warn: Bool) {
        var ok: [String] = [], none: [String] = [], bad: [String] = []
        for s in sources {
            switch s.state {
            case "failed": bad.append(s.label)
            case "no_data": none.append(s.label + (s.note.map { " (\($0))" } ?? ""))
            default: ok.append(s.label)
            }
        }
        if sources.isEmpty { ok = checked }
        var t = "Nothing stands out right now."
        if !ok.isEmpty { t += " Cavnar AI checked " + ok.joined(separator: ", ") + "." }
        if !none.isEmpty { t += " Nothing to read yet for " + none.joined(separator: "; ") + "." }
        if !bad.isEmpty { t += " Couldn\u{2019}t check " + bad.joined(separator: ", ") + " just now \u{2014} it tries again next time." }
        return (t, !bad.isEmpty)
    }
}

// MARK: - Review-link invites (GET/POST /mobile/api/guest-optin-invites)

struct OptinInvitesState: Decodable, Equatable {
    var ok = false
    var enabled = false
    var acknowledgedAt: String?
    var disclosure: String?
    var error: String?

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.mktBool(.ok) ?? false
        enabled = c.mktBool(.enabled) ?? false
        acknowledgedAt = c.mktText(.acknowledgedAt)
        disclosure = c.mktText(.disclosure)
        error = c.mktText(.error)
    }

    enum CodingKeys: String, CodingKey {
        case ok, enabled, disclosure, error
        case acknowledgedAt = "acknowledged_at"
    }
}

/// POST {enabled, acknowledged}: turning invites on IS the acknowledgement.
struct OptinInvitesBody: Encodable, Equatable {
    let enabled: Bool
    let acknowledged: Bool
}
