import SwiftUI

/// The PAR hours check under a draft and a past week (schedule audit
/// 10/3/26 D-1, E-7/P-6, E-24). The budget is the HOURLY crew's — the
/// labor target counts salaries, so the salaried pay comes off it first —
/// and only the hourly hours are held against it: "298h hourly of 312h
/// budget · 110h salaried". It read "Budgeted 312h" against every hour on
/// the week, salaried included, so a week with two salaried owners on it
/// read over budget when it wasn't.
struct ParHoursCheck: View {
    let budget: Double
    /// Every hour on the week (the older payloads' only figure).
    let scheduled: Double
    var hourly: Double? = nil
    var salaried: Double? = nil
    var dollars: Double? = nil
    var basis: BudgetBasis? = nil

    /// The hours the budget judges: the hourly ones when the week says
    /// them, else every hour (a week saved before the split).
    private var judged: Double { hourly ?? scheduled }

    var body: some View {
        let diff = judged - budget
        let withinRange = abs(diff) <= max(budget * 0.05, 1)
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    CavnarKicker("Hours against the budget")
                    HomeMixedText.make("Hourly budget \(budget.commaFormatted)h"
                                       + (dollars.flatMap { $0 > 0 ? " (\(SetupWords.dollars($0)))" : nil } ?? "")
                                       + " for the week", size: 14, color: .cavnarInk2)
                    if let hourly {
                        HomeMixedText.make("\(hourly.commaFormatted)h hourly of \(budget.commaFormatted)h budget"
                                           + ((salaried ?? 0) > 0 ? " \u{00B7} \(salaried!.commaFormatted)h salaried" : ""),
                                           size: 13, color: .cavnarInk3)
                    }
                }
                Spacer()
                Text(withinRange ? "On budget" : (diff > 0 ? "+\(diff.commaFormatted)h over" : "\(diff.commaFormatted)h under"))
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(withinRange ? Color.cavnarGreen : Color.cavnarAmber)
            }
            if let text = basis?.text {
                HomeMixedText.make(text, size: 12.5, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let caveat = basis?.caveat {
                HomeMixedText.make(caveat + " Pay rates are set on the web, in Account \u{2192} Targets & pay rates.",
                                   size: 12.5, weight: 600, color: .cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(10)
        .background(Color.cavnarPaper2.opacity(0.6))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }
}
