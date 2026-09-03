import SwiftUI

/// Pushed from Account's "Plan & payment" row. Adds recent invoices to
/// what the old inline card showed (next charge / amount / payment
/// method / manage link only) — mobile_api.py's billing route now
/// includes a short invoice history from the same Stripe customer.
struct AccountBillingDetailView: View {
    let viewModel: AccountViewModel
    let billing: BillingSummary?
    @Environment(\.scenePhase) private var scenePhase

    /// Prefer live state over the snapshot the sheet was opened with — the
    /// owner can change their plan in Stripe's portal while this sheet is
    /// backgrounded, and the snapshot would keep showing the pre-handoff
    /// state forever (audit 4.1).
    private var live: BillingSummary? { viewModel.billing ?? billing }

    /// Stripe's billing portal only ever lives on these hosts. `portalURL` is
    /// whatever the backend put in the JSON, and URL(string:) accepts far more
    /// than https — a tampered response (or a backend bug) could point this at
    /// an arbitrary phishing page opened from inside the trusted app UI
    /// (audit 1.4). Anything that isn't Stripe means no link is offered.
    private func validatedPortalURL(_ raw: String?) -> URL? {
        guard let raw, let url = URL(string: raw),
              url.scheme?.lowercased() == "https",
              let host = url.host?.lowercased(),
              host == "stripe.com" || host.hasSuffix(".stripe.com")
        else { return nil }
        return url
    }

    // Own NavigationStack — presented as a sheet from AccountView, matching
    // every other Account detail screen (see ScheduleHistoryView's comment
    // for why). The explicit maxWidth on the outer VStack below matters
    // here specifically: the "no active subscription" branch is a single
    // short Text with nothing else to stretch it, and a ScrollView proposes
    // its content its own ideal width rather than the screen's — without
    // the frame, that one narrow Text made the whole VStack (and therefore
    // cavnarModuleBackground()'s wash) hug to text-width instead of filling
    // the sheet, which read as "the sheet is miniaturized."
    var body: some View {
        NavigationStack {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                hero
                if let billing = live, billing.ok, billing.status != "inactive" {
                    statusStrip(billing)
                    VStack(alignment: .leading, spacing: 10) {
                        row("Next charge", billing.nextDate ?? "—", isNumber: true)
                        divider()
                        row("Amount", billing.amount ?? "—", isNumber: true)
                        divider()
                        row("Payment method", billing.paymentMethod ?? "—")
                        if let url = validatedPortalURL(billing.portalURL) {
                            divider()
                            Link(destination: url) {
                                HStack {
                                    Text("Manage payment method").font(.cavnarBody(15.5, weight: 600))
                                    Spacer()
                                    Image(systemName: "arrow.up.right").font(.system(size: 11))
                                }
                            }
                            .foregroundStyle(Color.cavnarEmber)
                        }
                    }
                    .cavnarCard()

                    if let invoices = billing.invoices, !invoices.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            AccountKicker(text: "Recent invoices")
                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(Array(invoices.enumerated()), id: \.element.id) { index, invoice in
                                    invoiceRow(invoice)
                                    if index < invoices.count - 1 {
                                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                                    }
                                }
                            }
                            .cavnarCard()
                        }
                    }
                }
                // No body text in the empty state — the hero already
                // shows "No active plan" + billing?.message (or the same
                // "Contact will@cavnar.ai to get set up" fallback) right
                // above; this used to repeat that exact string a second
                // time.
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
        }
        .accountSheetChrome("Billing")
        // Billing is the one screen whose source of truth changes outside the
        // app: the owner leaves for Stripe's portal, updates a card, and comes
        // back. Without these it kept showing "Payment past due" indefinitely
        // after they had already fixed it (audit 4.1).
        .task { await viewModel.loadBilling() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await viewModel.loadBilling() } }
        }
        .refreshable { await viewModel.loadBilling() }
        }
    }

    // MARK: - Identity (option A)

    private var planTitle: String {
        guard let billing = live, billing.ok, billing.status != "inactive", let status = billing.status else { return "No active plan" }
        switch status {
        case "active": return "Active plan"
        case "trialing": return "Free trial"
        case "past_due": return "Payment past due"
        default: return status.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    // "will@cavnar.ai" as a tappable ember link, subject prefilled — only
    // for the static fallback copy (below); billing?.message is arbitrary
    // server text and isn't assumed to contain the address at all, let
    // alone in a linkable form.
    private var billingMailtoLink: String {
        let subject = "Cavnar AI billing question"
        let encoded = subject.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? subject
        return "mailto:will@cavnar.ai?subject=\(encoded)"
    }

    private var hero: some View {
        AccountHero(title: planTitle) {
            GlowBadge(systemImage: "creditcard", size: 64)
        } subtitle: {
            if let billing = live, billing.ok, billing.status != "inactive" {
                Text(billing.amount ?? "—").font(.cavnarNumber(15.5, weight: 600))
                    + Text(" · next charge ")
                    + Text(billing.nextDate ?? "—").font(.cavnarNumber(15.5, weight: 600))
            } else if let message = live?.message {
                Text(message)
            } else if let url = URL(string: billingMailtoLink) {
                // A real Link, not a markdown link embedded in Text. The
                // markdown-in-Text approach looked identical and even
                // colored correctly, but never actually became tappable —
                // confirmed on a real device, twice, after two different
                // "should be correct" fixes. Link is the exact mechanism
                // "Contact Will" in Help & FAQ already uses successfully, so
                // this stops guessing and copies the thing that's proven to
                // work: Link owns the tap gesture and calls openURL itself,
                // it doesn't depend on Text's internal markdown-link
                // hit-testing at all, so Text+Text concatenation for
                // per-segment color is completely safe here.
                Link(destination: url) {
                    Text("Contact ").foregroundStyle(Color.cavnarInk3)
                        + Text("will@cavnar.ai").foregroundStyle(Color.cavnarEmber)
                        + Text(" to get set up").foregroundStyle(Color.cavnarInk3)
                }
            }
        }
    }

    private func statusStrip(_ billing: BillingSummary) -> some View {
        let status = billing.status ?? ""
        let good = status == "active" || status == "trialing"
        return HStack(spacing: 8) {
            AccountStatTile(label: "Status", value: status == "trialing" ? "Trial" : status.capitalized,
                            tone: good ? .cavnarGreen : .cavnarAmber)
            AccountStatTile(label: "Amount", value: billing.amount ?? "—", valueIsNumber: true)
            AccountStatTile(label: "Next charge", value: billing.nextDate ?? "—", valueIsNumber: true)
        }
    }

    private func row(_ label: String, _ value: String, isNumber: Bool = false) -> some View {
        HStack {
            Text(label).font(.cavnarBody(15.5)).foregroundStyle(Color.cavnarInk3)
            Spacer()
            Text(value)
                .font(isNumber ? .cavnarNumber(15.5, weight: 600) : .cavnarBody(15.5, weight: 600))
                .foregroundStyle(Color.cavnarInk)
        }
    }

    private func invoiceRow(_ invoice: BillingInvoice) -> some View {
        Group {
            if let urlString = invoice.pdfURL, let url = URL(string: urlString) {
                Link(destination: url) { invoiceRowContent(invoice) }
            } else {
                invoiceRowContent(invoice)
            }
        }
        .padding(.vertical, 10)
    }

    private func invoiceRowContent(_ invoice: BillingInvoice) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(invoice.date).font(.cavnarBody(15.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                Text(invoice.status.capitalized).font(.cavnarBody(15.5)).foregroundStyle(Color.cavnarInk3)
            }
            Spacer()
            Text(invoice.amount).font(.cavnarNumber(15.5, weight: 600)).foregroundStyle(Color.cavnarInk)
            if invoice.pdfURL != nil {
                Image(systemName: "arrow.down.circle")
                    .font(.system(size: 13))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
    }

    private func divider() -> some View {
        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
    }
}
