import SwiftUI

/// Pushed from Account's "Plan & payment" row: the plan and recent
/// invoices, read only. App Store Guideline 3.1.1 (iOS parity #13): the app
/// links to no payment page — the Stripe portal link and the invoice PDF
/// links are gone, and where billing is managed is said in plain text,
/// never a link or a button. Pause stays here (it stops billing; it buys
/// nothing), which is also where the paused message points: "Resume any
/// time from Account → Billing" (auth._billing_blocked_message).
struct AccountBillingDetailView: View {
    let viewModel: AccountViewModel
    let billing: BillingSummary?
    @Environment(\.scenePhase) private var scenePhase

    /// Prefer live state over the snapshot the sheet was opened with — the
    /// plan can change on the web while this sheet is backgrounded, and the
    /// snapshot would keep showing the old state forever (audit 4.1).
    private var live: BillingSummary? { viewModel.billing ?? billing }

    /// How billing is handled — plain text, deliberately not a link (3.1.1),
    /// and neutral: the app sends nobody anywhere to pay (re-audit 10/8/26,
    /// #15; the server's health card says the same, account_health.IOS_BILLING_NOTE).
    static let manageNote = "Billing is handled under your service agreement."

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
                // Past due says what to do next — in plain text, no payment
                // link (Guideline 3.1.1; re-audit L19).
                if live?.status == "past_due" {
                    Text(verbatim: "The last charge didn\u{2019}t go through. Contact will@cavnar.ai to settle it.")
                        .cavnarText(.body, color: .cavnarAmber)
                        .fixedSize(horizontal: false, vertical: true)
                }
                // The modules on the plan and what's been measured — from
                // the account health card (iOS readability round).
                if let health = viewModel.health, !health.features.isEmpty || health.measured?.line != nil {
                    AccountPlanModules(health: health)
                }
                // The figures once: amount, next charge and the card here,
                // nowhere else (not the hero, not a tile strip, not the
                // Account row).
                if let billing = live, billing.ok, billing.status != "inactive" {
                    VStack(alignment: .leading, spacing: 10) {
                        row("Next charge", billing.nextDate ?? "—", isNumber: true)
                        divider()
                        row("Amount", billing.amount ?? "—", isNumber: true)
                        divider()
                        row("Payment method", billing.paymentMethod ?? "—")
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
                            .accountCard()
                        }
                    }
                }
                // Plain text, read only: no portal, no invoice link (3.1.1).
                Text(Self.manageNote)
                    .cavnarText(.secondary)
                    .textSelection(.disabled)

                // Pause lives with billing, as the paused message says.
                AccountPauseSection()
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
        }
        .accountSheetChrome("Billing")
        // Billing is the one screen whose source of truth changes outside the
        // app: the owner updates a card on the web and comes back. Without
        // these it kept showing "Payment past due" indefinitely after they
        // had already fixed it (audit 4.1).
        .task { await viewModel.loadBilling() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await viewModel.loadBilling() } }
        }
        .cavnarEmberRefreshable { await viewModel.loadBilling() }
        }
    }

    // MARK: - Identity (option A)

    private var planTitle: String {
        // A teammate's phone: billing is the owner's, not "no plan".
        if live?.reason == "owner_only" { return "Billing is the owner's" }
        guard let billing = live, billing.ok, billing.status != "inactive", let status = billing.status else { return "No active plan" }
        switch status {
        case "active": return "Active plan"
        case "trialing": return "Free trial"
        case "past_due": return "Payment past due"
        default: return status.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    private var hero: some View {
        AccountHero(title: planTitle) {
            GlowBadge(systemImage: "creditcard", size: 64)
        } subtitle: {
            if let billing = live, billing.ok, billing.status != "inactive" {
                // The plan, by what's on it — the figures are in the card below.
                Text(planLine)
            } else if let message = live?.message {
                Text(message)
            } else {
                // Informational only — no link to sign up or pay (3.1.1).
                Text("No plan on file for this account.")
            }
        }
    }

    /// "Reviews, Labor and Food cost" — the modules on the plan, as the
    /// plan's name; "Cavnar AI" when the server didn't say.
    private var planLine: String {
        let on = (viewModel.health?.features ?? []).filter(\.on).map(\.label)
        switch on.count {
        case 0: return "Cavnar AI"
        case 1: return on[0]
        default: return on.dropLast().joined(separator: ", ") + " and " + (on.last ?? "")
        }
    }

    private func row(_ label: String, _ value: String, isNumber: Bool = false) -> some View {
        HStack {
            Text(label).cavnarText(.body)
            Spacer()
            Group {
                if isNumber {
                    HomeMixedText.make(value, role: .label)
                } else {
                    Text(value)
                }
            }
            .cavnarText(.label)
        }
    }

    /// Read only — the PDF link is not offered in the app (3.1.1); the web
    /// keeps it. The date is the server's M/D/YY.
    private func invoiceRow(_ invoice: BillingInvoice) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                HomeMixedText.make(invoice.date, role: .label)
                Text(Self.invoiceStatus(invoice.status)).cavnarText(.secondary)
            }
            Spacer()
            HomeMixedText.make(invoice.amount, role: .label)
        }
        .padding(.vertical, 10)
        .accessibilityElement(children: .combine)
    }

    /// The web's words for Stripe's invoice states (_INV_STATUS).
    static func invoiceStatus(_ raw: String) -> String {
        switch raw {
        case "paid": return "Paid"
        case "open": return "Due"
        case "draft": return "Draft"
        case "void": return "Void"
        case "uncollectible": return "Unpaid"
        default: return raw.capitalized
        }
    }

    private func divider() -> some View {
        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
    }
}
