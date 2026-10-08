import SwiftUI

/// Still open's outward steps and the swipe that carries a row's second
/// choice (parity audit #3). The web opens the confirm card in place under
/// the row (hbConfirmAct); the phone opens the same card — Ask's
/// ProposalCard, built by POST /mobile/api/command/propose — in a sheet.

/// The step whose confirm card is open: the row, the step (Send now,
/// Approve, Deny) and the command action it proposes.
struct HomeActionProposal: Identifiable {
    let item: ActionItem
    let step: ActionItem.Step
    let confirm: ActionItem.Confirm
    var id: String { item.key + "|" + step.label }
}

/// The confirm card for one Still-open step. Nothing runs until Confirm:
/// the card names who it reaches and what goes out, and Confirm posts
/// through Ask's own confirm (its proposal header, its job poll, its
/// warning). A refusal is said in the server's words, never swallowed.
struct HomeActionProposalSheet: View {
    let proposal: HomeActionProposal
    let viewModel: HomeFollowThroughViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var card: AskProposal?
    @State private var loading = true
    @State private var askViewModel = AskCavnarViewModel()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text(proposal.item.title)
                        .font(.cavnarHeadline(19))
                        .foregroundStyle(Color.cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    if let card {
                        ProposalCard(proposal: card, viewModel: askViewModel, onDone: {
                            viewModel.rowNote[proposal.item.key] = askViewModel.lastConfirmWarning.map {
                                HomeFollowThroughViewModel.RowNote(text: $0, tone: .warn)
                            } ?? HomeFollowThroughViewModel.RowNote(text: Self.doneLine(proposal.step), tone: .good)
                            Task {
                                await viewModel.load()
                                // Long enough to read Done (and a warning).
                                try? await Task.sleep(for: .seconds(askViewModel.lastConfirmWarning == nil ? 1.2 : 3))
                                dismiss()
                            }
                        })
                    } else if loading {
                        CavnarSkeletonLines(widths: [0.6, 0.95, 0.8], lineHeight: 12, spacing: 10)
                    } else {
                        Text(viewModel.rowNote[proposal.item.key]?.text
                             ?? "Couldn\u{2019}t open that \u{2014} open the item instead.")
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .topLeading)
            }
            .cavnarModuleBackground()
            .accountSheetChrome(proposal.step.label)
        }
        .presentationDetents([.medium, .large])
        .task {
            viewModel.rowNote[proposal.item.key] = nil
            card = await viewModel.propose(proposal.item, proposal.confirm)
            loading = false
        }
    }

    /// "Approved" / "Denied" / "Sent" — what the row says once it went.
    static func doneLine(_ step: ActionItem.Step) -> String {
        switch step.confirm?.args["decision"] {
        case .string("approve")?: return "Approved"
        case .string("deny")?: return "Denied"
        default: return step.confirm?.action == "publish_schedule" ? "Sent to staff" : "Done"
        }
    }
}

// MARK: - Swipe on a card row

/// A row's second choice as a trailing swipe, on a row that lives in a
/// card's VStack rather than a List (where `.swipeActions` does nothing):
/// drag left to reveal it, tap it, or swipe all the way to run it. The same
/// action is in the row's context menu and its accessibility actions, so
/// nothing depends on the gesture.
struct HomeSwipeAction {
    let label: String
    let systemImage: String
    let tint: Color
    let action: () -> Void
}

private struct HomeSwipeModifier: ViewModifier {
    let swipe: HomeSwipeAction
    @State private var offset: CGFloat = 0
    @State private var settled: CGFloat = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private let width: CGFloat = 88

    func body(content: Content) -> some View {
        content
            .offset(x: offset)
            .overlay(alignment: .trailing) {
                if offset < 0 {
                    Button {
                        close()
                        swipe.action()
                    } label: {
                        VStack(spacing: 4) {
                            Image(systemName: swipe.systemImage)
                                .font(.system(size: 15, weight: .bold))
                            Text(swipe.label)
                                .font(.cavnarBody(12.5, weight: 700))
                                .lineLimit(1)
                                .minimumScaleFactor(0.8)
                        }
                        .foregroundStyle(Color.cavnarInk)
                        .frame(width: max(0, -offset))
                        .frame(maxHeight: .infinity)
                        .background(swipe.tint == .cavnarInk3 ? Color.cavnarPaper3 : swipe.tint,
                                    in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .padding(.vertical, 6)
                    .transition(.opacity)
                }
            }
            .simultaneousGesture(
                DragGesture(minimumDistance: 18)
                    .onChanged { v in
                        // Horizontal only: a vertical scroll that wanders
                        // sideways never opens it.
                        guard abs(v.translation.width) > abs(v.translation.height) * 1.5 else { return }
                        offset = min(0, max(-width * 1.8, settled + v.translation.width))
                    }
                    .onEnded { v in
                        guard abs(v.translation.width) > abs(v.translation.height) * 1.5 else {
                            return
                        }
                        if offset < -width * 1.5 {
                            Haptic.medium()
                            close()
                            swipe.action()
                        } else if offset < -width / 2 {
                            animate { offset = -width; settled = -width }
                            Haptic.selection()
                        } else {
                            close()
                        }
                    }
            )
            .accessibilityAction(named: Text(swipe.label)) { swipe.action() }
    }

    private func close() { animate { offset = 0; settled = 0 } }

    private func animate(_ change: () -> Void) {
        if reduceMotion { change() } else { withAnimation(.easeOut(duration: 0.2), change) }
    }
}

extension View {
    /// A trailing swipe for a row in a card (nil: no swipe).
    @ViewBuilder
    func homeSwipeAction(_ swipe: HomeSwipeAction?) -> some View {
        if let swipe { modifier(HomeSwipeModifier(swipe: swipe)) } else { self }
    }
}
