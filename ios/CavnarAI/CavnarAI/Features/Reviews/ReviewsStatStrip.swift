import SwiftUI

/// The reputation figures on the phone: the rating and how much of it is
/// answered. The web's header carries four pills; on the iPhone the "to
/// approve" and "urgent" counts ride the filter chips (one set of inbox
/// counts, readability round 10/8/26 #55), so they are not said twice.
///
/// ReviewStats was modelled in full and then called from nowhere in the
/// app, so the Reviews tab opened straight into a list with no summary of
/// the restaurant's reputation anywhere on it. Two of the honesty lines
/// that model carries were invisible for the same reason and are shown
/// here too, behind the info button: how much of Google's history our
/// average actually covers (isFullHistory / officialReviewCount), and how
/// many reviews the sentiment and topic figures don't cover yet (unanalysed).
struct ReviewsStatStrip: View {
    let stats: ReviewStats
    @State private var showingCaveat = false

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(spacing: CavnarSpace.xs) {
                pill(value: String(format: "%.1f", stats.avgRating), unit: "\u{2605}",
                     label: "rating", tone: ratingTone,
                     spoken: "\(String(format: "%.1f", stats.avgRating)) star average rating")
                pill(value: "\(Int(stats.responseRate))", unit: "%",
                     label: "answered", tone: stats.responseRate >= 70 ? .good : (stats.responseRate >= 40 ? .warning : .bad),
                     spoken: "\(Int(stats.responseRate)) percent of reviews answered")
                if coverageCaveat != nil {
                    Button {
                        Haptic.light()
                        withAnimation(.easeOut(duration: 0.2)) { showingCaveat.toggle() }
                    } label: {
                        Image(systemName: showingCaveat ? "info.circle.fill" : "info.circle")
                            .font(.cavnar(.lead))
                            .foregroundStyle(Color.cavnarInk2)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("What these figures cover")
                    .accessibilityValue(showingCaveat ? "Expanded" : "Collapsed")
                }
            }
            .padding(.vertical, 2)

            if showingCaveat, let caveat = coverageCaveat {
                Text(caveat)
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .transition(.opacity)
            }
        }
    }

    private var ratingTone: CavnarTone {
        stats.avgRating >= 4 ? .good : (stats.avgRating >= 3 ? .warning : .bad)
    }

    /// Two separate honesty statements, joined only when both apply.
    private var coverageCaveat: String? {
        var parts: [String] = []
        if !stats.holdsEveryReview, let official = stats.officialReviewCount, official > stats.total {
            parts.append("Average is over the \(stats.total) reviews Cavnar AI holds, not all \(official) on Google.")
        }
        if let unanalysed = stats.unanalysed, unanalysed > 0 {
            parts.append("\(unanalysed) not yet analysed \u{2014} the topic and sentiment figures don't cover them.")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " ")
    }

    private func pill(value: String, unit: String?, label: String,
                      tone: CavnarTone, spoken: String) -> some View {
        // The figure beside its word: a status dot, the number, the label.
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            Circle()
                .fill(tone.foreground)
                .frame(width: 8, height: 8)
                .alignmentGuide(.firstTextBaseline) { d in d[.bottom] + 2 }
            HStack(alignment: .firstTextBaseline, spacing: 1) {
                Text(value)
                    .cavnarText(.figureM)
                if let unit {
                    Text(unit)
                        .font(.cavnar(.figureS))
                        .foregroundStyle(Color.cavnarInk2)
                }
            }
            Text(label)
                .cavnarText(.secondary)
            Spacer(minLength: 0)
        }
        .lineLimit(1)
        .minimumScaleFactor(0.85)
        .padding(.horizontal, CavnarSpace.s)
        .padding(.vertical, CavnarSpace.xs + 2)
        .frame(maxWidth: .infinity)
        .background(Color.cavnarPaper2.opacity(0.75))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
            .strokeBorder(Color.cavnarPaper3.opacity(0.6), lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }
}

/// The inbox's 30-second "why" (density #32): one line under the stat
/// strip that says what the reviews are about, built only from counts the
/// phone already holds — the urgent count and the categories on the
/// reviews loaded — never a model call. "2 urgent — both about wait time",
/// or "Top complaint: cold food (6 of the last 20)". Nil when there is
/// nothing to say.
///
/// When the server sends its why line (GET /mobile/api/reviews/why-line —
/// the rating's move over its window and the STORED top complaint, the
/// same read the web inbox leads with) that leads: "Rating ▼0.1 over 8
/// weeks · top complaint: cold food (6 mentions)". The phone-side read is
/// the fallback for an older server.
enum ReviewsWhyLine {
    /// How many of the newest reviews the complaint count reads.
    static let window = 20

    /// The server's line, or nil when it carries nothing to say. A null
    /// `rating_delta` is "below the floor", never a flat rating; a stale
    /// complaint is left out rather than stated as current.
    static func make(urgent: Int, server: ReviewsWhyPayload?) -> String? {
        guard let s = server else { return nil }
        var parts: [String] = []
        if urgent > 0 { parts.append("\(urgent) urgent") }
        if let delta = s.ratingDelta {
            let weeks = (s.windowDays ?? 0) >= 14 ? "\((s.windowDays ?? 0) / 7) weeks" : "\(s.windowDays ?? 0) days"
            if abs(delta) < 0.05 {
                parts.append("rating level over \(weeks)")
            } else {
                parts.append("rating \(delta > 0 ? "\u{25B2}" : "\u{25BC}")\(String(format: "%.1f", abs(delta))) over \(weeks)")
            }
        }
        if let c = s.complaint, c.stale != true, let label = c.label, !label.isEmpty {
            let n = c.mentions.map { " (\($0) mention\($0 == 1 ? "" : "s"))" } ?? ""
            parts.append("top complaint: \(label.lowercased())\(n)")
        }
        // The urgent count alone is the local line's job — it can say what
        // the urgent ones are about.
        guard parts.count > (urgent > 0 ? 1 : 0) else { return nil }
        let line = parts.joined(separator: " \u{00B7} ")
        return line.prefix(1).uppercased() + line.dropFirst()
    }

    /// The server line when it says something, else the phone's own.
    static func make(urgent: Int, server: ReviewsWhyPayload?, reviews: [Review]) -> String? {
        make(urgent: urgent, server: server) ?? make(urgent: urgent, reviews: reviews)
    }

    static func make(urgent: Int, reviews: [Review]) -> String? {
        let recent = Array(reviews.prefix(window))
        if urgent > 0 {
            let urgentRows = recent.filter { $0.isUrgent && !$0.isPosted }
            let lead = "\(urgent) urgent"
            if let top = topCategory(urgentRows), top.count == urgentRows.count, urgentRows.count > 0 {
                let about = urgentRows.count == 1 ? "about \(top.label)"
                                                  : (urgentRows.count == 2 ? "both about \(top.label)"
                                                                           : "all about \(top.label)")
                return "\(lead) \u{2014} \(about)"
            }
            if let top = topCategory(recent.filter(isComplaint)), top.count >= 2 {
                return "\(lead) \u{00B7} top complaint: \(top.label) (\(top.count) of the last \(recent.count))"
            }
            return "\(lead) waiting on a reply"
        }
        if let top = topCategory(recent.filter(isComplaint)), top.count >= 2 {
            return "Top complaint: \(top.label) (\(top.count) of the last \(recent.count))"
        }
        return nil
    }

    private static func isComplaint(_ r: Review) -> Bool {
        r.sentiment == "negative" || (r.rating ?? 5) <= 2
    }

    private static func topCategory(_ rows: [Review]) -> (label: String, count: Int)? {
        var counts: [String: Int] = [:]
        for r in rows {
            for c in Set(r.categories) where !c.isEmpty { counts[c, default: 0] += 1 }
        }
        guard let best = counts.max(by: { ($0.value, $1.key) < ($1.value, $0.key) }) else { return nil }
        return (best.key.replacingOccurrences(of: "_", with: " ").lowercased(), best.value)
    }
}

/// GET /mobile/api/reviews/why-line — the rating's move over its window and
/// the stored top complaint. Every field lenient: an odd value is nil, and
/// `rating_delta` null is "below the floor", never 0.
struct ReviewsWhyPayload: Decodable, Equatable {
    struct Complaint: Decodable, Equatable {
        let category: String?
        let label: String?
        let mentions: Int?
        let windowDays: Int?
        let stale: Bool?
        let asOf: String?

        enum CodingKeys: String, CodingKey {
            case category, label, mentions, stale
            case windowDays = "window_days"
            case asOf = "as_of"
        }

        init(from decoder: Decoder) throws {
            let c = try? decoder.container(keyedBy: CodingKeys.self)
            category = (try? c?.decodeIfPresent(String.self, forKey: .category)) ?? nil
            label = (try? c?.decodeIfPresent(String.self, forKey: .label)) ?? nil
            mentions = (try? c?.decodeIfPresent(Int.self, forKey: .mentions)) ?? nil
            windowDays = (try? c?.decodeIfPresent(Int.self, forKey: .windowDays)) ?? nil
            stale = (try? c?.decodeIfPresent(Bool.self, forKey: .stale)) ?? nil
            asOf = (try? c?.decodeIfPresent(String.self, forKey: .asOf)) ?? nil
        }
    }

    let ratingDelta: Double?
    let recentN: Int?
    let priorN: Int?
    let windowDays: Int?
    let complaint: Complaint?

    enum CodingKeys: String, CodingKey {
        case complaint
        case ratingDelta = "rating_delta"
        case recentN = "recent_n"
        case priorN = "prior_n"
        case windowDays = "window_days"
    }

    init(from decoder: Decoder) throws {
        let c = try? decoder.container(keyedBy: CodingKeys.self)
        ratingDelta = (try? c?.decodeIfPresent(Double.self, forKey: .ratingDelta)) ?? nil
        recentN = (try? c?.decodeIfPresent(Int.self, forKey: .recentN)) ?? nil
        priorN = (try? c?.decodeIfPresent(Int.self, forKey: .priorN)) ?? nil
        windowDays = (try? c?.decodeIfPresent(Int.self, forKey: .windowDays)) ?? nil
        complaint = (try? c?.decodeIfPresent(Complaint.self, forKey: .complaint)) ?? nil
    }
}
