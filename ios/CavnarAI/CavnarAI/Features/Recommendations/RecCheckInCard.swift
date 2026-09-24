import SwiftUI

// The check-in (#21), "What worked for you" (#28) and the two writes the
// recommendation record makes besides an answer: the check-in itself and
// "Stop measuring" (#35). Shared by Home and the Recommendations screen.

extension APIClient {
    struct RecCheckInBody: Encodable, Equatable {
        let key: String
        /// The tracker (outcome) the card is about, sent with every
        /// check-in so the answer lands on THIS result — not on whichever
        /// episode of the key is newest (a Track re-shown after its silence
        /// opens a new episode with no tracker behind it).
        let trackerId: Int
        let didIt: String
        let conditionsChanged: Bool
        let surface: String

        enum CodingKeys: String, CodingKey {
            case key, surface
            case trackerId = "tracker_id"
            case didIt = "did_it"
            case conditionsChanged = "conditions_changed"
        }
    }

    struct RecCheckInResponse: Decodable {
        struct Checkin: Decodable {
            struct Attribution: Decodable {
                /// "yes" / "partly" / "no" — the answer as the server stored
                /// it. Declared a Bool, every real reply failed to decode and
                /// no check-in ever read as saved. A Bool (an older build)
                /// is still read: true = "yes", false = "no".
                let implemented: String?
                let confounded: Bool?
                let discount: Bool?

                enum CodingKeys: String, CodingKey { case implemented, confounded, discount }

                init(from decoder: Decoder) throws {
                    let c = try decoder.container(keyedBy: CodingKeys.self)
                    if let s = try? c.decodeIfPresent(String.self, forKey: .implemented) {
                        implemented = s
                    } else if let b = try? c.decodeIfPresent(Bool.self, forKey: .implemented) {
                        implemented = b ? "yes" : "no"
                    } else {
                        implemented = nil
                    }
                    confounded = try? c.decodeIfPresent(Bool.self, forKey: .confounded)
                    discount = try? c.decodeIfPresent(Bool.self, forKey: .discount)
                }
            }
            let didIt: String?
            let conditionsChanged: Bool?
            let trackerId: Int?
            let note: String?
            let attribution: Attribution?
            enum CodingKeys: String, CodingKey {
                case attribution, note
                case didIt = "did_it"
                case conditionsChanged = "conditions_changed"
                case trackerId = "tracker_id"
            }

            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                didIt = try? c.decodeIfPresent(String.self, forKey: .didIt)
                conditionsChanged = try? c.decodeIfPresent(Bool.self, forKey: .conditionsChanged)
                trackerId = try? c.decodeIfPresent(Int.self, forKey: .trackerId)
                note = try? c.decodeIfPresent(String.self, forKey: .note)
                attribution = try? c.decodeIfPresent(Attribution.self, forKey: .attribution)
            }
        }
        let ok: Bool
        let recorded: Bool?
        let error: String?
        /// Read leniently: an odd `checkin` block never turns a saved
        /// answer into "couldn't save that".
        let checkin: Checkin?

        enum CodingKeys: String, CodingKey { case ok, recorded, error, checkin }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
            recorded = try? c.decodeIfPresent(Bool.self, forKey: .recorded)
            error = try? c.decodeIfPresent(String.self, forKey: .error)
            checkin = try? c.decodeIfPresent(Checkin.self, forKey: .checkin)
        }
    }

    /// POST /recs/checkin — "Did you make this change? Did anything else
    /// change?" The answer changes the result it is about: "no" stops it
    /// counting, "something else changed" caps its grade. `trackerId` is
    /// the result the card shows — required, so no caller can leave it out.
    func checkIn(key: String, trackerId: Int, didIt: String, conditionsChanged: Bool,
                 surface: String) async throws -> RecCheckInResponse {
        try await send("/mobile/api/recs/checkin", method: .post,
                       body: RecCheckInBody(key: key, trackerId: trackerId, didIt: didIt,
                                            conditionsChanged: conditionsChanged, surface: surface),
                       retryTransient: false)
    }

    /// POST /outcomes/<id>/abandon — stop measuring a change that was
    /// reversed or no longer applies. Only a tracker still running stops.
    func abandonOutcome(id: Int) async throws -> OKResponse {
        try await send("/mobile/api/outcomes/\(id)/abandon", method: .post, retryTransient: false)
    }
}

/// The answer pill — a one-tap answer to a question Cavnar asks (Yes /
/// Partly / No on a check-in, Yes / No on "Was this useful?"). Capsule,
/// ember hairline on a faint ember wash; the chosen one fills.
struct RecAnswerPillStyle: ButtonStyle {
    var selected: Bool = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.cavnarBody(13.5, weight: 700))
            .foregroundStyle(selected ? Color.white : Color.cavnarEmber2)
            .padding(.horizontal, 15)
            .padding(.vertical, 7)
            .background(Capsule().fill(selected ? Color.cavnarEmber : Color.cavnarEmber.opacity(0.1)))
            .overlay(Capsule().strokeBorder(Color.cavnarEmber.opacity(selected ? 0 : 0.35), lineWidth: 1))
            .scaleEffect(configuration.isPressed ? 0.96 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

/// "Did you make this change?" — asked once a tracked result has landed,
/// under the result it is about. One tap answers it: Yes / Partly / No,
/// carrying whether something else changed over those weeks (off unless the
/// owner ticks it). The result's label is refreshed after, because the
/// answer changes it.
struct RecCheckInCard: View {
    let outcome: RecOutcome
    /// The ledger surface the answer is recorded on ("home", "ios").
    let surface: String
    /// Reload whatever shows the result — its attribution now reads differently.
    var onAnswered: () async -> Void = {}
    var client: APIClient = .shared

    @State private var somethingElseChanged = false
    @State private var busy: String?
    @State private var thanks: String?
    @State private var errorMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: "checklist")
                    .font(.system(size: 11, weight: .bold))
                Text("CHECK IN")
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(1.4)
            }
            .foregroundStyle(Color.cavnarEmber2)

            HomeMixedText.make(outcome.resultLine ?? outcome.summary ?? outcome.title ?? "Your change was measured",
                               size: 14, weight: 600, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)

            if let thanks {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Image(systemName: "checkmark").font(.system(size: 10, weight: .bold))
                    Text(thanks).font(.cavnarBody(13, weight: 500))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .foregroundStyle(Color.cavnarInk3)
                .transition(.opacity)
            } else {
                Text("Did you make this change?")
                    .font(.cavnarBody(14.5, weight: 700))
                    .foregroundStyle(Color.cavnarInk2)
                HStack(spacing: 8) {
                    ForEach(RecCheckIn.answers, id: \.code) { answer in
                        Button {
                            Haptic.light()
                            Task { await send(answer.code) }
                        } label: {
                            if busy == answer.code {
                                CavnarShimmerText(text: answer.label, color: Color.cavnarEmber2)
                            } else {
                                Text(answer.label)
                            }
                        }
                        .buttonStyle(RecAnswerPillStyle())
                        .disabled(busy != nil)
                    }
                }
                Button {
                    Haptic.selection()
                    somethingElseChanged.toggle()
                } label: {
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: somethingElseChanged ? "checkmark.square.fill" : "square")
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(somethingElseChanged ? Color.cavnarEmber : Color.cavnarInk3)
                        Text("Something else changed these weeks too")
                            .font(.cavnarBody(13, weight: 500))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(busy != nil)
                .accessibilityLabel("Did anything else change these weeks?")
                .accessibilityValue(somethingElseChanged ? "Yes" : "No")
                if let errorMessage {
                    Text(errorMessage)
                        .font(.cavnarBody(12.5))
                        .foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .animation(.easeOut(duration: 0.2), value: thanks)
    }

    @MainActor
    private func send(_ didIt: String) async {
        guard busy == nil, let key = RecCheckIn.key(for: outcome) else { return }
        busy = didIt
        errorMessage = nil
        defer { busy = nil }
        do {
            let r = try await client.checkIn(key: key, trackerId: outcome.id, didIt: didIt,
                                             conditionsChanged: somethingElseChanged, surface: surface)
            guard r.ok else {
                errorMessage = r.error ?? "Couldn\u{2019}t save that."
                return
            }
            Haptic.success()
            thanks = Self.thanks(didIt: didIt, conditionsChanged: somethingElseChanged)
            await onAnswered()
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            errorMessage = error.mayHaveReachedServer && error.status == nil
                ? "Couldn\u{2019}t confirm that saved \u{2014} reopen this screen to check."
                : error.message
        } catch {
            errorMessage = "Couldn\u{2019}t save that."
        }
    }

    /// What the answer did to the result, in one sentence.
    static func thanks(didIt: String, conditionsChanged: Bool) -> String {
        if didIt == "no" { return "Noted \u{2014} this result no longer counts as one of Cavnar\u{2019}s." }
        if conditionsChanged { return "Noted \u{2014} with other changes in those weeks, the result is read more cautiously." }
        return didIt == "partly" ? "Noted \u{2014} you made part of this change." : "Noted \u{2014} you made this change."
    }
}

/// "What worked for you" — the server's sentences about this restaurant's
/// own measured record, rendered exactly as given (numbers in the number
/// face). The card does not exist until the server says there is enough.
struct WhatWorkedCard: View {
    let whatWorked: WhatWorked

    var body: some View {
        if whatWorked.isShown {
            VStack(alignment: .leading, spacing: 14) {
                HomeSectionHeader(kicker: "Your record", title: "What worked for you",
                                  trailing: whatWorked.days.map { "\($0) days" })
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(Array(whatWorked.sentences.enumerated()), id: \.offset) { _, sentence in
                        HStack(alignment: .top, spacing: 12) {
                            Circle().fill(Color.cavnarGreen).frame(width: 7, height: 7).padding(.top, 7)
                            HomeMixedText.make(sentence, size: 14.5, weight: 500, color: .cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 0)
                        }
                    }
                    if let caveat = whatWorked.caveat, !caveat.isEmpty {
                        CavnarCaveat(title: "Before and after, not proof", detail: caveat)
                            .padding(.top, 4)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .cavnarCard()
            }
        }
    }
}
