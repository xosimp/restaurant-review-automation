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
    /// silence that keeps the recommendation from coming back, and the
    /// owner's one-tap reason (`reason_code`, RecReason).
    case notForUs
    /// "Track" — starts a before-and-after tracker on the module's own
    /// metric (strategy_routes.REC_TRACK_METRICS). Offered only where one
    /// exists — see `defaults(for:)`.
    case accepted

    /// The modules whose recommendations Track can measure — the keys of
    /// strategy_routes.REC_TRACK_METRICS. Intel has no metric of its own,
    /// and neither has Marketing: a post has no honest metric, and Track
    /// there started a sales tracker credited to Labor (rec-ROI #11). Both
    /// offer Done and Not for us only.
    static let trackableModules: Set<String> = ["reviews", "food", "labor"]

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
        case .notForUs:  return "Asks why, then stops this recommendation coming back"
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
        var reasonCode: String? = nil

        enum CodingKeys: String, CodingKey {
            case key, event, surface, module, kind
            case reasonCode = "reason_code"
        }
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
        /// Exactly one of these on Track, and on Done for a recommendation
        /// that carries a metric (API_REFERENCE → Tracker-start replies).
        let tracker: RecTracker?
        let trackerRefused: RecTrackerRefused?

        enum CodingKeys: String, CodingKey {
            case ok, recorded, error, message, tracker
            case trackerRefused = "tracker_refused"
        }
    }

    /// Records the owner's answer to one recommendation. `module` defaults
    /// to the surface — every screen on the phone answers for its own module.
    func answerRecommendation(key: String, answer: RecAnswer, surface: String,
                              module: String? = nil, reasonCode: String? = nil) async throws -> RecEventResponse {
        try await send(
            "/mobile/api/recs/event", method: .post,
            body: RecEventBody(key: key, event: answer.event, surface: surface,
                               module: module ?? surface, kind: answer.kind,
                               reasonCode: answer == .notForUs ? reasonCode : nil),
            // An answer is a write the owner watches land; never replayed on
            // a guess.
            retryTransient: false)
    }

    /// `evidence_viewed` — the owner opened a recommendation's reasoning
    /// (rec-ROI #38). Fire and forget; a failure changes nothing on screen.
    func recordEvidenceViewed(key: String, surface: String, module: String? = nil) async {
        let _: OKResponse? = try? await send(
            "/mobile/api/recs/event", method: .post,
            body: RecEventBody(key: key, event: "evidence_viewed", surface: surface,
                               module: module ?? surface, kind: nil),
            hapticOnError: false, retryTransient: false)
    }
}

/// Once per recommendation per launch: opening the same "Why" three times
/// is one look at the evidence, not three.
@MainActor
enum RecEvidenceLog {
    private static var sent: Set<String> = []

    static func viewed(key: String?, surface: String, module: String? = nil, client: APIClient = .shared) {
        guard let key, !key.isEmpty, sent.insert(key).inserted else { return }
        Task { await client.recordEvidenceViewed(key: key, surface: surface, module: module) }
    }
}

// MARK: - The reason picker

/// "Not for us" asks why — the six reasons, one tap, in a confirmation
/// dialog (never a text field the owner has to type into on the floor).
/// Used by every Not for us on the phone: RecAnswerRow, Home's cards, the
/// second hide on Needs attention, and Ask's "Not now". `skipLabel` adds an
/// answer that goes ahead without a reason ("Just hide it", "Just not now").
struct RecReasonDialog: ViewModifier {
    @Binding var isPresented: Bool
    var title: String
    var message: String?
    var skipLabel: String?
    var onSkip: (() -> Void)?
    let onPick: (RecReason) -> Void

    func body(content: Content) -> some View {
        content.confirmationDialog(title, isPresented: $isPresented, titleVisibility: .visible) {
            ForEach(RecReason.allCases) { reason in
                Button(reason.label) { onPick(reason) }
            }
            if let skipLabel, let onSkip {
                Button(skipLabel) { onSkip() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            if let message { Text(message) }
        }
    }
}

extension View {
    func recReasonDialog(isPresented: Binding<Bool>,
                         title: String = "Why isn\u{2019}t it for you?",
                         message: String? = "One tap tells Cavnar AI what to stop suggesting.",
                         skipLabel: String? = nil,
                         onSkip: (() -> Void)? = nil,
                         onPick: @escaping (RecReason) -> Void) -> some View {
        modifier(RecReasonDialog(isPresented: isPresented, title: title, message: message,
                                 skipLabel: skipLabel, onSkip: onSkip, onPick: onPick))
    }
}

/// Done / Not for us / Track under one recommendation — the phone's copy of
/// the web's `.rec-ans` row and the same small-text-button look as Home's
/// recommendation answers. After the server records the answer, the buttons
/// give way to one muted sentence saying what happens next, and — when a
/// tracker started or could not — a second line saying what is measured
/// until when, or why nothing is.
///
/// Reused by Reviews, Food Cost, Marketing, Intel, Labor, the Daily Report,
/// Home's cross-module lines and loss flags, Ask's suggestions, the content
/// calendar and the AI-visibility roadmap. A screen that only offers some of
/// the answers (a reprice card's "Not for us" beside its own primary
/// button) passes `answers`.
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
    private struct Outcome: Equatable {
        let key: String
        let answer: RecAnswer
        let message: String?
        let trackerLine: String?
    }
    @State private var outcome: Outcome?
    @State private var busy = false
    @State private var errorMessage: String?
    @State private var askingReason = false

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
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 10, weight: .bold))
                        .accessibilityHidden(true)
                    HomeMixedText.make(answered.message ?? answered.answer.confirmation,
                                       size: 12.5, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .foregroundStyle(Color.cavnarInk3)
                .transition(.opacity)
                .accessibilityElement(children: .combine)
                if let line = answered.trackerLine {
                    RecTrackerLine(text: line)
                        .transition(.opacity)
                }
            } else {
                HStack(spacing: 16) {
                    ForEach(shownAnswers, id: \.self) { answer in
                        Button {
                            Haptic.light()
                            if answer == .notForUs {
                                askingReason = true
                            } else {
                                Task { await submit(answer) }
                            }
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
        .recReasonDialog(isPresented: $askingReason) { reason in
            Task { await submit(.notForUs, reason: reason) }
        }
    }

    @MainActor
    private func submit(_ answer: RecAnswer, reason: RecReason? = nil) async {
        guard !busy else { return }
        busy = true
        errorMessage = nil
        defer { busy = false }
        do {
            let r = try await client.answerRecommendation(key: key, answer: answer, surface: surface,
                                                          module: module, reasonCode: reason?.code)
            guard r.ok else {
                errorMessage = r.error ?? "Couldn\u{2019}t save that."
                return
            }
            Haptic.success()
            outcome = Outcome(key: key, answer: answer, message: r.message,
                              trackerLine: RecTrackerNote.extraLine(message: r.message, tracker: r.tracker,
                                                                    refused: r.trackerRefused))
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

/// "Measuring labor % until 10/21/26" (or why nothing is measured) under an
/// answer — a gauge glyph in ember2 so it reads as the product now watching,
/// the sentence in the muted body face with its figures in Space Grotesk.
struct RecTrackerLine: View {
    let text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: "gauge.with.dots.needle.33percent")
                .font(.system(size: 10.5, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber2)
                .accessibilityHidden(true)
            HomeMixedText.make(text, size: 12.5, weight: 600, color: .cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
    }
}
