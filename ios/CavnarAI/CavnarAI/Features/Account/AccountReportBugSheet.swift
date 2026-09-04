import SwiftUI
import UIKit

/// Account -> Support -> Report a bug. Lands in Will's inbox with the build
/// stamp and device details attached automatically, so "which build is
/// this?" never has to be asked.
struct AccountReportBugSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var message = ""
    @State private var postedLabel: String?
    private enum Field: Hashable { case message }
    @FocusState private var focused: Field?

    private var deviceLine: String {
        let device = UIDevice.current
        return "\(device.model) · iOS \(device.systemVersion)"
    }

    private var buildLine: String {
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        return "v\(version) · build \(BuildInfo.gitSHA) · \(BuildInfo.builtAt)"
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Report a bug") {
                        GlowBadge(systemImage: "ladybug", size: 64)
                    } subtitle: {
                        Text("Goes straight to Will")
                    }

                    AccountSection(kicker: "What happened") {
                        AccountEditor(label: "Describe it", placeholder: "What were you doing, and what did you expect instead?",
                                      text: $message, focus: $focused, field: .message, showsDivider: false)
                    }

                    AccountSection(kicker: "Attached automatically") {
                        AccountKVRow(label: "Build") { AccountValue(text: buildLine, isNumber: true) }
                        AccountKVRow(label: "Device", showsDivider: false) { AccountValue(text: deviceLine) }
                    }

                    if let error = viewModel.reportBugError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    Button {
                        Task {
                            if await viewModel.reportBug(message: message, build: buildLine, device: deviceLine) {
                                Haptic.success()
                                postedLabel = "Sent — thank you"
                            }
                        }
                    } label: {
                        Group {
                            if viewModel.isReportingBug { CavnarShimmerText(text: "Sending…") } else { Text("Send report") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: message.trimmingCharacters(in: .whitespaces).count < 5 || viewModel.isReportingBug))
                    .disabled(message.trimmingCharacters(in: .whitespaces).count < 5 || viewModel.isReportingBug)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Report a Bug")
            .keyboardDoneToolbar { focused = nil }
            .cavnarPostedOverlay(postedLabel) { dismiss() }
        }
    }
}
