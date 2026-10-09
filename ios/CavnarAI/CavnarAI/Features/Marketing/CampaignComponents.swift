import SwiftUI
import WebKit

// Small pieces the Campaign Studio, the Campaigns tab and the Text Club
// share: the send gate's sheet, the email's rendered preview, the chips and
// the check lines.

/// The kicker over a card's content — the design system's `CavnarKicker`
/// with an optional tag capsule beside it ("Picked by Cavnar AI").
struct CampaignKicker: View {
    let text: String
    var tag: String? = nil
    var tagIsAI = false

    var body: some View {
        HStack(spacing: 8) {
            CavnarKicker(text)
            if let tag, !tag.isEmpty {
                HStack(spacing: 4) {
                    if tagIsAI {
                        Image(systemName: "sparkles").font(.cavnar(.tag)).accessibilityHidden(true)
                    }
                    Text(tag).cavnarText(.tag, color: tagIsAI ? .cavnarEmber2 : .cavnarInk2)
                }
                .foregroundStyle(tagIsAI ? Color.cavnarEmber2 : Color.cavnarInk3)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(Capsule().fill(tagIsAI ? Color.cavnarEmber.opacity(0.14) : Color.white.opacity(0.05)))
            }
        }
    }
}

/// A chip that is on or off — a channel, a platform, an idea. A 44pt tap
/// target; on reads ember, off reads quiet.
struct CampaignToggleChip: View {
    let label: String
    var count: Int? = nil
    var isOn: Bool
    var systemImage: String? = nil
    var action: () -> Void

    var body: some View {
        Button {
            Haptic.selection()
            action()
        } label: {
            HStack(spacing: 6) {
                Image(systemName: isOn ? "checkmark.circle.fill" : (systemImage ?? "circle"))
                    .font(.cavnar(.caption))
                    .accessibilityHidden(true)
                Text(label).font(.cavnarBody(CavnarType.secondary, weight: 700))
                if let count {
                    Text("\(count)").font(.cavnarNumber(CavnarType.secondary, weight: 700))
                        .foregroundStyle(isOn ? Color.cavnarEmber2 : Color.cavnarInk2)
                }
            }
            .foregroundStyle(isOn ? Color.cavnarInk : Color.cavnarInk2)
            .padding(.horizontal, 12)
            .frame(minHeight: 44)
            .background(Capsule().fill(isOn ? Color.cavnarEmber.opacity(0.16) : Color.white.opacity(0.04)))
            .overlay(Capsule().strokeBorder(isOn ? Color.cavnarEmber.opacity(0.55) : Color.cavnarPaper3, lineWidth: 1))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isOn ? .isSelected : [])
    }
}

/// One line of a check list or a send result: a green tick or an amber warn.
struct CampaignCheckLine: View {
    let ok: Bool
    let text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .font(.cavnar(.caption))
                .foregroundStyle(ok ? Color.cavnarGreen : Color.cavnarAmber)
                .accessibilityHidden(true)
            CavnarMixedText(text, role: .secondary, color: ok ? .cavnarInk2 : .cavnarInk)
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }
}

/// "Cavnar AI flagged this" — what the send gate held back and why, with
/// the owner's two ways on: fix it, or drop it. Nothing was sent.
struct SendGateSheet: View {
    let flag: SendGateFlag
    var onEdit: () -> Void
    var onDiscard: () -> Void
    @Environment(\.dismiss) private var dismiss
    /// "Discard it" asks first — the draft is gone after it (L4).
    @State private var confirmingDiscard = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    CampaignKicker(text: flag.channel == "email" ? "Your email" : "Your text")
                    Text("Cavnar AI flagged this")
                        .font(.cavnarHeadline(CavnarType.section))
                        .foregroundStyle(Color.cavnarInk)
                    HomeMixedText.make(flag.message, size: CavnarType.body, weight: 500, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    if !flag.reasons.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(flag.reasons, id: \.self) { reason in
                                CampaignCheckLine(ok: false, text: reason)
                            }
                        }
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .cavnarCard()
                    }
                    // Ink2, readable (re-audit 10/8/26 M14), above the buttons.
                    Text("Nothing went out. It reaches every guest in the audience and can\u{2019}t be recalled, so it was held for you to look at.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    VStack(spacing: 10) {
                        Button {
                            Haptic.light()
                            dismiss()
                            onEdit()
                        } label: {
                            Text("Edit it").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle())
                        Button(role: .destructive) {
                            Haptic.light()
                            confirmingDiscard = true
                        } label: {
                            Text("Discard it").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                    .padding(.top, 4)
                }
                .padding(20)
            }
            .accountSheetChrome("Held back")
            .confirmationDialog(flag.channel == "email" ? "Discard this email?" : "Discard this text?",
                                isPresented: $confirmingDiscard, titleVisibility: .visible) {
                Button("Discard it", role: .destructive) {
                    onDiscard()
                    dismiss()
                }
                Button("Keep it", role: .cancel) {}
            } message: {
                Text("Its words are cleared from the Studio. Nothing was sent.")
            }
        }
        .presentationDetents([.medium, .large])
    }
}

/// The email exactly as a guest gets it — the server's own render
/// (POST /guest-newsletter/preview), at the phone's width. Read-only: no
/// script, and no link leaves the preview. Email is light-only, so this is
/// the one light surface on the screen (DESIGN_SYSTEM → Email).
struct NewsletterWebPreview: UIViewRepresentable {
    let html: String

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.defaultWebpagePreferences.allowsContentJavaScript = false
        let view = WKWebView(frame: .zero, configuration: config)
        view.navigationDelegate = context.coordinator
        view.isOpaque = false
        view.backgroundColor = .clear
        view.scrollView.backgroundColor = .clear
        view.accessibilityLabel = "The email as your guests get it"
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        guard context.coordinator.loaded != html else { return }
        context.coordinator.loaded = html
        view.loadHTMLString(html, baseURL: nil)
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var loaded: String?

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
            // The preview's own load only; a tapped button stays put.
            decisionHandler(navigationAction.navigationType == .other ? .allow : .cancel)
        }
    }
}
