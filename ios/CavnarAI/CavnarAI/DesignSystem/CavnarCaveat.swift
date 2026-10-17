import SwiftUI

/// The banner shown above an AI passage the app cannot fully stand behind:
/// example data standing in for a restaurant that hasn't connected inventory,
/// or figures the backend could not trace back to the data it handed the
/// model. Both used to render exactly like a verified analysis, which is the
/// whole problem — an owner had no way to tell a measured number from one the
/// model produced.
struct CavnarCaveat: View {
    let title: String
    /// Empty draws the title alone — a caveat whose title already says it
    /// all needs no second sentence (text-reduction round 10/17/26).
    /// Every existing `CavnarCaveat(title:detail:)` call is unchanged.
    var detail: String = ""

    var body: some View {
        // Secondary (14) throughout, the icon sized by the same font so it
        // grows with the words (10/8/26: it was a fixed 14pt glyph).
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.cavnar(.secondary))
                .foregroundStyle(Color.cavnarAmber)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                Text(title)
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarAmber)
                if !detail.isEmpty {
                    Text(detail)
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 13)
        .padding(.vertical, 11)
        .background(Color.cavnarAmber.opacity(0.10), in: RoundedRectangle(cornerRadius: 9))
        .overlay(
            RoundedRectangle(cornerRadius: 9)
                .stroke(Color.cavnarAmber.opacity(0.28), lineWidth: 1)
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel(detail.isEmpty ? title : "\(title). \(detail)")
    }

    /// The two the app actually raises today.
    static var exampleData: CavnarCaveat {
        CavnarCaveat(
            title: "Example data",
            detail: "A sample pantry, not your numbers. Connect Toast or upload a count."
        )
    }

    /// A cached AI read served because a new one failed (B6#12).
    static func olderRead(_ note: String) -> CavnarCaveat {
        CavnarCaveat(title: "Older read", detail: note)
    }

    /// A module read the server could not write, in the server's own words
    /// (a budget stop, an outage, a refused read — InsightRefresh.follow).
    static func readUnavailable(_ message: String) -> CavnarCaveat {
        CavnarCaveat(title: "Read unavailable", detail: message)
    }

    static func unverifiedFigures(_ figures: [String]) -> CavnarCaveat {
        let detail: String
        if figures.isEmpty {
            // One short warning each (text-reduction round 10/17/26): what
            // didn't trace, and check first — never softened.
            detail = "Not every figure here traces to your data. Check before acting."
        } else if figures.count == 1 {
            detail = "\(figures[0]) doesn't trace to your data. Check before acting."
        } else {
            detail = "\(figures.count) figures (\(figures.prefix(3).joined(separator: ", "))) don't trace to your data. Check before acting."
        }
        return CavnarCaveat(title: "Unverified numbers", detail: detail)
    }

    /// A cause the read named that no measured signal backs (H2,
    /// ai_guard.unsupported_causes): the "because…" the model supplied
    /// itself. The figures may trace; the why does not.
    static func unverifiedCauses(_ causes: [String]) -> CavnarCaveat {
        let named = causes.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        let detail: String
        // Said as what it is (9/26/26): Cavnar AI's hypothesis, not a
        // finding the app disputes with itself.
        if named.isEmpty {
            detail = "A reason nothing measured confirms yet. Check before acting."
        } else if named.count == 1 {
            detail = "\u{201C}\(named[0])\u{201D} is Cavnar AI\u{2019}s guess: nothing measured links these yet. Check before acting."
        } else {
            detail = "\(named.count) reasons (\(named.prefix(2).map { "\u{201C}\($0)\u{201D}" }.joined(separator: ", "))) are Cavnar AI\u{2019}s guesses: nothing measured links them yet. Check before acting."
        }
        return CavnarCaveat(title: "Hypothesis", detail: detail)
    }

    /// A name the passage used that was never in the data behind it.
    ///
    /// The figure check cannot catch this — a fabricated guest is not a
    /// figure — and it is the most damaging thing an AI read of someone's
    /// reviews can get wrong, because the entire premise of the panel is
    /// that Cavnar has actually read them.
    static func unverifiedNames(_ names: [String]) -> CavnarCaveat {
        let who = names.isEmpty ? "a name here"
            : "\(names.prefix(2).joined(separator: ", "))"
        return CavnarCaveat(
            title: "Unverified name",
            detail: "No reviewer in your data matches \(who). Open the review before acting."
        )
    }

    /// The last read Cavnar completed, rather than a fresh one. Shown rather
    /// than hidden: a stale read beats no read, but it must not be presented
    /// as current — the whole reason ai_guard.freshness exists.
    static func olderRead(asOf: String?) -> CavnarCaveat {
        CavnarCaveat(
            title: "Older read",
            // The stale path runs because writing a new read failed and
            // nothing is queued: promise only the retry that does happen
            // (M-33). `asOf` arrives M/D/YY from the server (M-26).
            detail: asOf.map { "From \($0). A new one couldn\u{2019}t be written \u{2014} Cavnar AI tries again next time this opens." }
                ?? "A new one couldn\u{2019}t be written \u{2014} Cavnar AI tries again next time this opens."
        )
    }
}
