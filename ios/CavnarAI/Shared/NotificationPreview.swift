import Foundation

/// What a notification's own expanded view shows (the CavnarNotificationContent
/// extension, parity audit #36): before "Approve & post", the reply that one
/// press publishes — push.py puts it in the payload as `draft` (clipped to
/// fit APNs, `draft_complete` false when it was) — and for the nightly report
/// its headline, whole. Read from the payload only; nothing here calls the
/// API. Compiled into the app as well, so its reading is unit-tested there.
struct NotificationPreview: Equatable, Sendable {
    enum Kind: Equatable, Sendable {
        /// A review with a drafted reply (CAVNAR_REVIEW_DRAFTED).
        case reply
        /// The nightly Daily Sales Report (CAVNAR_DSR).
        case report
        /// Tonight's lineup brief waiting for approval (CAVNAR_LINEUP, or
        /// CAVNAR_LINEUP_REVIEW when the draft did not ride whole): the
        /// words Approve sends to staff (re-audit 10/8/26).
        case lineup
        /// Anything else: the title and body as sent.
        case other
    }

    let kind: Kind
    /// The ember kicker over the content, in the app's own words.
    let kicker: String
    let title: String
    /// The notification's body: the guest's words for a review, the night's
    /// headline for the report.
    let body: String
    /// The reply Approve & post publishes; nil when the push carried none.
    let draft: String?
    /// False when `draft` is only the start of the reply.
    let draftComplete: Bool
    /// "M/D/YY" — the night the report is for.
    let night: String?

    static let replyCategory = "CAVNAR_REVIEW_DRAFTED"
    static let reportCategory = "CAVNAR_DSR"
    static let lineupCategories: Set<String> = ["CAVNAR_LINEUP", "CAVNAR_LINEUP_REVIEW"]

    init(title: String, body: String, category: String, userInfo: [AnyHashable: Any]) {
        let cavnar = (userInfo["cavnar"] as? [String: Any]) ?? [:]
        self.title = title
        self.body = body
        let rawDraft = (cavnar["draft"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        let complete = (cavnar["draft_complete"] as? Bool) ?? (cavnar["draft_complete"] as? NSNumber)?.boolValue
        switch category {
        case _ where Self.lineupCategories.contains(category):
            kind = .lineup
            kicker = "Lineup brief"
            draft = (rawDraft?.isEmpty == false) ? rawDraft : nil
            // Approve is only on CAVNAR_LINEUP: the review category's draft
            // is never "the words Approve sends".
            draftComplete = draft != nil && complete == true && category == "CAVNAR_LINEUP"
            night = Self.mdy(cavnar["day"] as? String)
        case Self.replyCategory:
            kind = .reply
            kicker = "The review"
            draft = (rawDraft?.isEmpty == false) ? rawDraft : nil
            draftComplete = draft != nil && complete == true
            night = nil
        case Self.reportCategory:
            kind = .report
            kicker = "Daily report"
            draft = nil
            draftComplete = false
            night = Self.mdy((cavnar["business_date"] as? String) ?? (userInfo["business_date"] as? String))
        default:
            kind = .other
            kicker = "Cavnar AI"
            draft = nil
            draftComplete = false
            night = nil
        }
    }

    /// The kicker over the drafted text's own card; nil when there is none.
    var draftKicker: String? {
        switch kind {
        case .reply: return "The reply"
        case .lineup: return "What staff read"
        default: return nil
        }
    }

    /// What sits under the reply: whether the whole of it is shown.
    var draftNote: String? {
        if kind == .lineup {
            guard draft != nil else { return "Open it to read the brief before it goes to staff." }
            return draftComplete ? "This is the brief Approve sends to your team."
                                 : "Open it to read the whole brief before it goes to staff."
        }
        guard kind == .reply else { return nil }
        guard draft != nil else { return "Open it to read the reply before you post it." }
        return draftComplete ? "This is the reply Approve & post publishes."
                             : "The reply is longer than a notification holds \u{2014} open it to read the rest."
    }

    /// `2026-10-06` → `10/6/26`; nil for anything else (never an ISO date).
    static func mdy(_ iso: String?) -> String? {
        guard let iso, iso.count >= 10 else { return nil }
        let p = iso.prefix(10).split(separator: "-")
        guard p.count == 3, p[0].count == 4, let m = Int(p[1]), let d = Int(p[2]) else { return nil }
        return "\(m)/\(d)/\(p[0].suffix(2))"
    }
}
