import SwiftUI

/// Count the bar — the shelf count behind the bar, after close, on the
/// phone that is in the owner's hand there. Three values at what was paid
/// (beer, liquor, wine), the day counted (today by default, never a day
/// still to come) and an optional note, saved through POST
/// /mobile/api/food-cost/bar/counts (bar.counts.save_count). Only the
/// categories typed are sent; a blank box is left as it is, never a 0.
/// Built from the Account Sheet Kit like every other small form sheet.
struct FoodCostBarCountSheet: View {
    /// Called with the confirmation line once the server saved the count.
    var onSaved: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    @FocusState private var focus: Field?
    @State private var beer = ""
    @State private var liquor = ""
    @State private var wine = ""
    @State private var note = ""
    @State private var countDate = CavnarDate.isoDay(Date())
    @State private var busy = false
    @State private var error: BarCountError?

    enum Field: Hashable { case beer, liquor, wine, note }

    private var isDirty: Bool {
        ![beer, liquor, wine, note].allSatisfy { $0.trimmingCharacters(in: .whitespaces).isEmpty }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    AccountHero(title: "Count the bar") {
                        GlowBadge(systemImage: "wineglass", size: 64)
                    } subtitle: {
                        Text("What's on the shelves, at cost")
                    }

                    Text("Count after close. Value each category at what you paid; a full keg without its deposit.")
                        .cavnarText(.body)
                        .fixedSize(horizontal: false, vertical: true)

                    AccountSection(kicker: "Value at cost, in dollars") {
                        valueField("Beer & seltzer", text: $beer, field: .beer, key: "beer")
                        valueField("Liquor", text: $liquor, field: .liquor, key: "liquor")
                        valueField("Wine", text: $wine, field: .wine, key: "wine", last: true)
                    }

                    AccountSection(kicker: "When") {
                        AccountKVRow(label: "Counted on", showsDivider: false) {
                            CavnarDateChip(iso: $countDate, latest: Date(), accessibilityName: "Counted on")
                        }
                    }

                    AccountSection(kicker: "Note") {
                        AccountEditor(label: "Optional", placeholder: "Anything worth knowing about this count",
                                      text: $note, focus: $focus, field: .note, showsDivider: false)
                    }

                    if let error, error.field == nil {
                        Text(error.message)
                            .font(.cavnar(.secondary))
                            .foregroundStyle(Color.cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    Button {
                        focus = nil
                        Task { await save() }
                    } label: {
                        Group {
                            if busy { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Save the count") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: busy))
                    .disabled(busy)
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.interactively)
            .accountSheetChrome("Count the Bar", isDirty: isDirty && !busy)
            .keyboardDoneToolbar { focus = nil }
        }
    }

    private func valueField(_ label: String, text: Binding<String>, field: Field, key: String,
                            last: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            AccountField(label: label, text: text, focus: $focus, field: field,
                         keyboardType: .decimalPad, isNumber: true, showsDivider: !last)
            if let error, error.field == key {
                Text(error.message)
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarRedText)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 6)
            }
        }
    }

    private func save() async {
        let built = BarCountBody.make(date: countDate, beer: beer, liquor: liquor, wine: wine, note: note)
        let body: BarCountBody
        switch built {
        case .failure(let e):
            error = e
            Haptic.error()
            return
        case .success(let b):
            body = b
        }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let r: BarCountResponse = try await APIClient.shared.send(
                "/mobile/api/food-cost/bar/counts", method: .post, body: body,
                hapticOnError: false, retryTransient: false)
            guard r.ok else {
                error = BarCountError(field: nil, message: r.error ?? "That count didn\u{2019}t save.")
                Haptic.error()
                return
            }
            Haptic.success()
            onSaved(r.countDateMdy.map { "Bar count saved for \($0)" } ?? "Bar count saved")
            dismiss()
        } catch is CancellationError {
        } catch let e as APIClient.APIError {
            // A 400 carries the server's own sentence ("A count can't be
            // dated in the future."). A count whose answer was lost may
            // have saved — say so rather than invite a second one.
            let lost = e.status == nil && e.mayHaveReachedServer
            error = BarCountError(field: nil, message: lost
                ? "Couldn\u{2019}t confirm the count saved \u{2014} check the Bar page before saving again."
                : e.message)
            Haptic.error()
        } catch {
            self.error = BarCountError(field: nil, message: "That count didn\u{2019}t save.")
            Haptic.error()
        }
    }
}
