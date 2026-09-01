import SwiftUI

/// Pushed from Account's "Plan & payment" row. Adds recent invoices to
/// what the old inline card showed (next charge / amount / payment
/// method / manage link only) — mobile_api.py's billing route now
/// includes a short invoice history from the same Stripe customer.
struct AccountBillingDetailView: View {
    let billing: BillingSummary?

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
            VStack(alignment: .leading, spacing: 24) {
                if let billing, billing.ok, billing.status != "inactive" {
                    VStack(alignment: .leading, spacing: 10) {
                        row("Next charge", billing.nextDate ?? "—")
                        divider()
                        row("Amount", billing.amount ?? "—", isNumber: true)
                        divider()
                        row("Payment method", billing.paymentMethod ?? "—")
                        if let urlString = billing.portalURL, let url = URL(string: urlString) {
                            divider()
                            Link(destination: url) {
                                HStack {
                                    Text("Manage payment method").font(.cavnarBody(14.5, weight: 600))
                                    Spacer()
                                    Image(systemName: "arrow.up.right").font(.system(size: 11))
                                }
                            }
                            .foregroundStyle(Color.cavnarEmber)
                        }
                    }
                    .cavnarCard()

                    if let invoices = billing.invoices, !invoices.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("RECENT INVOICES")
                                .font(.cavnarBody(14.5, weight: 700))
                                .foregroundStyle(Color.cavnarInk3)
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
                } else {
                    Text(billing?.message ?? "No active subscription. Contact will@cavnar.ai")
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
        }
        .cavnarModuleBackground()
        .navigationTitle("Billing")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("Billing") }
        }
    }

    private func row(_ label: String, _ value: String, isNumber: Bool = false) -> some View {
        HStack {
            Text(label).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk3)
            Spacer()
            Text(value)
                .font(isNumber ? .cavnarNumber(14.5, weight: 600) : .cavnarBody(14.5, weight: 600))
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
                Text(invoice.date).font(.cavnarBody(14.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                Text(invoice.status.capitalized).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk3)
            }
            Spacer()
            Text(invoice.amount).font(.cavnarNumber(14.5, weight: 600)).foregroundStyle(Color.cavnarInk)
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
