import SwiftUI

/// One owner answer to a model-written recommendation. The three the phone
/// offers, and exactly what each one sends to POST /mobile/api/recs/event —
/// the one door into rec_ledger for web and iOS (strategy_routes
/// `_do_rec_event`). The web twin is `client_api.rec_controls_html`.
enum RecAnswer: String, CaseIterable, Sendable {
    /// "Done" — the owner did it. Silenced for good, and the module's
    /// insight is told not to suggest it again in other words.
    case completed
    /// "Not for us" — sent as `dismissed` with `kind: not_for_us`, the
    /// silence that keeps the recommendation from coming back.
    case notForUs
    /// "Track" — starts a before-and-after tracker on the module's own
    /// metric (strategy_routes.REC_TRACK_METRICS). Offered only where one
    /// exists — see `defaults(for:)`.
    case accepted

    /// The modules whose recommendations Track can measure. Intel has no
    /// metric of its own, so it offers Done and Not for us only (M-8).
    static let trackableModules: Set<String> = ["reviews", "food", "marketing", "labor"]

    static func defaults(for module: String) -> [RecAnswer] {
        trackableModules.contains(module) ? allCases : [.completed, .notForUs]
    }

    /// The ledger's event name.
    var event: String {
        switch self {
        case .completed: return "completed"
        case .notForUs:  return "dismissed"
        case .accepted:  return "accepted"
        }
    }

    /// Only a dismissal carries a kind.
    var kind: String? { self == .notForUs ? "not_for_us" : nil }

    var label: String {
        switch self {
        case .completed: return "Done"
        case .notForUs:  return "Not for us"
        case .accepted:  return "Track"
        }
    }

    /// The muted line that replaces the buttons once the server has it —
    /// used only when the server sends no `message` of its own (an older
    /// server). The server's sentence says what actually happened: a
    /// tracker on a named metric, or only hidden.
    var confirmation: String {
        switch self {
        case .completed: return "Done \u{2014} Cavnar won\u{2019}t suggest it again"
        case .notForUs:  return "Noted \u{2014} it won\u{2019}t come back"
        case .accepted:  return "Noted \u{2014} hidden for 14 days"
        }
    }

    var accessibilityHint: String {
        switch self {
        case .completed: return "Marks this done so it isn't suggested again"
        case .notForUs:  return "Stops this recommendation coming back"
        case .accepted:  return "Cavnar compares the numbers before and after you act on it"
        }
    }
}

extension APIClient {
    struct RecEventBody: Encodable, Equatable {
        let key: String
        let event: String
        let surface: String
        let module: String
        /// Omitted from the JSON when nil (synthesized encodeIfPresent).
        let kind: String?
    }

    struct RecEventResponse: Decodable {
        let ok: Bool
        /// False when the server already had this answer; still a success
        /// from the owner's point of view.
        let recorded: Bool?
        let error: String?
        /// What the answer actually did, in the owner's words (a tracker on
        /// a named metric, or only hidden). Shown instead of `confirmation`.
        let message: String?
    }

    /// Records the owner's answer to one recommendation. `module` defaults
    /// to the surface — every screen on the phone answers for its own module.
    func answerRecommendation(key: String, answer: RecAnswer, surface: String,
                              module: String? = nil) async throws -> RecEventResponse {
        try await send(
            "/mobile/api/recs/event", method: .post,
            body: RecEventBody(key: key, event: answer.event, surface: surface,
                               module: module ?? surface, kind: answer.kind),
            // An answer is a write the owner watches land; never replayed on
            // a guess.
            retryTransient: false)
    }
}

/// Done / Not for us / Track under one recommendation — the phone's copy of
/// the web's `.rec-ans` row and the same small-text-button look as Home's
/// recommendation answers. After the server records the answer, the buttons
/// give way to one muted sentence saying what happens next.
///
/// Reused by Reviews, Food Cost, Marketing and Intel. A screen that only
/// offers some of the answers (a reprice card's "Not for us" beside its own
/// primary button) passes `answers`.
struct RecAnswerRow: View {
    let key: String
    let surface: String
    var module: String? = nil
    /// nil = the defaults for this row's module (`RecAnswer.defaults`).
    var answers: [RecAnswer]? = nil
    /// Told once the server has the answer — lets a caller hide the action
    /// the answer belongs to (a reprice card's Set button).
    var onAnswered: ((RecAnswer) -> Void)? = nil
    var client: APIClient = .shared

    /// Keyed by the rec key, not just "answered": a row inside a ForEach
    /// keyed by position keeps its @State when a reload removes the line
    /// above it, and the next recommendation must not inherit the answer.
    private struct Outcome: Equatable { let key: String; let answer: RecAnswer; let message: String? }
    @State private var outcome: Outcome?
    @State private var busy = false
    @State private var errorMessage: String?

    private var answered: Outcome? {
        guard let outcome, outcome.key == key else { return nil }
        return outcome
    }

    private var shownAnswers: [RecAnswer] {
        answers ?? RecAnswer.defaults(for: module ?? surface)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let answered {
                HStack(spacing: 6) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 10, weight: .bold))
                        .accessibilityHidden(true)
                    Text(answered.message ?? answered.answer.confirmation)
                        .font(.cavnarBody(12.5, weight: 500))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .foregroundStyle(Color.cavnarInk3)
                .transition(.opacity)
                .accessibilityElement(children: .combine)
            } else {
                HStack(spacing: 16) {
                    ForEach(shownAnswers, id: \.self) { answer in
                        Button {
                            Haptic.light()
                            Task { await submit(answer) }
                        } label: {
                            Text(answer.label)
                                .font(.cavnarBody(12.5, weight: answer == .accepted ? 700 : 600))
                                .foregroundStyle(answer == .accepted ? Color.cavnarEmber2 : Color.cavnarInk3)
                                .padding(.vertical, 4)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(busy)
                        .accessibilityHint(answer.accessibilityHint)
                    }
                }
                .opacity(busy ? 0.45 : 1)
                if let errorMessage {
                    Text(errorMessage)
                        .font(.cavnarBody(12))
                        .foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .animation(.easeOut(duration: 0.2), value: outcome)
    }

    @MainActor
    private func submit(_ answer: RecAnswer) async {
        guard !busy else { return }
        busy = true
        errorMessage = nil
        defer { busy = false }
        do {
            let r = try await client.answerRecommendation(key: key, answer: answer,
                                                          surface: surface, module: module)
            guard r.ok else {
                errorMessage = r.error ?? "Couldn\u{2019}t save that."
                return
            }
            Haptic.success()
            outcome = Outcome(key: key, answer: answer, message: r.message)
            onAnswered?(answer)
        } catch is CancellationError {
            // The screen went away mid-send — nothing to say.
        } catch let error as APIClient.APIError {
            // A write whose answer was lost may still have landed; say so
            // rather than inviting a blind second tap (DESIGN_SYSTEM §10).
            errorMessage = error.mayHaveReachedServer && error.status == nil
                ? "Couldn\u{2019}t confirm that saved \u{2014} reopen this screen to check."
                : error.message
        } catch {
            errorMessage = "Couldn\u{2019}t save that."
        }
    }
}
