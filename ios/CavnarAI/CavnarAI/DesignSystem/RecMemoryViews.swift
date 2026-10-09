import SwiftUI

// What a recommendation remembers, drawn on the card (memory round 9/29/26):
// the owner's earlier answer, a delegate's decline, a re-test, a caution
// from another module, and a conflict with other advice the owner settles
// in one tap. One set of views, so Home's cards, Needs attention, the
// brief, the one-thing hero and the nightly report say it the same way
// (DESIGN_SYSTEM §12 → "What a card remembers").

// MARK: - History lines

/// "You passed on this on 3/12/26 ($120/mo then)." / "Dana passed on this:
/// already doing it (9/28/26)." / the re-test line — each the server's own
/// sentence, muted, with a small glyph saying which kind of memory it is.
/// Draws nothing when there is nothing to say.
struct RecMemoryNote: View {
    var previous: RecPreviousAnswer? = nil
    var delegate: RecDelegateAnswer? = nil
    var retest: Bool = false
    /// One line each, for a fixed-height card; the full text otherwise.
    var compact: Bool = false

    var body: some View {
        let rows = Self.rows(previous: previous, delegate: delegate, retest: retest)
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 3) {
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Image(systemName: row.symbol)
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Color.cavnarInk3)
                            .accessibilityHidden(true)
                        HomeMixedText.make(row.text, size: CavnarType.caption, weight: 500, color: .cavnarInk3)
                            .lineLimit(compact ? 1 : nil)
                            .fixedSize(horizontal: false, vertical: !compact)
                    }
                }
            }
            .accessibilityElement(children: .combine)
        }
    }

    struct Row: Equatable {
        let symbol: String
        let text: String
    }

    /// The rows in the order drawn — the owner's own answer first, then a
    /// delegate's, then the re-test (said only when no earlier answer is).
    static func rows(previous: RecPreviousAnswer?, delegate: RecDelegateAnswer?, retest: Bool) -> [Row] {
        var out: [Row] = []
        if let p = previous { out.append(Row(symbol: "clock.arrow.circlepath", text: p.text)) }
        if let d = delegate { out.append(Row(symbol: "person.fill", text: d.text)) }
        if retest && previous == nil { out.append(Row(symbol: "arrow.clockwise", text: RecMemoryLines.retestLine)) }
        return out
    }
}

/// What another module knows that argues against the card — "Tuesday had
/// three service complaints last month" on a trim (staffing_signals.
/// trim_guard, M3). Amber, never red: the card stands, ranked lower.
struct RecCautionLine: View {
    let text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(Color.cavnarAmber)
                .accessibilityHidden(true)
            HomeMixedText.make(text, size: CavnarType.caption, weight: 600, color: .cavnarAmber)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Caution: \(text)")
    }
}

// MARK: - The conflict

extension APIClient {
    struct ConflictBody: Encodable, Equatable {
        let conflict: String
        let prefer: String
    }

    struct ConflictResponse: Decodable {
        let ok: Bool
        let message: String?
        let error: String?
    }

    /// The owner's choice between advice that pulls against itself (POST
    /// /mobile/api/recs/conflict {conflict, prefer}): stored as a decision,
    /// so the same conflict resolves the same way next time.
    func settleConflict(_ conflict: RecConflict, prefer: String) async throws -> ConflictResponse {
        try await send(conflict.mobilePath, method: .post,
                       body: ConflictBody(conflict: conflict.id, prefer: prefer),
                       retryTransient: false)
    }
}

/// "Pulls against: Fill Tuesday with a text to regulars" — the conflict a
/// card carries, the server's why, and one tap per way to settle it. The
/// card is never removed from under the owner: the choice is recorded,
/// the server's sentence replaces the buttons, and the caller reloads so
/// the card the owner chose against is held from then on.
struct RecConflictPanel: View {
    let conflict: RecConflict
    /// Told once the server has the choice, so the screen can re-read.
    var onSettled: () -> Void = {}
    var client: APIClient = .shared

    @State private var busy = false
    @State private var settled: String?
    @State private var errorMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            CavnarKicker(Self.kicker(conflict))
            if let why = conflict.why {
                HomeMixedText.make(why, size: CavnarType.secondary, weight: 500, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let settled {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 10, weight: .bold))
                        .accessibilityHidden(true)
                    HomeMixedText.make(settled, size: CavnarType.caption, weight: 600, color: .cavnarGreen)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .foregroundStyle(Color.cavnarGreen)
                .transition(.opacity)
            } else if conflict.isAnswerable {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(conflict.choose) { choice in
                        Button {
                            Haptic.light()
                            Task { await settle(choice) }
                        } label: {
                            HomeMixedText.make(Self.buttonLabel(choice), size: CavnarType.secondary, weight: 700,
                                               color: .cavnarEmber2)
                                .multilineTextAlignment(.leading)
                                .frame(minHeight: 44, alignment: .leading)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(busy)
                        .accessibilityHint("Settles the conflict this way from now on")
                    }
                }
                if busy {
                    CavnarSkeletonBar(height: 3)
                        .accessibilityLabel("Saving your choice")
                }
                if let errorMessage {
                    Text(errorMessage)
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarAmber.opacity(0.07), in: RoundedRectangle(cornerRadius: CavnarRadius.control))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
            .strokeBorder(Color.cavnarAmber.opacity(0.3), lineWidth: 1))
        .animation(.easeOut(duration: 0.2), value: settled)
    }

    /// "Pulls against: Fill Tuesday …" when the other side is a card, else
    /// the server's name for the conflict, else a plain "Pulls against
    /// other advice".
    static func kicker(_ c: RecConflict) -> String {
        if let w = c.with { return "Pulls against: " + w }
        if let l = c.label { return l }
        return "Pulls against other advice"
    }

    /// "Keep “Trim Tuesday staffing”" for a card's own title, or the
    /// server's short label ("Keep it", "Hold it") as it is.
    static func buttonLabel(_ choice: RecConflict.Choice) -> String {
        let l = choice.label
        let short = l.count <= 14 || l.lowercased().hasPrefix("keep") || l.lowercased().hasPrefix("hold")
        return short ? l : "Keep \u{201C}\(l)\u{201D}"
    }

    @MainActor
    private func settle(_ choice: RecConflict.Choice) async {
        guard !busy else { return }
        busy = true
        errorMessage = nil
        defer { busy = false }
        do {
            let r = try await client.settleConflict(conflict, prefer: choice.signature)
            guard r.ok else {
                errorMessage = r.error ?? "Couldn\u{2019}t save that."
                return
            }
            Haptic.success()
            settled = r.message ?? "Noted \u{2014} Cavnar AI will settle this the same way next time"
            onSettled()
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t save that."
        }
    }
}

// MARK: - Why did you undo it?

/// The question an undo of an automatic send earns (memory round 9/29/26,
/// M1 "undo"): an undone schedule publish or supplier order counts against
/// the trust that queued it, and the cancel answer carries `ask_why`
/// {route, options[{code, label}]} — asked once, one tap, never typed.
struct UndoAskWhy: Decodable, Hashable, Sendable {
    struct Option: Decodable, Hashable, Sendable, Identifiable {
        let code: String
        let label: String
        var id: String { code }
    }
    let route: String
    let options: [Option]

    enum CodingKeys: String, CodingKey { case route, options }

    init(route: String, options: [Option]) { self.route = route; self.options = options }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        route = try c.decode(String.self, forKey: .route)
        options = ((try? c.decodeIfPresent(HomeLenientListDecodable<Option>.self, forKey: .options)) ?? nil)?.items ?? []
    }

    /// The server names the shared route ("/actions/12/why"); the phone
    /// posts to its twin under /mobile/api.
    var mobilePath: String { route.hasPrefix("/mobile/api/") ? route : "/mobile/api" + route }
}

/// POST /mobile/api/actions/<id>/cancel's answer: the server's sentence for
/// what the undo did, and the question it asks.
struct UndoResponse: Decodable {
    let ok: Bool
    var error: String? = nil
    var message: String? = nil
    var askWhy: UndoAskWhy? = nil

    enum CodingKeys: String, CodingKey {
        case ok, error, message
        case askWhy = "ask_why"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = ((try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? nil) ?? false
        error = (try? c.decodeIfPresent(String.self, forKey: .error)) ?? nil
        message = (try? c.decodeIfPresent(String.self, forKey: .message)) ?? nil
        askWhy = (try? c.decodeIfPresent(UndoAskWhy.self, forKey: .askWhy)) ?? nil
    }
}

extension APIClient {
    struct UndoWhyBody: Encodable, Equatable {
        let reasonCode: String
        enum CodingKeys: String, CodingKey { case reasonCode = "reason_code" }
    }

    /// The undo of a queued send (delayed.cancel).
    func undoQueuedAction(_ id: Int) async throws -> UndoResponse {
        try await send("/mobile/api/actions/\(id)/cancel", method: .post, body: [String: String](),
                       retryTransient: false)
    }

    /// The owner's one-tap answer to "why did you undo it?". Quiet on failure:
    /// the undo itself already stands.
    @discardableResult
    func answerUndoWhy(_ ask: UndoAskWhy, code: String) async -> Bool {
        let r: OKResponse? = try? await send(ask.mobilePath, method: .post, body: UndoWhyBody(reasonCode: code),
                                             hapticOnError: false, retryTransient: false)
        return r?.ok == true
    }
}

extension View {
    /// "Why did you undo it?" — the server's reasons as one-tap buttons,
    /// asked right after an undo that counts against earned trust.
    func undoWhyDialog(_ ask: Binding<UndoAskWhy?>, client: APIClient = .shared) -> some View {
        confirmationDialog("Why did you undo it?",
                           isPresented: Binding(get: { ask.wrappedValue != nil },
                                                set: { if !$0 { ask.wrappedValue = nil } }),
                           titleVisibility: .visible,
                           presenting: ask.wrappedValue) { a in
            ForEach(a.options) { option in
                Button(option.label) {
                    Task { await client.answerUndoWhy(a, code: option.code) }
                }
            }
            Button("Skip", role: .cancel) {}
        } message: { _ in
            Text("Cavnar AI waits for a few clean runs before doing this on its own again.")
        }
    }
}
