import SwiftUI

/// The blocking "Update Cavnar AI" sheet, over everything, once the server
/// answered 426 (AppUpdateGate; release-safety audit #25 A06-02). Nothing
/// under it can be reached: this build can no longer read the server's
/// answers, so every screen would only fail. One primary action — open the
/// App Store (DESIGN_SYSTEM §5, one primary per sheet) — and a secondary
/// "Check again" that asks the server once more (the minimum may have been
/// lowered: blind re-audit 10/27/26); no close button. A floating card (§10
/// surface hierarchy) on the Paper page, the seal above the headline, the
/// server's own sentence as the body, the ember pulse while it asks.
struct AppUpdateRequiredView: View {
    let requirement: AppUpdateRequirement
    var isChecking = false
    var onCheckAgain: () -> Void = {}
    @Environment(\.openURL) private var openURL

    static let title = "Update Cavnar AI"
    static let buttonTitle = "Open the App Store"
    static let checkTitle = "Check again"

    var body: some View {
        ZStack {
            Color.cavnarPaper.opacity(0.96).ignoresSafeArea()
            ScrollView {
                VStack(spacing: 18) {
                    CavnarSealMark(size: 56)
                        .accessibilityHidden(true)
                    Text(Self.title)
                        .cavnarText(.headline)
                        .multilineTextAlignment(.center)
                        .accessibilityAddTraits(.isHeader)
                    Text(requirement.message)
                        .cavnarText(.body)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                    Button {
                        openURL(requirement.openURL)
                    } label: {
                        Text(Self.buttonTitle)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .accessibilityHint("Opens the App Store to update the app")
                    Button {
                        onCheckAgain()
                    } label: {
                        Text(Self.checkTitle)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(isChecking)
                    .accessibilityHint("Asks Cavnar AI again whether this version still needs the update")
                    if isChecking {
                        CavnarSkeletonBar(height: 3)
                            .frame(maxWidth: 160)
                            .accessibilityElement()
                            .accessibilityLabel("Checking with Cavnar AI")
                    }
                }
                .padding(24)
                .frame(maxWidth: 420)
                .cavnarCard(.floating)
                .padding(.horizontal, 20)
                .frame(maxWidth: .infinity, minHeight: 0)
                .padding(.vertical, 80)
            }
            .scrollBounceBehavior(.basedOnSize)
        }
        .accessibilityElement(children: .contain)
        .accessibilityAddTraits(.isModal)
    }
}

/// RootView's one modifier for the sheet: shown, with a short fade (none
/// under Reduce Motion), whenever the gate holds a requirement — and, while
/// it does, the server is asked again on every return to the foreground and
/// every `AppUpdateGate.recheckInterval`, so a lowered minimum lets the app
/// back in without a relaunch.
struct AppUpdateOverlay: ViewModifier {
    let gate: AppUpdateGate
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase

    func body(content: Content) -> some View {
        content
            .overlay {
                if let need = gate.requirement {
                    AppUpdateRequiredView(requirement: need, isChecking: gate.isChecking,
                                          onCheckAgain: { Task { await gate.recheck() } })
                        .transition(.opacity)
                        .task(id: need) {
                            while !Task.isCancelled {
                                try? await Task.sleep(for: .seconds(AppUpdateGate.recheckInterval))
                                if Task.isCancelled { break }
                                await gate.recheck()
                            }
                        }
                }
            }
            .onChange(of: scenePhase) { _, phase in
                guard phase == .active, gate.requirement != nil else { return }
                Task { await gate.recheck() }
            }
            .animation(reduceMotion ? nil : .easeOut(duration: 0.25), value: gate.requirement)
    }
}
