import SwiftUI

// "Who runs the floor instead?" for a change to a published week (blind
// re-audit 10/26/26 B2). Approving time off or an availability change,
// saving availability, or a roster setting (deactivating someone, who runs
// the floor, training, a weekday off) is refused when it would leave a
// minute of a published week with no manager on the floor — the owner's
// highest rule — unless the change names who runs the floor instead (409
// `needs_cover`, `covered_by`). Nothing changed on the refusal; the answer
// re-sends the same change with the name. The web asks it inline; the drop
// approval has its own block (NeedsCoverBlock).

/// One refused change waiting on a name.
struct FloorCoverAsk: Identifiable {
    let id = UUID()
    /// The server's sentence: what the change leaves with no manager.
    let gap: String
    /// The button that re-sends it: "Approve" or "Save".
    var verb = "Save"
    /// The same change again, with who runs the floor.
    let retry: @MainActor (String) async -> Void
}

extension APIClient.APIError {
    private struct NeedsCoverReply: Decodable {
        var needsCover: Bool? = nil
        var managerGap: String? = nil
        enum CodingKeys: String, CodingKey {
            case needsCover = "needs_cover"
            case managerGap = "manager_gap"
        }
    }

    /// The gap sentence when this refusal is a 409 `needs_cover`; nil for
    /// any other error.
    var floorGap: String? {
        guard status == 409, let reply = decodeBody(NeedsCoverReply.self), reply.needsCover == true else { return nil }
        let gap = (reply.managerGap ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return gap.isEmpty ? message : gap
    }
}

/// The question as an alert over whichever screen made the change.
private struct FloorCoverAlert: ViewModifier {
    @Binding var ask: FloorCoverAsk?
    @State private var who = ""

    private var name: String { who.trimmingCharacters(in: .whitespacesAndNewlines) }

    func body(content: Content) -> some View {
        content.alert("No manager on the floor",
                      isPresented: Binding(get: { ask != nil }, set: { if !$0 { ask = nil } }),
                      presenting: ask) { pending in
            TextField("A manager, or you", text: $who)
            Button(pending.verb) {
                let covering = name
                who = ""
                guard !covering.isEmpty else { return }
                Task { await pending.retry(covering) }
            }
            Button("Don\u{2019}t change it", role: .cancel) { who = "" }
        } message: { pending in
            Text("\(pending.gap)\n\nWho runs the floor instead? The owner counts.")
        }
    }
}

extension View {
    /// Asks who runs the floor when `ask` is set, and re-sends the change.
    func floorCoverAlert(_ ask: Binding<FloorCoverAsk?>) -> some View {
        modifier(FloorCoverAlert(ask: ask))
    }
}
