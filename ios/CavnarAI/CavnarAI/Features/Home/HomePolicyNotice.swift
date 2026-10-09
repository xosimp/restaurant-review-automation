import SwiftUI

/// "We updated our Privacy Policy and Terms on 10/1/26: how Cavnar AI's
/// benchmarks use pooled, de-identified figures. Read what changed →" —
/// the dashboard notice a material policy change owes an account holder for
/// 30 days (policy_notice; the server decides who and until when). The ✕
/// dismisses it for this login on every device (POST /account/policy-
/// notice/dismiss); it leaves only once the server has the dismissal, and
/// a failed one is said. The link opens cavnar.ai/privacy.
struct HomePolicyNoticeCard: View {
    let notice: HomePolicyNotice
    /// Told once the dismissal is saved — Home re-reads.
    var onDismissed: () -> Void = {}
    var client: APIClient = .shared

    @Environment(\.openURL) private var openURL
    @State private var busy = false
    @State private var gone = false
    @State private var error: String?

    var body: some View {
        if !gone {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "doc.text.magnifyingglass")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(width: 32, height: 32)
                    .background(Color.cavnarEmber.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 6) {
                    HomeMixedText.make(notice.text, role: .body, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    Button {
                        Haptic.light()
                        openURL(notice.destination)
                    } label: {
                        Text(notice.linkText)
                            .cavnarText(.label, color: .cavnarEmber2)
                            .frame(minHeight: 44, alignment: .leading)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("Opens the privacy policy on cavnar.ai")
                    if busy { CavnarSkeletonBar(height: 3) }
                    if let error {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
                AccountActionChip(symbol: "xmark", tone: .cavnarInk3,
                                  accessibilityLabel: "Dismiss the policy notice") {
                    Task { await dismiss() }
                }
                .disabled(busy)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(Color.cavnarPaper2.opacity(0.7))
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card)
                .strokeBorder(Color.cavnarEmber2.opacity(0.28), lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
            .transition(.opacity)
        }
    }

    private struct Resp: Decodable { let ok: Bool; let error: String? }

    @MainActor
    private func dismiss() async {
        guard !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let r: Resp = try await client.send(notice.mobileDismissPath, method: .post, body: [String: String](),
                                                retryTransient: false)
            guard r.ok else { error = r.error ?? "Couldn\u{2019}t dismiss it."; return }
            Haptic.selection()
            withAnimation(.easeOut(duration: 0.2)) { gone = true }
            onDismissed()
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t dismiss it."
        }
    }
}
