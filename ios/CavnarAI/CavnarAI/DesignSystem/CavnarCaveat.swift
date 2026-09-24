import SwiftUI

/// The banner shown above an AI passage the app cannot fully stand behind:
/// example data standing in for a restaurant that hasn't connected inventory,
/// or figures the backend could not trace back to the data it handed the
/// model. Both used to render exactly like a verified analysis, which is the
/// whole problem — an owner had no way to tell a measured number from one the
/// model produced.
struct CavnarCaveat: View {
    let title: String
    let detail: String

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Color.cavnarAmber)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.cavnarBody(12.5, weight: 700))
                    .foregroundStyle(Color.cavnarAmber)
                Text(detail)
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 9)
        .background(Color.cavnarAmber.opacity(0.10), in: RoundedRectangle(cornerRadius: 9))
        .overlay(
            RoundedRectangle(cornerRadius: 9)
                .stroke(Color.cavnarAmber.opacity(0.28), lineWidth: 1)
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title). \(detail)")
    }

    /// The two the app actually raises today.
    static var exampleData: CavnarCaveat {
        CavnarCaveat(
            title: "Example data",
            detail: "This is a sample pantry, not your numbers. Connect Toast or upload a count to see your own."
        )
    }

    /// A cached AI read served because a new one failed (B6#12).
    static func olderRead(_ note: String) -> CavnarCaveat {
        CavnarCaveat(title: "Older read", detail: note)
    }

    static func unverifiedFigures(_ figures: [String]) -> CavnarCaveat {
        let detail: String
        if figures.isEmpty {
            detail = "Cavnar couldn't trace every figure here back to your data. Check before acting on them."
        } else if figures.count == 1 {
            detail = "Cavnar couldn't trace \(figures[0]) back to your data. Check before acting on it."
        } else {
            detail = "Cavnar couldn't trace \(figures.count) figures here (\(figures.prefix(3).joined(separator: ", "))) back to your data. Check before acting on them."
        }
        return CavnarCaveat(title: "Unverified numbers", detail: detail)
    }

    /// A cause the read named that no measured signal backs (H2,
    /// ai_guard.unsupported_causes): the "because…" the model supplied
    /// itself. The figures may trace; the why does not.
    static func unverifiedCauses(_ causes: [String]) -> CavnarCaveat {
        let named = causes.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        let detail: String
        if named.isEmpty {
            detail = "Cavnar couldn\u{2019}t match the reason this read gives to anything in your data. Treat the why as a guess until you\u{2019}ve checked it."
        } else if named.count == 1 {
            detail = "Cavnar couldn\u{2019}t match \u{201C}\(named[0])\u{201D} to anything in your data. Treat that reason as a guess until you\u{2019}ve checked it."
        } else {
            detail = "Cavnar couldn\u{2019}t match \(named.count) reasons here (\(named.prefix(2).map { "\u{201C}\($0)\u{201D}" }.joined(separator: ", "))) to anything in your data. Treat them as guesses until you\u{2019}ve checked."
        }
        return CavnarCaveat(title: "Unverified cause", detail: detail)
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
            detail: "Cavnar couldn't match \(who) to a reviewer in your data. Open the review before acting on this."
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
            detail: asOf.map { "This is the last read Cavnar finished, from \($0). A new one couldn\u{2019}t be written just now \u{2014} Cavnar tries again the next time this opens." }
                ?? "This is the last read Cavnar finished. A new one couldn\u{2019}t be written just now \u{2014} Cavnar tries again the next time this opens."
        )
    }
}
