import SwiftUI

/// Scheduling setup → Next week's forecast (schedule audit 10/3/26 D-1, D-2,
/// E-24, D-23, D-24, D-33): the week before anything is drafted — each
/// day's forecast sales, how far it sits from a usual one and why, and the
/// hours the draft will be given. The budget is the HOURLY crew's: the labor
/// target counts salaries, so the salaried pay comes off it first, and the
/// basis says so; when the wage is partly assumed, the caveat says that too.
/// GET labor/schedule-forecast, the same numbers the draft is handed.
struct ForecastPreviewSection: View {
    @Bindable var store: TeamSetupStore
    @State private var expanded = false

    private var forecast: ForecastPreview? { store.forecast }

    var body: some View {
        CavnarDropdown(
            title: "Next week\u{2019}s forecast",
            subtitle: subtitle,
            tone: .neutral,
            isExpanded: $expanded,
            onExpand: { Task { await store.loadForecast() } }
        ) {
            VStack(alignment: .leading, spacing: 14) {
                if store.isLoadingForecast && forecast == nil {
                    CavnarSkeletonBar(height: 3)
                        .padding(.vertical, 8)
                        .accessibilityLabel("Loading next week\u{2019}s forecast")
                } else if let error = store.forecastError, forecast == nil {
                    Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                } else if let f = forecast {
                    if !f.available {
                        Text(f.reason ?? "There isn\u{2019}t enough sales history yet to forecast next week.")
                            .font(.cavnar(.secondary))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        freshness(f)
                        figures(f)
                        basis(f)
                        days(f)
                    }
                }
            }
        }
    }

    private var subtitle: String {
        guard let f = forecast, f.available else { return "What the draft will be given" }
        var parts: [String] = []
        if let rev = f.projectedRevenue, rev > 0 { parts.append(SetupWords.dollars(rev) + " projected") }
        if let h = f.hoursBudget, h > 0 { parts.append("\(h.commaFormatted)h hourly budget") }
        return parts.isEmpty ? "What the draft will be given" : parts.joined(separator: " \u{00B7} ")
    }

    // MARK: Freshness (D-33)

    @ViewBuilder
    private func freshness(_ f: ForecastPreview) -> some View {
        if let d = f.dataThrough {
            if d.blocked {
                CavnarCaveat(title: "Sales stopped coming in",
                             detail: d.message ?? d.line ?? "The forecast can\u{2019}t be trusted until sales are current.")
            } else if let line = d.line {
                HomeMixedText.make(line, size: CavnarType.caption, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: The week's money and hours

    private func figures(_ f: ForecastPreview) -> some View {
        HStack(alignment: .top, spacing: 10) {
            DSRStatTile(label: "Projected sales",
                        value: f.projectedRevenue.map(SetupWords.dollars) ?? "\u{2014}",
                        detail: f.projectedRevenueSource)
            DSRStatTile(label: "Hourly budget",
                        value: f.hoursBudget.map { "\($0.commaFormatted)h" } ?? "\u{2014}",
                        detail: f.laborBudgetDollars.flatMap { $0 > 0 ? SetupWords.dollars($0) + " for hourly staff" : nil })
        }
    }

    /// "Your 35% labor target counts salaries: the hourly budget is the
    /// target's dollars less the 2 salaried people's pay…" — never "$X at
    /// 35%". The salaried dollars ride only for the owner.
    @ViewBuilder
    private func basis(_ f: ForecastPreview) -> some View {
        if let b = f.budgetBasis {
            VStack(alignment: .leading, spacing: 6) {
                if let text = b.text {
                    HomeMixedText.make(text, size: CavnarType.secondary, color: b.salariesExceedTarget == true ? .cavnarAmber : .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let target = b.targetDollars, let salaried = b.salariedWeekCost {
                    HomeMixedText.make("Target \(SetupWords.dollars(target)) for the week \u{2212} salaried pay "
                                       + "\(SetupWords.dollars(salaried)) = \(SetupWords.dollars(max(0, target - salaried))) hourly.",
                                       size: CavnarType.caption, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let caveat = b.caveat {
                    // The wage behind the hours is partly a guess — set pay
                    // rates (web, Account → Targets & pay rates) and it's
                    // measured (E-24).
                    CavnarCaveat(title: "Set pay rates",
                                 detail: caveat + " Pay rates are set on the web, in Account \u{2192} Targets & pay rates.")
                }
            }
        }
    }

    // MARK: Each day (D-23, D-24)

    private func days(_ f: ForecastPreview) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(f.days.enumerated()), id: \.element.id) { index, day in
                dayRow(day, reasons: f.dailyTargetReasons[day.date] ?? [])
                if index < f.days.count - 1 { AccountRowDivider() }
            }
        }
        .accountCard()
    }

    private func dayRow(_ day: ForecastPreview.Day, reasons: [String]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                HomeMixedText.make("\(day.weekday.prefix(3)) \(CavnarDate.mdy(day.date))", size: CavnarType.body, weight: 600,
                                   color: day.closed ? .cavnarInk3 : .cavnarInk)
                if let pct = day.demand?.pct, Int(pct.rounded()) != 0, !day.closed {
                    Text(SetupWords.signedPct(pct))
                        .font(.cavnarNumber(CavnarType.caption, weight: 700))
                        .foregroundStyle(pct > 0 ? Color.cavnarGreen : Color.cavnarAmber)
                }
                Spacer(minLength: 6)
                if day.closed {
                    Text("Closed").font(.cavnarBody(CavnarType.secondary, weight: 600)).foregroundStyle(Color.cavnarInk3)
                } else if let h = day.hours {
                    Text("\(h.commaFormatted)h")
                        .font(.cavnarNumber(CavnarType.body, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                }
            }
            if !day.closed {
                if let sales = day.sales {
                    let range = (day.low != nil && day.high != nil)
                        ? " (\(SetupWords.dollars(day.low!))\u{2013}\(SetupWords.dollars(day.high!)))" : ""
                    HomeMixedText.make("Sales about \(SetupWords.dollars(sales))\(range)", size: CavnarType.caption, color: .cavnarInk3)
                } else if let why = day.reason {
                    HomeMixedText.make(why, size: CavnarType.caption, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                // Why the day sits off a usual one, and why its hours
                // moved: "+30% — Homecoming", "−15% — rain forecast".
                ForEach(Array(Set(day.demand?.reasons ?? []).union(reasons)).sorted(), id: \.self) { why in
                    HomeMixedText.make(why, size: CavnarType.caption, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !day.effects.isEmpty {
                    HomeMixedText.make(day.effects.joined(separator: " \u{00B7} "), size: CavnarType.caption, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.vertical, 9)
    }
}
