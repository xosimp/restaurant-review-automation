import SwiftUI
import Observation

/// One queued automatic send (delayed.py) — "Next week's schedule goes out
/// at 11:00", "A supplier order goes out in an hour" — with the two things
/// an owner opens it to do: Undo it, or look at it first.
///
/// The push said "Undo from Home before then" and landed on Labor, where no
/// undo exists (friction audit #3, U3-1). The push, its notification row
/// and any `action/<id>` nav path now open this sheet instead. Undo is a
/// status change on a row nothing has acted on yet, so it happens at once;
/// the server says so when the send already went.
struct PendingAction: Decodable, Identifiable, Equatable {
    let id: Int
    let kind: String?
    let label: String?
    let executeAt: String?
    let status: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, label, status
        case executeAt = "execute_at"
    }

    /// Where "Review" goes: the schedule or the order it would send.
    var reviewNav: String {
        switch kind {
        case "schedule_publish", "schedule_changes_send": return "labor/schedule"
        case "order_send": return "inventory/order"
        default: return "home"
        }
    }

    /// "Goes out at 11:00am" — on the restaurant's clock, M/D/YY when it
    /// isn't today there.
    func goesOutLine(now: Date = Date(), zone: TimeZone = RestaurantClock.timeZone) -> String? {
        guard let raw = executeAt, let at = CavnarDate.timestamp(raw) else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let c = calendar.dateComponents([.hour, .minute], from: at)
        guard let h = c.hour, let m = c.minute else { return nil }
        let clock = "\(h % 12 == 0 ? 12 : h % 12):\(String(format: "%02d", m))\(h < 12 ? "am" : "pm")"
        if calendar.isDate(at, inSameDayAs: now) { return "Goes out at \(clock)" }
        return "Goes out \(CavnarDate.mdy(at, in: zone)) at \(clock)"
    }
}

@Observable
@MainActor
final class PendingActionViewModel {
    private(set) var action: PendingAction?
    private(set) var isLoading = false
    private(set) var isUndoing = false
    private(set) var undone = false
    var errorMessage: String?
    private let client: APIClient

    init(client: APIClient = .shared) { self.client = client }

    private struct PendingResponse: Decodable {
        let ok: Bool
        let actions: [PendingAction]?
    }

    func load(id: Int) async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let r: PendingResponse = try await client.send("/mobile/api/actions/pending", hapticOnError: false)
            action = (r.actions ?? []).first { $0.id == id }
            if action == nil {
                errorMessage = "That already went out, or was already undone."
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t reach Cavnar AI."
        }
    }

    func undo() async {
        guard let action, !isUndoing else { return }
        isUndoing = true
        errorMessage = nil
        defer { isUndoing = false }
        do {
            let r: APIClient.OKResponse = try await client.send(
                "/mobile/api/actions/\(action.id)/cancel", method: .post, body: [String: String]())
            if r.ok {
                undone = true
                Haptic.success()
                // The Lock Screen countdown ends with it (F3-9).
                PendingSendActivities.finish(actionId: action.id, status: "stopped", note: nil)
            } else {
                errorMessage = r.error ?? "That already went out, or was already undone."
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t reach Cavnar AI."
        }
    }
}

struct PendingActionSheet: View {
    let actionId: Int
    @State private var viewModel = PendingActionViewModel()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                if viewModel.isLoading && viewModel.action == nil {
                    CavnarSkeletonLines(widths: [0.5, 0.9, 0.7], lineHeight: 12, spacing: 10)
                } else if let action = viewModel.action {
                    Text(viewModel.undone ? "UNDONE" : "GOING OUT ON ITS OWN")
                        .font(.cavnarBody(12, weight: 700))
                        .tracking(1.4)
                        .foregroundStyle(viewModel.undone ? Color.cavnarInk3 : Color.cavnarEmber2)
                    Text(action.label ?? "A queued send")
                        .font(.cavnarHeadline(20))
                        .foregroundStyle(Color.cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    if viewModel.undone {
                        Text("Nothing went out. You can send it yourself whenever it's ready.")
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarInk2)
                    } else if let line = action.goesOutLine() {
                        HomeMixedText.make(line, size: 14.5, weight: 600, color: .cavnarInk2)
                    }
                    if let error = viewModel.errorMessage {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                    }
                    Spacer(minLength: 8)
                    HStack(spacing: 12) {
                        Button {
                            open(action.reviewNav)
                        } label: {
                            Text("Review").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                        if !viewModel.undone {
                            Button {
                                Task { await viewModel.undo() }
                            } label: {
                                Group {
                                    if viewModel.isUndoing {
                                        CavnarShimmerText(text: "Undoing…")
                                    } else {
                                        Text("Undo")
                                    }
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isUndoing))
                            .disabled(viewModel.isUndoing)
                        }
                    }
                } else {
                    Text(viewModel.errorMessage ?? "That already went out, or was already undone.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk2)
                    Spacer(minLength: 8)
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .accountSheetChrome("Queued send")
        }
        .task { await viewModel.load(id: actionId) }
    }

    /// Review: close this and open the schedule or order it would send.
    private func open(_ path: String) {
        Haptic.light()
        dismiss()
        if let nav = NavPath(path) {
            NotificationCenter.default.post(name: .cavnarOpenNav, object: nav)
        }
    }
}
