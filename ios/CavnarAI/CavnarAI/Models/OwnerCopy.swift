import Foundation

/// The owner-copy rules that are logic rather than layout (never-say audit,
/// workstream C; DESIGN_SYSTEM.md → *Money labels and positive status*).
/// Pure functions over payload fields, so each rule is unit-tested once and
/// every view reads the same answer:
///
/// - A money figure says what KIND it is. Only a measured result may read as
///   one; an opportunity is a gap, "available, not captured"; a projection
///   says "projected". Never "savings" on a gap, never the green tone on one.
/// - A positive status word ("On target", "All clear") needs the payload to
///   say the data under it is live, complete and fresh. Anything short of
///   that is "—" and the reason.
/// - A model's forecast is conditional on its cause, and a certainty is not
///   shown at all.
enum OwnerCopy {

    // MARK: - Money kinds

    /// The word a money figure carries, from its `kind` (the server's money
    /// `kind` field, when it sends one). Nil for an unknown or absent kind,
    /// so the caller keeps its own default.
    static func kindWord(_ kind: String?) -> String? {
        switch (kind ?? "").lowercased() {
        case "measured": return "measured"
        case "computed": return "computed"
        case "estimate": return "estimated"
        case "opportunity": return "at stake \u{00B7} not captured"
        case "projection": return "projected"
        case "forecast": return "forecast"
        case "plan": return "budget"
        case "benchmark": return "vs benchmark"
        case "price": return "price"
        default: return nil
        }
    }

    // MARK: - Positive status

    enum Tone: Equatable { case good, warn, bad, neutral }

    struct Status: Equatable {
        let label: String
        let tone: Tone
    }

    /// Whether the labor read may carry a positive word or the green tone:
    /// live data, complete (no missing-sales days, clocked not planned
    /// hours), long enough to read against the target, and with sales under
    /// it — labor at 0% with no sales used to read "Excellent" (NS1 #4).
    static func laborPositiveAllowed(isLive: Bool, dataComplete: Bool?, salesDataMissing: Bool?,
                                     hoursAreEstimated: Bool?, periodTooShort: Bool?, pct: Double) -> Bool {
        isLive && dataComplete != false && salesDataMissing != true && hoursAreEstimated != true
            && periodTooShort != true && pct > 0
    }

    /// The verdict beside labor % against the owner's own target. Sample
    /// data gets no verdict (the sample week's 36.8% is not this restaurant
    /// over target); over target always says so (a partial figure already
    /// over is not reassuring); at or under says "On target" only when
    /// allowed.
    static func laborBucket(pct: Double, target: Double, isLive: Bool, positiveAllowed: Bool) -> Status {
        if !isLive { return Status(label: "Sample", tone: .neutral) }
        if pct > target + 8 { return Status(label: "Needs Attention", tone: .bad) }
        if pct > target + 3 { return Status(label: "Above Target", tone: .warn) }
        if pct > target { return Status(label: "Slightly Over", tone: .warn) }
        if !positiveAllowed { return Status(label: "\u{2014} Incomplete data", tone: .neutral) }
        if pct <= target - 3 { return Status(label: "Well Under Target", tone: .good) }
        return Status(label: "On Target", tone: .good)
    }

    /// Whether Home may say "All clear": nothing needs attention AND the
    /// server's `monitoring.all_clear` is true when it sends one — a live
    /// source, none stale. An older payload with no flag falls back to its
    /// live count and stale count. Returns the reason when it may not.
    static func allClear(attentionEmpty: Bool, monitoring: HomeMonitoring?) -> (clear: Bool, reason: String?) {
        guard attentionEmpty else { return (false, nil) }
        guard let m = monitoring else {
            return (false, "Nothing flagged \u{2014} but the sources under this haven't reported yet.")
        }
        if let stale = m.stale, stale > 0 {
            return (false, "Nothing flagged \u{2014} but \(stale) source\(stale == 1 ? " is" : "s are") out of date or undated, so this is not a clean bill.")
        }
        if let live = m.countLive, live == 0 {
            return (false, "Nothing flagged \u{2014} but no source under this is current yet, so there is nothing to watch.")
        }
        if m.allClear == false {
            return (false, "Nothing flagged \u{2014} but the sources under this are not all current.")
        }
        return (true, nil)
    }

    // MARK: - Labor money tiles

    struct MoneyTile: Equatable {
        let value: Double
        let label: String
        let sublabel: String
        let tone: Tone
    }

    /// The Labor tab's money tiles. The gap above target is an opportunity —
    /// "Gap to target / mo", available, not captured, in the warn tone;
    /// sample data carries no dollars at all (NS3 C3). Never "savings",
    /// never green. There is no "$ under industry" tile any more
    /// (Benchmarking #34, BM3-15): the published figure includes benefits
    /// and this restaurant's labor is wages from its shifts, so dollars
    /// "under" it were not a like-for-like gap, and they carried no action.
    /// The industry parameters stay so every caller compiles unchanged.
    static func laborMoneyTiles(isLive: Bool, monthly: Double, annual: Double,
                                vsIndustryMonthly: Double, vsIndustryAnnual: Double,
                                industryText: String?, periodDays: Int?) -> [MoneyTile] {
        guard isLive else { return [] }
        let window = periodDays.map { "from a \($0)-day window" } ?? "from this window"
        if monthly > 0 {
            var tiles = [MoneyTile(value: monthly, label: "Gap to target / mo",
                                   sublabel: "available, not captured", tone: .warn)]
            if annual > 0 {
                tiles.append(MoneyTile(value: annual, label: "Per year \u{00B7} projected",
                                       sublabel: window, tone: .neutral))
            }
            return tiles
        }
        return []
    }

    // MARK: - Diagnoses

    /// The heading over a diagnosis — what the evidence points to, never
    /// "why this is happening" (NS1 H10).
    static let diagnosisHeading = "What the evidence points to"

    /// The model's `expected_outcome` is its forecast IF its cause is right,
    /// so it is headed that way.
    static let expectedOutcomeLabel = "If this is the cause, you\u{2019}d expect\u{2026}"

    /// The outcome to show, or nil: a line stated as a certainty
    /// ("guaranteed", "will eliminate") is not shown.
    static func expectedOutcome(_ text: String?) -> String? {
        guard let t = text?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty else { return nil }
        let banned = ["guarantee", "definitely", "certainly", "will eliminate"]
        let lower = t.lowercased()
        return banned.contains(where: { lower.contains($0) }) ? nil : t
    }

    /// "If ignored:" stated a prediction as fact; it is a risk.
    static let ifIgnoredLabel = "Risk if left alone: "

    // MARK: - Recoverable / at-stake figures

    /// The DSR food block's drivers total: an opportunity, with its basis
    /// and "partial" when a source was missing (NS1 #7). Nil when absent.
    static func dsrAtStakeLine(amount: String?, basis: String?, complete: Bool?) -> String? {
        guard let amount else { return nil }
        var s = "At stake each month: \(amount) \u{2014} an opportunity, not captured"
        if let b = basis?.trimmingCharacters(in: .whitespacesAndNewlines), !b.isEmpty {
            s += " (\(b.hasSuffix(".") ? String(b.dropLast()) : b))"
        }
        if complete == false { s += " \u{00B7} partial" }
        return s + "."
    }

    // MARK: - Server labels read as the owner reads them

    /// A pulse-chip or KPI label as shown. The server's food "recoverable"
    /// label sits on a one-week waste projection — an opportunity — and its
    /// labor "Recoverable" on the gap above target, so both read as what
    /// they are (NS1 H11, NS3 food #6). Anything else passes through.
    static func displayLabel(_ label: String) -> String {
        switch label.trimmingCharacters(in: .whitespaces).lowercased() {
        case "recoverable", "recoverable / mo", "recoverable/mo": return "opportunity / mo"
        default: return label
        }
    }

    // MARK: - Schedule progress

    /// The first step names last year's same days only when that input
    /// exists (NS1 #21).
    static func firstScheduleStep(lastYearAvailable: Bool) -> String {
        lastYearAvailable ? "Reading last year's same days" : "Reading your shift and sales history"
    }
}
