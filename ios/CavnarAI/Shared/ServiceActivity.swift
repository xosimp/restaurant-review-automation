import ActivityKit
import Foundation

/// "Tonight's service" on the Lock Screen and in the Dynamic Island
/// (parity audit #94): how the night is running against a typical same
/// weekday ("▲ 8% vs a typical Fri") and who on the published schedule
/// hasn't clocked in. Opt-in per phone (Account → Alerts → On this phone);
/// it starts at service open, follows the 20-minute service slot and ends
/// at close.
///
/// Both halves are built on the server by one function
/// (live_activities.service_content) — for GET /mobile/api/intraday/tonight
/// and for every push — and decoded here as is, so the app's own start and
/// the server's updates can never disagree. Dates in the ContentState are
/// ActivityKit's (seconds since 2001, push.apple_date).
struct ServiceAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// open | closed
        var status: String
        /// "▲ 8% vs a typical Fri"; nil while the pulse can't say (then
        /// `pulseNote` says why — never a 0%).
        var pulseLine: String?
        var pulseUp: Bool?
        var pulseNote: String?
        /// Who on the published schedule hasn't clocked in (the first four),
        /// and how many in all; `coverageNote` when it couldn't be checked.
        var missing: [String]
        var missingCount: Int?
        var coverageNote: String?
        var closesAt: Date?
        var updatedAt: Date?

        init(status: String, pulseLine: String? = nil, pulseUp: Bool? = nil, pulseNote: String? = nil,
             missing: [String] = [], missingCount: Int? = nil, coverageNote: String? = nil,
             closesAt: Date? = nil, updatedAt: Date? = nil) {
            self.status = status
            self.pulseLine = pulseLine
            self.pulseUp = pulseUp
            self.pulseNote = pulseNote
            self.missing = missing
            self.missingCount = missingCount
            self.coverageNote = coverageNote
            self.closesAt = closesAt
            self.updatedAt = updatedAt
        }

        enum CodingKeys: String, CodingKey {
            case status, pulseLine, pulseUp, pulseNote, missing, missingCount, coverageNote, closesAt, updatedAt
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            status = (try? c.decode(String.self, forKey: .status)) ?? "open"
            pulseLine = try? c.decodeIfPresent(String.self, forKey: .pulseLine)
            pulseUp = try? c.decodeIfPresent(Bool.self, forKey: .pulseUp)
            pulseNote = try? c.decodeIfPresent(String.self, forKey: .pulseNote)
            missing = ((try? c.decodeIfPresent([String].self, forKey: .missing)) ?? nil) ?? []
            missingCount = try? c.decodeIfPresent(Int.self, forKey: .missingCount)
            coverageNote = try? c.decodeIfPresent(String.self, forKey: .coverageNote)
            closesAt = try? c.decodeIfPresent(Date.self, forKey: .closesAt)
            updatedAt = try? c.decodeIfPresent(Date.self, forKey: .updatedAt)
        }

        /// "Dana and Luis haven't clocked in" / "Everyone's in" / the reason
        /// it couldn't be checked.
        var coverageLine: String {
            let total = missingCount ?? missing.count
            guard total > 0 else {
                return coverageNote ?? "Everyone on the schedule is in"
            }
            let shown = missing.prefix(2).joined(separator: " and ")
            let more = total - min(2, missing.count)
            let who = more > 0 ? "\(shown) +\(more)" : shown
            return total == 1 && more == 0 ? "\(who) hasn't clocked in" : "\(who) haven't clocked in"
        }
    }

    let restaurantId: Int
    let restaurantName: String
    /// The service's business date (ISO) — what the server keys it by.
    let businessDate: String
    /// "Fri 10/9/26".
    let dayLabel: String
}
