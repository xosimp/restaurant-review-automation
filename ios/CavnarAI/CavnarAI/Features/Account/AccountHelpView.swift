import SwiftUI

private struct FAQItem: Identifiable {
    let id = UUID()
    let question: String
    let answer: String
}

private struct FAQGroup: Identifiable {
    let id = UUID()
    let title: String
    let items: [FAQItem]
}

/// Opened from Account's "Help & FAQ" row. Static, hand-authored content —
/// no CMS/DB backing for v1. Content should be reviewed/edited by Will
/// before shipping; this is a first draft grounded in the app's actual
/// features, not placeholder copy.
struct AccountHelpView: View {
    @State private var expanded: Set<UUID> = []
    @Environment(\.colorSchemeContrast) private var contrast

    private static let groups: [FAQGroup] = [
        FAQGroup(title: "Getting started", items: [
            FAQItem(question: "How is Cavnar AI set up for my restaurant?",
                    answer: "Will personally sets up every restaurant — connecting your Google Business Profile, POS system, and configuring the modules you're paying for. You won't need to do any technical setup yourself; if something looks disconnected or missing, contact Will."),
            FAQItem(question: "What's the difference between the modules?",
                    answer: "Reviews drafts AI responses to your Google reviews and flags urgent ones. Intel tracks what's being said about you and nearby competitors. Marketing automates guest outreach. Labor pulls hours and schedules from your POS. Food Cost tracks ingredient spend and waste. Not every restaurant has every module — check Account for what's active on yours."),
        ]),
        // Every answer names a row as it reads in the app today (re-audit
        // M4: auto-approve exists, "Plan & billing", Export is a web row,
        // and there was no "Send me a preview" in the app).
        FAQGroup(title: "Reviews", items: [
            FAQItem(question: "Does Cavnar AI post responses automatically?",
                    answer: "Only if the account owner turns it on. By default every drafted reply waits for your approval — edit it, approve it as-is, or regenerate it from the review. Under Account → Profile & details → Auto-approve, the owner can let drafted 5-star replies (and, if chosen, 4-star) post on their own, up to a daily cap. Anything sensitive still waits for you."),
            FAQItem(question: "Why does a review show as \"urgent\"?",
                    answer: "Two kinds: a 1- or 2-star review from the last 30 days that hasn\u{2019}t been answered yet, and any review about illness, injury, a legal threat or staff misconduct. Urgent reviews sit at the top of your list until they\u{2019}re answered. The safety and legal ones can also send you an alert, depending on your Alert settings."),
        ]),
        FAQGroup(title: "Security", items: [
            FAQItem(question: "What does two-factor authentication protect?",
                    answer: "2FA adds a one-time code (by email or text) on top of your password at sign-in. Turn it on from Account → Security & devices. Once it's on, you'll also get a set of one-time backup codes — save them somewhere safe in case you ever lose access to your email or phone."),
            FAQItem(question: "What does \"Require Face ID to reopen\" do?",
                    answer: "When it's on, the app locks with Face ID (or your device passcode) every time you background it and come back — the same way a banking app does. It's on by default; turn it off in Account → Security & devices if you'd rather not be prompted."),
            FAQItem(question: "Can more than one person log in for my restaurant?",
                    answer: "Yes — the account owner can add teammates from Account → Manage team. Each teammate gets their own real login (not a shared password), and the owner can remove access at any time."),
        ]),
        FAQGroup(title: "Billing & account", items: [
            FAQItem(question: "How is billing handled?",
                    answer: "Billing is handled under your service agreement. Account → Plan & billing shows your plan and recent invoices, and lets the owner pause; for anything else to change on it, contact Will directly."),
            FAQItem(question: "Can I export my review data?",
                    answer: "Yes — Account → Export my data opens your web dashboard, where you pick reviews, labor, food cost or settings and they're emailed to you."),
            FAQItem(question: "How do I cancel my account?",
                    answer: "Getting started with Cavnar AI includes signing a service agreement, so cancellation isn't self-serve — go to Account → Close my account to request it. 30 days' written notice is required; your account stays active through the end of your current billing period plus 30 days."),
        ]),
        FAQGroup(title: "Notifications", items: [
            FAQItem(question: "How do I control which alerts I get?",
                    answer: "Account → Notifications holds your own choices on this phone: push, the alert types you've muted, quiet hours, and how much to hear from Cavnar AI. The restaurant's alert rules — what triggers an alert, text and email, the weekly digest — are set by the owner on the web, from the Restaurant alert rules row there."),
            FAQItem(question: "What's the difference between alerts and the weekly digest?",
                    answer: "Alerts are near-real-time — something specific just happened. The weekly digest is a full summary sent on the day you choose. To see one now, open Account → Email history on the web and use Send me a preview digest."),
        ]),
    ]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Help & FAQ") {
                        GlowBadge(systemImage: "questionmark.circle", size: 64)
                    } subtitle: {
                        Text("Common questions about Cavnar AI")
                    }

                    consultantCard

                    ForEach(Self.groups) { group in
                        VStack(alignment: .leading, spacing: 8) {
                            AccountKicker(text: group.title)
                            VStack(spacing: 0) {
                                ForEach(Array(group.items.enumerated()), id: \.element.id) { index, item in
                                    faqRow(item)
                                    if index < group.items.count - 1 { AccountRowDivider() }
                                }
                            }
                            .cavnarCard()
                        }
                    }

                    // The bottom "Contact Will" link repeated the
                    // consultant card at the top (iOS readability round).

                    HStack(spacing: 18) {
                        if let url = URL(string: "https://cavnar.ai/privacy") {
                            Link("Privacy policy", destination: url)
                        }
                        if let url = URL(string: "https://cavnar.ai/terms") {
                            Link("Terms of service", destination: url)
                        }
                    }
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarInk2)
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .center)
                    .padding(.top, 6)
                    // The build line is in Report a bug, where it's needed.
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Help & FAQ")
        }
    }

    /// The web Account tab opens on this: a real person, two ways to reach
    /// him. Same-day response, always.
    private var consultantCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            AccountKicker(text: "Your consultant")
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 12) {
                    // A tall portrait: fill the width, then show the band
                    // that has the face in it (roughly the top half of the
                    // photo), not the centre crop, which was all shirt.
                    Color.clear
                        .frame(width: 52, height: 52)
                        .overlay(alignment: .top) {
                            Image("WillPortrait")
                                .resizable().scaledToFill()
                                .frame(width: 52)
                                .offset(y: -4)
                        }
                        .clipShape(Circle())
                        .overlay(Circle().strokeBorder(Color.cavnarEmber.opacity(0.5), lineWidth: 1))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Will Cavnar").font(.cavnarBody(CavnarType.body, weight: 700)).foregroundStyle(Color.cavnarInk)
                        Text("Founder, Cavnar AI · your dedicated restaurant intelligence consultant")
                            .font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                HStack(spacing: 10) {
                    if let url = URL(string: "mailto:will@cavnar.ai") {
                        Link(destination: url) {
                            Label("Email Will", systemImage: "envelope").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                    if let url = URL(string: "https://calendly.com/will-cavnar/30min") {
                        Link(destination: url) {
                            Label("Book a call", systemImage: "calendar").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle())
                    }
                }
            }
            .cavnarCard()
        }
    }

    private func faqRow(_ item: FAQItem) -> some View {
        let isOpen = expanded.contains(item.id)
        return VStack(alignment: .leading, spacing: isOpen ? 12 : 0) {
            Button {
                Haptic.light()
                withAnimation(.easeOut(duration: 0.2)) {
                    if isOpen { expanded.remove(item.id) } else { expanded.insert(item.id) }
                }
            } label: {
                HStack(alignment: .top, spacing: 10) {
                    Text(item.question)
                        .font(.cavnarBody(CavnarType.body, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .multilineTextAlignment(.leading)
                        .lineSpacing(2)
                    Spacer(minLength: 8)
                    Image(systemName: isOpen ? "chevron.up" : "chevron.down")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Color.cavnarInk3)
                        .padding(.top, 2)
                }
            }
            .buttonStyle(.plain)
            if isOpen {
                Text(item.answer)
                    .font(.cavnar(.body))
                    .foregroundStyle(Color.cavnarInk2)
                    .lineSpacing(4)
                    .padding(.bottom, 2)
            }
        }
        .padding(.vertical, 11)
    }
}
