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
    @State private var refName = ""
    @State private var refEmail = ""
    @State private var refNote = ""
    @State private var refSending = false
    @State private var refSent = false
    @State private var refError: String?
    private let viewModel = AccountViewModel()

    private static let groups: [FAQGroup] = [
        FAQGroup(title: "Getting started", items: [
            FAQItem(question: "How is Cavnar AI set up for my restaurant?",
                    answer: "Will personally sets up every restaurant — connecting your Google Business Profile, POS system, and configuring the modules you're paying for. You won't need to do any technical setup yourself; if something looks disconnected or missing, contact Will."),
            FAQItem(question: "What's the difference between the modules?",
                    answer: "Reviews drafts AI responses to your Google reviews and flags urgent ones. Intel tracks what's being said about you and nearby competitors. Marketing automates guest outreach. Labor pulls hours and schedules from your POS. Food Cost tracks ingredient spend and waste. Not every restaurant has every module — check Account for what's active on yours."),
        ]),
        FAQGroup(title: "Reviews", items: [
            FAQItem(question: "Does Cavnar AI post responses automatically?",
                    answer: "No — every AI-drafted response needs your approval before it goes out. You can edit the draft, approve it as-is, or regenerate it from the review's detail screen."),
            FAQItem(question: "Why does a review show as \"urgent\"?",
                    answer: "Reviews mentioning health, safety, or a sharp negative sentiment are flagged urgent so they surface above routine reviews and can trigger an alert, depending on your Alert settings."),
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
            FAQItem(question: "How do I change my plan or payment method?",
                    answer: "Go to Account → Plan & payment. If you don't have an active plan yet, or need something changed on your contract, contact Will directly."),
            FAQItem(question: "Can I export my review data?",
                    answer: "Yes — Account → Export my data emails you a CSV of your reviews (date, rating, text, and response status)."),
            FAQItem(question: "How do I cancel my account?",
                    answer: "Getting started with Cavnar AI includes signing a service agreement, so cancellation isn't self-serve — go to Account → Close my account to request it. 30 days' written notice is required; your account stays active through the end of your current billing period plus 30 days."),
        ]),
        FAQGroup(title: "Notifications", items: [
            FAQItem(question: "How do I control which alerts I get?",
                    answer: "Account → Alerts & digest controls what triggers an alert (1-star reviews, a rating spike, health mentions, etc.) and whether it reaches you by text, email, or push. You can also set quiet hours so alerts wait until morning."),
            FAQItem(question: "What's the difference between alerts and the weekly digest?",
                    answer: "Alerts are near-real-time — something specific just happened. The weekly digest is a full summary sent on the day you choose. You can preview it any time from Account → Alerts & digest → Send me a preview."),
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
                    referralCard

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

                    Text("Can't find what you're looking for?")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                    if let url = URL(string: "mailto:will@cavnar.ai") {
                        Link(destination: url) {
                            HStack {
                                Text("Contact Will").font(.cavnarBody(15.5, weight: 600))
                                Spacer()
                                Image(systemName: "arrow.up.right").font(.system(size: 11))
                            }
                        }
                        .foregroundStyle(Color.cavnarEmber)
                    }

                    HStack(spacing: 18) {
                        if let url = URL(string: "https://cavnar.ai/privacy") {
                            Link("Privacy policy", destination: url)
                        }
                        if let url = URL(string: "https://cavnar.ai/terms") {
                            Link("Terms of service", destination: url)
                        }
                    }
                    .font(.cavnarBody(14, weight: 600))
                    .foregroundStyle(Color.cavnarInk3)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.top, 6)

                    Text("Build \(BuildInfo.gitSHA) · \(BuildInfo.builtAt)")
                        .font(.cavnarBody(12))
                        .foregroundStyle(Color.cavnarInk3.opacity(0.6))
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.top, 8)
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
                    Image("WillPortrait")
                        .resizable().scaledToFill()
                        .frame(width: 48, height: 48)
                        .clipShape(Circle())
                        .overlay(Circle().strokeBorder(Color.cavnarEmber.opacity(0.5), lineWidth: 1))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Will Cavnar").font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                        Text("Founder, Cavnar AI · your dedicated restaurant intelligence consultant")
                            .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
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

    /// The web's referral card: one free month if they sign up.
    private var referralCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            AccountKicker(text: "Know another restaurant owner?")
            VStack(alignment: .leading, spacing: 10) {
                Text("Send them an intro — you get one free month if they sign up.")
                    .font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                if refSent {
                    HStack(spacing: 8) {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.cavnarGreen)
                        Text("Referral sent — thank you.").font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarInk)
                    }
                } else {
                    TextField("Restaurant or owner name", text: $refName).cavnarTextFieldStyle()
                    TextField("Their email", text: $refEmail).cavnarTextFieldStyle()
                        .keyboardType(.emailAddress).textInputAutocapitalization(.never).autocorrectionDisabled()
                    TextField("A note from you (optional)", text: $refNote, axis: .vertical).cavnarTextFieldStyle()
                    if let refError {
                        Text(refError).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                    }
                    Button {
                        Task {
                            refSending = true
                            refError = nil
                            let err = await viewModel.sendReferral(name: refName, email: refEmail, note: refNote)
                            refSending = false
                            if let err { refError = err; Haptic.error() } else { refSent = true; Haptic.success() }
                        }
                    } label: {
                        Group {
                            if refSending { CavnarShimmerText(text: "Sending…") } else { Text("Send referral") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: refSending || refName.isEmpty || !refEmail.contains("@")))
                    .disabled(refSending || refName.isEmpty || !refEmail.contains("@"))
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
                        .font(.cavnarBody(15.5, weight: 700))
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
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
                    .lineSpacing(4)
                    .padding(.bottom, 2)
            }
        }
        .padding(.vertical, 11)
    }
}
