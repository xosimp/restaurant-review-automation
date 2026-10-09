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

    /// The verdict, with the PAR budget read as a CEILING (re-audit 10/8/26
    /// H2): any hour over it is "over" in amber — a 5% band used to read a
    /// week up to 15h over as a green "On budget". At or under it is on
    /// budget. Rounding to a tenth keeps 312.04 against 312 from reading over.
    static func verdict(judged: Double, budget: Double) -> (text: String, over: Bool) {
        let diff = ((judged - budget) * 10).rounded() / 10
        if diff > 0 { return ("+\(diff.commaFormatted)h over", true) }
        return ("On budget", false)
    }

    var body: some View {
        let verdict = Self.verdict(judged: judged, budget: budget)
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    CavnarKicker("Hours against the budget")
                    HomeMixedText.make("Hourly budget \(budget.commaFormatted)h"
                                       + (dollars.flatMap { $0 > 0 ? " (\(SetupWords.dollars($0)))" : nil } ?? "")
                                       + " for the week", size: CavnarType.secondary, color: .cavnarInk2)
                    if let hourly {
                        HomeMixedText.make("\(hourly.commaFormatted)h hourly of \(budget.commaFormatted)h budget"
                                           + ((salaried ?? 0) > 0 ? " \u{00B7} \(salaried!.commaFormatted)h salaried" : ""),
                                           size: CavnarType.caption, color: .cavnarInk2)
                    }
                }
                Spacer()
                HomeMixedText.make(verdict.text, size: CavnarType.secondary, weight: 700,
                                   color: verdict.over ? .cavnarAmber : .cavnarGreen)
            }
            if let text = basis?.text {
                HomeMixedText.make(text, size: CavnarType.caption, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let caveat = basis?.caveat {
                HomeMixedText.make(caveat, size: CavnarType.caption, weight: 600, color: .cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
                // Account → Targets & pay rates sits in the web's
                // Restaurant section (M6).
                CavnarWebLinkRow(title: "Pay rates", subtitle: "Account \u{2192} Targets & pay rates",
                                 path: "account/restaurant")
            }
        }
        .padding(10)
        .background(Color.cavnarPaper2.opacity(0.6))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }
}
