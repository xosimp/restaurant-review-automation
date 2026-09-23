import Foundation

/// A photo uploaded for a post. Instagram requires an image and the app's only
/// way to supply one used to be a text field asking for a public URL — on a
/// phone, where the photo is in the camera roll and has no URL anywhere.
struct MarketingMedia: Decodable, Identifiable, Hashable {
    let id: Int
    let token: String
    let url: String
    let width: Int?
    let height: Int?

    enum CodingKeys: String, CodingKey {
        case id, token, url, width, height
        case mediaId = "media_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // The upload response calls it media_id; the list response calls it id.
        id = try c.decodeIfPresent(Int.self, forKey: .mediaId)
            ?? c.decode(Int.self, forKey: .id)
        token = try c.decode(String.self, forKey: .token)
        url = try c.decodeIfPresent(String.self, forKey: .url) ?? ""
        width = try c.decodeIfPresent(Int.self, forKey: .width)
        height = try c.decodeIfPresent(Int.self, forKey: .height)
    }
}

/// What the post will look like where it lands, and whether it will be
/// accepted. Computed on the server so the app and the dashboard can't
/// disagree about whether something fits.
struct MarketingPreview: Decodable {
    let platform: String
    let characters: Int
    let limit: Int?
    let overLimit: Bool
    let visibleBeforeMore: String
    let truncated: Bool
    let hashtags: [String]
    let hashtagCount: Int
    let imageURL: String?
    let problems: [String]
    let ready: Bool

    enum CodingKeys: String, CodingKey {
        case platform, characters, limit, truncated, hashtags, ready, problems
        case overLimit = "over_limit"
        case visibleBeforeMore = "visible_before_more"
        case hashtagCount = "hashtag_count"
        case imageURL = "image_url"
    }
}

/// A post written now and published later.
struct ScheduledPost: Decodable, Identifiable {
    let id: Int
    let platform: String
    let contentType: String?
    let topic: String?
    let body: String
    let scheduledFor: String
    let status: String
    let error: String?
    let mediaToken: String?

    enum CodingKeys: String, CodingKey {
        case id, platform, topic, body, status, error
        case contentType = "content_type"
        case scheduledFor = "scheduled_for"
        case mediaToken = "media_token"
    }

    var isPending: Bool { status == "scheduled" }

    /// "9/6/26 · 11:00am" — the restaurant's own wall clock, which is what
    /// the owner picked, so it is read as one rather than converted.
    var whenLabel: String { CavnarDate.mdyTime(scheduledFor) }

    var statusLabel: String {
        switch status {
        case "scheduled": return "Scheduled"
        case "posted": return "Posted"
        case "failed": return "Didn't go out"
        case "cancelled": return "Cancelled"
        default: return status.capitalized
        }
    }
}

/// Saved copy. Generated content used to survive exactly as long as the
/// screen it was on.
struct MarketingDraft: Decodable, Identifiable {
    let id: Int
    let contentType: String?
    let topic: String?
    let body: String
    let status: String
    let updatedAt: String?
    let createdByName: String?
    let approvedByName: String?
    let mediaToken: String?

    enum CodingKeys: String, CodingKey {
        case id, topic, body, status
        case contentType = "content_type"
        case updatedAt = "updated_at"
        case createdByName = "created_by_name"
        case approvedByName = "approved_by_name"
        case mediaToken = "media_token"
    }

    var isApproved: Bool { status == "approved" }
}

/// Reach and engagement for a period, against the period before it. The old
/// summary was all-time totals: "Total reach 4,231" with no denominator and
/// no trend is a number, not a metric.
struct MarketingWindow: Decodable {
    struct Bucket: Decodable {
        let posts: Int
        let reach: Int
        let engagement: Int
        let engagementRate: Double?

        enum CodingKeys: String, CodingKey {
            case posts, reach, engagement
            case engagementRate = "engagement_rate"
        }
    }

    struct Change: Decodable {
        let posts: Double?
        let reach: Double?
        let engagement: Double?
    }

    struct Platform: Decodable, Identifiable {
        let platform: String
        let posts: Int
        let reach: Int
        let engagement: Int
        let engagementRate: Double?

        enum CodingKeys: String, CodingKey {
            case platform, posts, reach, engagement
            case engagementRate = "engagement_rate"
        }

        var id: String { platform }

        var label: String {
            switch platform {
            case "instagram": return "Instagram"
            case "facebook": return "Facebook"
            case "google": return "Google"
            default: return platform.capitalized
            }
        }
    }

    let days: Int
    let posts: Int
    let reach: Int
    let engagement: Int
    /// nil when nothing was seen (no reach or impressions measured) — the
    /// server no longer reports that as 0% (MOD-MKT-18).
    let engagementRate: Double?
    let previous: Bucket
    let change: Change
    let byPlatform: [Platform]

    enum CodingKeys: String, CodingKey {
        case days, posts, reach, engagement, previous, change
        case engagementRate = "engagement_rate"
        case byPlatform = "by_platform"
    }
}

/// What a post did to the till. Correlational, and labelled that way
/// everywhere it renders.
struct MarketingAttribution: Decodable {
    struct Post: Decodable, Identifiable {
        let topic: String?
        let platform: String?
        let postedAt: String?
        let windowSales: Double
        let baselineSales: Double
        let liftPct: Double
        let baselineDays: Int
        // What the post was about and what else its window showed
        // (marketing_signals._beyond_sales). Optional: older servers omit them.
        let menuItemName: String?
        let occasion: String?
        let postKind: String?
        let itemLiftPct: Double?
        let reviewsMentioning: Int?
        let guestListDelta: Int?
        let engagementRate: Double?
        /// "lifted" | "dropped" | "no_clear_change" — the lift judged against
        /// how much the same weekday moves on its own (`noiseBandPct`, ±%).
        /// The screen colours and words the result from this, never from
        /// the sign of liftPct. Optional: older servers omit both.
        let verdict: String?
        let noiseBandPct: Double?

        enum CodingKeys: String, CodingKey {
            case topic, platform, occasion, verdict
            case noiseBandPct = "noise_band_pct"
            case postedAt = "posted_at"
            case windowSales = "window_sales"
            case baselineSales = "baseline_sales"
            case liftPct = "lift_pct"
            case baselineDays = "baseline_days"
            case menuItemName = "menu_item_name"
            case postKind = "post_kind"
            case itemLiftPct = "item_lift_pct"
            case reviewsMentioning = "reviews_mentioning"
            case guestListDelta = "guest_list_delta"
            case engagementRate = "engagement_rate"
        }

        var id: String { (topic ?? "") + (postedAt ?? "") }

        enum LiftVerdict: Equatable { case lifted, dropped, noClearChange }

        /// The server's verdict; an older payload without one falls back to
        /// the sign of the lift (the old behaviour), and an unknown word to
        /// "no clear change" — never a colour the server didn't give.
        var liftVerdict: LiftVerdict {
            switch verdict {
            case "lifted": return .lifted
            case "dropped": return .dropped
            case .some: return .noClearChange
            case .none: return liftPct >= 0 ? .lifted : .dropped
            }
        }

        /// "Margherita · game day", or nil when nothing was inferred.
        var aboutLabel: String? {
            let bits = [menuItemName, occasion?.replacingOccurrences(of: "_", with: " ")].compactMap { $0 }
            return bits.isEmpty ? nil : bits.joined(separator: " · ")
        }

        /// The secondary line: the dish's own units, reviews that mentioned
        /// it, engagement. Only what was measured.
        var detailLine: String? {
            var bits: [String] = []
            if let name = menuItemName, let il = itemLiftPct {
                bits.append("\(name) \(il > 0 ? "+" : "")\(Int(il.rounded()))% units")
            }
            if let n = reviewsMentioning, n > 0 { bits.append("\(n) review\(n == 1 ? "" : "s") mentioned it") }
            if let e = engagementRate { bits.append("\(String(format: "%.1f", e * 100))% engagement") }
            if let g = guestListDelta, g != 0 { bits.append("guest list \(g > 0 ? "+" : "")\(g)") }
            return bits.isEmpty ? nil : bits.joined(separator: " · ")
        }
    }

    struct Group: Decodable, Identifiable {
        let group: String
        let posts: Int
        let medianLiftPct: Double
        let medianItemLiftPct: Double?
        enum CodingKeys: String, CodingKey {
            case group, posts
            case medianLiftPct = "median_lift_pct"
            case medianItemLiftPct = "median_item_lift_pct"
        }
        var id: String { group }
    }

    let ok: Bool
    let reason: String?
    let posts: [Post]
    let weakest: [Post]?
    let measured: Int?
    let medianLiftPct: Double?
    let byKind: [Group]?
    let byOccasion: [Group]?
    let byDish: [Group]?

    enum CodingKeys: String, CodingKey {
        case ok, reason, posts, measured, weakest
        case medianLiftPct = "median_lift_pct"
        case byKind = "by_kind"
        case byOccasion = "by_occasion"
        case byDish = "by_dish"
    }

    /// Why there's no number yet, in words an owner can act on.
    var emptyExplanation: String {
        switch reason {
        case "no_pos_data":
            return "Connect your POS under Account → Connections and this will start showing what each post did to sales."
        case "not_enough_history":
            return "Not enough sales history yet to compare a post against the same weekday before it."
        default:
            return "Nothing measurable yet — this needs a few posts and a few weeks of sales behind them."
        }
    }
}

/// An audience a campaign can go to. `win_back` used to be a TONE the copy
/// was written in, never an AUDIENCE it was sent to, so a "we miss you" text
/// reached someone who ate there last night.
struct GuestSegment: Decodable, Identifiable, Hashable {
    let key: String
    let label: String
    let help: String
    let count: Int

    var id: String { key }
}

/// One campaign that went out. guest_campaigns has recorded every send since
/// the table existed and nothing ever displayed it.
struct GuestCampaign: Decodable, Identifiable {
    let id: Int
    let message: String
    let sentCount: Int
    let failedCount: Int
    let segmentLabel: String?
    let clicks: Int
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, message, clicks
        case sentCount = "sent_count"
        case failedCount = "failed_count"
        case segmentLabel = "segment_label"
        case createdAt = "created_at"
    }

    /// created_at is UTC; the day it went out is the restaurant's day.
    var whenLabel: String {
        let parser = DateFormatter()
        parser.locale = Locale(identifier: "en_US_POSIX")
        parser.dateFormat = "yyyy-MM-dd HH:mm:ss"
        parser.timeZone = TimeZone(identifier: "UTC")
        guard let raw = createdAt, let date = parser.date(from: String(raw.prefix(19))) else {
            return createdAt ?? ""
        }
        return CavnarDate.mdy(date, in: RestaurantClock.timeZone)
    }
}

/// The compliance picture. Consent has always been recorded and never shown —
/// if anyone ever asks how a number got on this list, this is the answer.
struct ConsentLedger: Decodable {
    let total: Int
    let textable: Int
    let unsubscribed: Int
    let noConsent: Int
    let textsThisMonth: Int
    let campaignsThisMonth: Int
    let window: String
    let minDaysBetween: Int

    enum CodingKeys: String, CodingKey {
        case total, textable, unsubscribed, window
        case noConsent = "no_consent"
        case textsThisMonth = "texts_this_month"
        case campaignsThisMonth = "campaigns_this_month"
        case minDaysBetween = "min_days_between"
    }
}
