import SwiftUI

/// One open issue, opened from its Home row or its push (parity audit #11):
/// what it is, who has it, and the three things the web Home does to it —
/// Resolve (with Undo, back on Home), Hand it to… (reassign: the person is
/// texted, so it confirms first) and, on a coverage issue, Ask someone to
/// cover (texted too, confirmed too). Routes: strategy_routes
/// /issues/<id>/resolve | reassign | ask-cover, both prefixes.
struct HomeIssueSheet: View {
    let issue: HomeDayViewModel.Issue
    let viewModel: HomeDayViewModel
    /// Resolve: the sheet closes and Home runs it with its Undo capsule.
    var onResolve: (HomeDayViewModel.Issue) -> Void

    @State private var contacts: [HomeDayViewModel.Contact]?
    @State private var loadingContacts = true
    @State private var handingTo: HomeDayViewModel.Contact?
    @State private var asking: HomeDayViewModel.CoverAsk?
    @State private var busy = false
    @State private var error: String?
    @State private var posted: String?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header
                    if issue.status != "resolved" {
                        Button {
                            Haptic.light()
                            onResolve(issue)
                        } label: {
                            Text("Resolve").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle())
                    }
                    coverSection
                    handSection
                    if let error {
                        Text(error)
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .topLeading)
            }
            .cavnarModuleBackground()
            .accountSheetChrome("Issue")
            .cavnarPostedOverlay(posted) { dismiss() }
        }
        .presentationDetents([.medium, .large])
        .task {
            contacts = await viewModel.reassignableContacts()
            loadingContacts = false
        }
        .confirmationDialog(handingTo.map { "Hand it to \($0.name)?" } ?? "",
                            isPresented: Binding(get: { handingTo != nil }, set: { if !$0 { handingTo = nil } }),
                            titleVisibility: .visible, presenting: handingTo) { contact in
            Button("Hand it to \(HomeDayViewModel.firstName(contact.name))") {
                Task {
                    busy = true
                    defer { busy = false }
                    if let reason = await viewModel.reassign(issue, to: contact) {
                        error = reason
                    } else {
                        posted = "Handed to \(HomeDayViewModel.firstName(contact.name))"
                    }
                }
            }
            Button("Not yet", role: .cancel) {}
        } message: { contact in
            Text("Cavnar AI texts \(HomeDayViewModel.firstName(contact.name)) a link to this issue.")
        }
        .confirmationDialog(asking.map { "Ask \($0.name) to cover?" } ?? "",
                            isPresented: Binding(get: { asking != nil }, set: { if !$0 { asking = nil } }),
                            titleVisibility: .visible, presenting: asking) { ask in
            Button("Ask \(HomeDayViewModel.firstName(ask.name))") {
                Task {
                    busy = true
                    defer { busy = false }
                    if await viewModel.askToCover(ask.issue, name: ask.name) {
                        posted = "\(HomeDayViewModel.firstName(ask.name)) has been asked"
                    } else {
                        error = viewModel.issueError
                    }
                }
            }
            Button("Not yet", role: .cancel) {}
        } message: { ask in
            Text(HomeDayViewModel.coverAskMessage(ask))
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(Self.kicker(issue).uppercased())
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .tracking(1.4)
                .foregroundStyle(Color.cavnarEmber2)
            HomeMixedText.make(issue.title, size: 21, weight: 700, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Circle().fill(issue.tone).frame(width: 8, height: 8)
                HomeMixedText.make(issue.statusLine + (issue.createdAt.map { " \u{00B7} filed \(CavnarDate.mdyLocal($0))" } ?? ""),
                                   size: 13, weight: 600, color: .cavnarInk3)
            }
            if let detail = issue.detail, !detail.isEmpty {
                HomeMixedText.make(detail, size: 15, weight: 500, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 2)
            }
            if issue.isGroup {
                CoverageGapList(issue: issue) { name in
                    asking = HomeDayViewModel.CoverAsk(issue: issue, name: name)
                }
            }
            ForEach(issue.askedNames, id: \.self) { name in
                CoverAnswerRow(issueId: issue.id, name: name)
            }
        }
    }

    /// One button per suggested cover nobody has asked yet.
    @ViewBuilder
    private var coverSection: some View {
        let covers = issue.isGroup ? [] : issue.coversToAsk
        if !covers.isEmpty {
            AccountSection(kicker: "Ask someone to cover") {
                ForEach(Array(covers.enumerated()), id: \.element) { index, name in
                    Button {
                        Haptic.light()
                        asking = HomeDayViewModel.CoverAsk(issue: issue, name: name)
                    } label: {
                        HStack {
                            Text("Ask \(HomeDayViewModel.firstName(name)) to cover")
                                .font(.cavnarBody(15, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            Spacer()
                            Image(systemName: "paperplane")
                                .font(.system(size: 13, weight: .semibold))
                                .foregroundStyle(Color.cavnarEmber2)
                        }
                        .frame(minHeight: 48)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(busy)
                    if index < covers.count - 1 { AccountRowDivider() }
                }
            }
        }
    }

    /// Hand it to someone else — the account holder's routed contacts who
    /// agreed to texts. Hidden for a login that may not reassign.
    @ViewBuilder
    private var handSection: some View {
        if issue.status != "resolved" {
            if loadingContacts {
                CavnarSkeletonLines(widths: [0.4, 0.8], lineHeight: 11, spacing: 10)
            } else if let contacts {
                AccountSection(kicker: "Hand it to") {
                    if contacts.isEmpty {
                        Text("Nobody on your alert contacts has agreed to texts yet \u{2014} add someone in Account \u{2192} Notifications.")
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.vertical, 10)
                    }
                    ForEach(Array(contacts.enumerated()), id: \.element.id) { index, contact in
                        Button {
                            Haptic.light()
                            handingTo = contact
                        } label: {
                            HStack {
                                Text(contact.name)
                                    .font(.cavnarBody(15, weight: 600))
                                    .foregroundStyle(Color.cavnarInk)
                                Spacer()
                                if contact.name == issue.assigneeName {
                                    Text("has it")
                                        .font(.cavnarBody(12.5, weight: 600))
                                        .foregroundStyle(Color.cavnarInk3)
                                } else {
                                    Image(systemName: "arrow.right.circle")
                                        .font(.system(size: 14, weight: .semibold))
                                        .foregroundStyle(Color.cavnarEmber2)
                                }
                            }
                            .frame(minHeight: 48)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(busy || contact.name == issue.assigneeName)
                        if index < contacts.count - 1 { AccountRowDivider() }
                    }
                }
            }
        }
    }

    static func kicker(_ issue: HomeDayViewModel.Issue) -> String {
        switch issue.kind {
        case "coverage", "no_show": return "A shift to cover"
        case "review": return "Reviews"
        case "stock": return "Stock"
        case "labor": return "Labor"
        case "loss": return "Comps, voids and refunds"
        case "checklist", "task_missed", "task_sheet", "task_pattern", "task_flag": return "Checklists"
        default: return issue.severity == "high" ? "Urgent issue" : "Open issue"
        }
    }
}
