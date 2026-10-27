import SwiftUI

/// The blocking "Update Cavnar AI" sheet, over everything, once the server
/// answered 426 (AppUpdateGate; release-safety audit #25 A06-02). Nothing
/// under it can be reached: this build can no longer read the server's
/// answers, so every screen would only fail. One primary action — open the
/// App Store (DESIGN_SYSTEM §5, one primary per sheet); no close button.
/// A floating card (§10 surface hierarchy) on the Paper page, the seal above
/// the headline, the server's own sentence as the body.
struct AppUpdateRequiredView: View {
    let requirement: AppUpdateRequirement
    @Environment(\.openURL) private var openURL

    static let title = "Update Cavnar AI"
    static let buttonTitle = "Open the App Store"

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
/// under Reduce Motion), whenever the gate holds a requirement.
struct AppUpdateOverlay: ViewModifier {
    let gate: AppUpdateGate
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .overlay {
                if let need = gate.requirement {
                    AppUpdateRequiredView(requirement: need)
                        .transition(.opacity)
                }
            }
            .animation(reduceMotion ? nil : .easeOut(duration: 0.25), value: gate.requirement)
    }
}
