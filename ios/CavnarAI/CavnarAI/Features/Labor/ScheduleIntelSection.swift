import SwiftUI

/// What the record says — the measured layer behind the draft: how each
/// weekday's dayparts have gone, sales per labor hour, who has carried
/// the weekends and closes, what staff keep dropping and claiming, who
/// has been trained up, and the pairs the record suggests.
///
/// Every number here is read from published weeks and the shift ledger.
/// A daypart with no data is absent, never a zero.
struct ScheduleIntelSection: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    var onExpand: (() -> Void)? = nil
    var onAddPair: ((SuggestedPair) -> Void)? = nil

    private static let dayOrder = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    private static let dayparts = ["morning", "night"]

    var body: some View {
        CavnarDropdown(
            title: "What the record says",
            subtitle: subtitle,
            tone: .neutral,
            isExpanded: $viewModel.intelExpanded,
            onExpand: { onExpand?(); Task { await viewModel.loadIntel() } }
        ) {
            VStack(alignment: .leading, spacing: 18) {
                if viewModel.isLoadingIntel && viewModel.intel == nil {
                    CavnarSkeletonLines(widths: [1.0, 0.8, 0.9, 0.6])
                } else if let error = viewModel.intelError, viewModel.intel == nil {
                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                } else if let intel = viewModel.intel, !intel.isEmpty {
                    if let revenue = intel.revenue, let value = revenue.value { revenueLine(revenue, value: value) }
                    if let outcomes = intel.outcomes, !outcomes.isEmpty { outcomesBlock(outcomes) }
                    if let splh = intel.splh, !splh.isEmpty { splhBlock(splh) }
                    if let ledger = intel.ledger, !ledger.isEmpty { ledgerBlock(ledger) }
                    if let behaviour = intel.behaviour, !behaviour.isEmpty { behaviourBlock(behaviour) }
                    if let could = intel.couldHold, !could.isEmpty { trainedUpBlock(could) }
                    if let pairs = intel.suggestedPairs, !pairs.isEmpty { pairsBlock(pairs) }
                    if let hidden = intel.suppressedRecommendationKinds, !hidden.isEmpty { hiddenKindsLine(hidden) }
                    Text("Measured from published weeks and the shift ledger — not written by the model.")
                        .font(.cavnarBody(12.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("Nothing recorded yet — the first published week writes the first row.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private var subtitle: String {
        guard let intel = viewModel.intel, !intel.isEmpty else { return "Outcomes, rotation and what staff keep doing" }
        var parts: [String] = []
        if let n = intel.outcomes?.count, n > 0 { parts.append("\(n) weekdays measured") }
        if let n = intel.suggestedPairs?.count, n > 0 { parts.append("\(n) suggested \(n == 1 ? "pair" : "pairs")") }
        return parts.isEmpty ? "Outcomes, rotation and what staff keep doing" : parts.joined(separator: " · ")
    }

    private func kicker(_ text: String, tone: Color = .cavnarInk3) -> some View {
        Text(text.uppercased())
            .font(.cavnarBody(11.5, weight: 700))
            .tracking(1.1)
            .foregroundStyle(tone)
    }

    // MARK: Revenue

    private func revenueLine(_ revenue: IntelRevenue, value: Double) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("$\(value.commaFormatted)")
                .font(.cavnarNumber(22, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .cavnarSensitive()
            VStack(alignment: .leading, spacing: 1) {
                Text("a week, projected")
                    .font(.cavnarBody(13.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk2)
                if let source = revenue.source, !source.isEmpty {
                    HomeMixedText.make(source, size: 12.5, color: .cavnarInk3)
                }
            }
            Spacer(minLength: 0)
        }
    }

    // MARK: Outcomes

    private func orderedDays(_ keys: some Collection<String>) -> [String] {
        Self.dayOrder.filter { keys.contains($0) } + keys.filter { !Self.dayOrder.contains($0) }.sorted()
    }

    private func outcomesBlock(_ outcomes: [String: [String: IntelOutcome]]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            kicker("How each daypart has gone")
            Text("Average sales and hours over the weeks recorded. A troubled daypart has gone wrong before — the draft will not thin it.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            VStack(spacing: 0) {
                ForEach(orderedDays(outcomes.keys), id: \.self) { day in
                    ForEach(Self.dayparts.filter { outcomes[day]?[$0] != nil }, id: \.self) { part in
                        if let o = outcomes[day]?[part] { outcomeRow(day: day, part: part, o) }
                    }
                }
            }
            .accountCard()
        }
    }

    private func outcomeRow(day: String, part: String, _ o: IntelOutcome) -> some View {
        let troubled = o.troubled == true
        return VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text("\(String(day.prefix(3))) \(part == "morning" ? "day" : "night")")
                            .font(.cavnarBody(14.5, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        if troubled {
                            Text("TROUBLED")
                                .font(.cavnarBody(9, weight: 700))
                                .tracking(0.5)
                                .foregroundStyle(Color.cavnarAmber)
                                .padding(.horizontal, 4)
                                .padding(.vertical, 1)
                                .background(Capsule().fill(Color.cavnarAmber.opacity(0.16)))
                        }
                    }
                    HomeMixedText.make(outcomeDetail(o), size: 12.5, color: .cavnarInk3)
                }
                Spacer(minLength: 6)
                if let splh = o.splh {
                    VStack(alignment: .trailing, spacing: 1) {
                        Text("$\(splh.commaFormatted)")
                            .font(.cavnarNumber(15, weight: 700))
                            .foregroundStyle(troubled ? Color.cavnarAmber : Color.cavnarInk)
                            .cavnarSensitive()
                        Text("per labor hour")
                            .font(.cavnarBody(10.5))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
            }
            .padding(.vertical, 8)
            .padding(.horizontal, troubled ? 8 : 0)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(troubled ? Color.cavnarAmber.opacity(0.08) : Color.clear))
            AccountRowDivider()
        }
    }

    private func outcomeDetail(_ o: IntelOutcome) -> String {
        var parts: [String] = []
        if let w = o.weeks, w > 0 { parts.append("\(w) \(w == 1 ? "week" : "weeks")") }
        if let h = o.avgHours, h > 0 { parts.append("\(h.commaFormatted)h") }
        if let s = o.avgSales, s > 0 { parts.append("$\(s.commaFormatted) sales") }
        if let i = o.issues, i > 0 { parts.append("\(i) \(i == 1 ? "issue" : "issues")") }
        if let r = o.rating { parts.append("rated \(String(format: "%.1f", r))") }
        return parts.joined(separator: " · ")
    }

    // MARK: Sales per labor hour

    private func splhBlock(_ splh: [String: [String: IntelSplh]]) -> some View {
        let entries: [(String, String, IntelSplh)] = orderedDays(splh.keys).flatMap { day in
            Self.dayparts.compactMap { part in (splh[day]?[part]).map { (day, part, $0) } }
        }
        let maxValue = entries.compactMap { $0.2.splh }.max() ?? 0
        return VStack(alignment: .leading, spacing: 8) {
            kicker("Sales per labor hour")
            if maxValue > 0 {
                VStack(spacing: 6) {
                    ForEach(Array(entries.enumerated()), id: \.offset) { _, e in
                        splhBar(day: e.0, part: e.1, value: e.2.splh ?? 0, max: maxValue)
                    }
                }
            } else {
                Text("No sales lined up against hours yet.")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
    }

    /// One glowing ember bar per daypart, longest = the best hour.
    private func splhBar(day: String, part: String, value: Double, max: Double) -> some View {
        let fraction = max > 0 ? CGFloat(value / max) : 0
        return HStack(spacing: 8) {
            Text("\(String(day.prefix(3))) \(part == "morning" ? "day" : "night")")
                .font(.cavnarBody(12.5, weight: 600))
                .foregroundStyle(Color.cavnarInk2)
                .frame(width: 72, alignment: .leading)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.white.opacity(0.05))
                    Capsule()
                        .fill(LinearGradient(colors: [Color.cavnarEmber2, Color.cavnarEmber],
                                             startPoint: .leading, endPoint: .trailing))
                        .frame(width: Swift.max(6, geo.size.width * fraction))
                        .shadow(color: Color.cavnarEmber.opacity(0.45), radius: 5)
                }
            }
            .frame(height: 10)
            Text("$\(value.commaFormatted)")
                .font(.cavnarNumber(13, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .frame(width: 52, alignment: .trailing)
                .cavnarSensitive()
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(day) \(part): \(Int(value)) dollars per labor hour")
    }

    // MARK: Rotation ledger

    private func ledgerBlock(_ ledger: [String: IntelLedgerEntry]) -> some View {
        let rows = ledger.map { ($0.key, $0.value) }
        let byWeekend = rows.sorted { ($0.1.weekend ?? 0, $0.0) > ($1.1.weekend ?? 0, $1.0) }
        let byClosing = rows.sorted { ($0.1.closing ?? 0, $0.0) > ($1.1.closing ?? 0, $1.0) }
        let weeks = rows.compactMap { $0.1.weeks }.max()
        return VStack(alignment: .leading, spacing: 8) {
            kicker("Rotation")
            Text(weeks.map { "Weekend and closing shifts over the last \($0) weeks — who has carried the most, and the least." }
                 ?? "Weekend and closing shifts lately — who has carried the most, and the least.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .top, spacing: 12) {
                ledgerColumn("Weekends", top: byWeekend.prefix(3).map { ($0.0, $0.1.weekend ?? 0) },
                             bottom: byWeekend.suffix(3).reversed().map { ($0.0, $0.1.weekend ?? 0) }, count: rows.count)
                ledgerColumn("Closes", top: byClosing.prefix(3).map { ($0.0, $0.1.closing ?? 0) },
                             bottom: byClosing.suffix(3).reversed().map { ($0.0, $0.1.closing ?? 0) }, count: rows.count)
            }
        }
    }

    private func ledgerColumn(_ label: String, top: [(String, Int)], bottom: [(String, Int)], count: Int) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label)
                .font(.cavnarBody(13.5, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            ForEach(Array(top.enumerated()), id: \.offset) { _, e in ledgerLine(e.0, e.1, tone: .cavnarEmber) }
            if count > 3 {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1).padding(.vertical, 2)
                ForEach(Array(bottom.enumerated()), id: \.offset) { _, e in ledgerLine(e.0, e.1, tone: .cavnarInk3) }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func ledgerLine(_ name: String, _ n: Int, tone: Color) -> some View {
        HStack(spacing: 6) {
            Text("\(n)")
                .font(.cavnarNumber(13.5, weight: 700))
                .foregroundStyle(tone)
                .frame(width: 22, alignment: .trailing)
            Text(name)
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk2)
                .lineLimit(1)
        }
    }

    // MARK: Behaviour

    private func behaviourBlock(_ behaviour: [String: IntelBehaviour]) -> some View {
        let rows = behaviour.filter { !($0.value.avoids ?? []).isEmpty || !($0.value.prefers ?? []).isEmpty
            || ($0.value.drops ?? 0) > 0 || ($0.value.claims ?? 0) > 0 }
            .sorted { $0.key < $1.key }
        return VStack(alignment: .leading, spacing: 8) {
            kicker("What staff keep doing")
            if rows.isEmpty {
                Text("No drops or claims on record yet.")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
            }
            ForEach(rows, id: \.key) { name, b in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 8) {
                        Text(name)
                            .font(.cavnarBody(14.5, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        Spacer(minLength: 4)
                        HomeMixedText.make(
                            [(b.drops ?? 0) > 0 ? "\(b.drops ?? 0) dropped" : nil,
                             (b.claims ?? 0) > 0 ? "\(b.claims ?? 0) claimed" : nil]
                                .compactMap { $0 }.joined(separator: " · "),
                            size: 12.5, weight: 600, color: .cavnarInk3)
                    }
                    if let avoids = b.avoids, !avoids.isEmpty {
                        HomeMixedText.make("keeps asking to drop \(avoids.joined(separator: ", "))", size: 13, color: .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let prefers = b.prefers, !prefers.isEmpty {
                        HomeMixedText.make("keeps picking up \(prefers.joined(separator: ", "))", size: 13, color: .cavnarGreen)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.vertical, 4)
            }
        }
    }

    // MARK: Trained up

    private func trainedUpBlock(_ could: [String: [String]]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            kicker("Trained up")
            Text("Roles somebody has covered enough shifts in, beside a rated colleague, to hold on their own.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(could.keys.sorted(), id: \.self) { name in
                HStack(alignment: .top, spacing: 8) {
                    Text(name)
                        .font(.cavnarBody(14, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .frame(minWidth: 70, alignment: .leading)
                    AccountFlowLayout(spacing: 6) {
                        ForEach(could[name] ?? [], id: \.self) { role in
                            AccountChip(text: role)
                        }
                    }
                }
                .padding(.vertical, 2)
            }
        }
    }

    // MARK: Suggested pairs

    private func pairsBlock(_ pairs: [SuggestedPair]) -> some View {
        let open = pairs.filter { !viewModel.ignoredSuggestions.contains($0.id) }
        return VStack(alignment: .leading, spacing: 8) {
            kicker("Pairs the record suggests")
            if open.isEmpty {
                Text("Nothing new to suggest.")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
            }
            ForEach(open) { pair in
                SuggestedPairRow(pair: pair, busy: viewModel.isSavingPair) {
                    onAddPair?(pair)
                } onIgnore: {
                    viewModel.ignoredSuggestions.insert(pair.id)
                }
            }
        }
    }

    private func hiddenKindsLine(_ hidden: [String]) -> some View {
        HomeMixedText.make(
            "\(hidden.count) recommendation \(hidden.count == 1 ? "kind" : "kinds") hidden because they were never acted on: \(hidden.joined(separator: ", ")). Accept one of that kind and it comes back.",
            size: 12.5, color: .cavnarInk3)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// One suggested pair: the two names, the evidence, Add / Ignore. Add
/// creates the pair through the same POST the pair editor uses.
struct SuggestedPairRow: View {
    let pair: SuggestedPair
    var busy: Bool = false
    let onAdd: () -> Void
    let onIgnore: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: "person.2.fill")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Color.cavnarGreen)
                Text("\(pair.a) with \(pair.b)")
                    .font(.cavnarBody(14.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Spacer(minLength: 4)
                if let rate = pair.cleanRate {
                    Text("\(Int((rate * 100).rounded()))%")
                        .font(.cavnarNumber(13.5, weight: 700))
                        .foregroundStyle(Color.cavnarGreen)
                        .accessibilityLabel("\(Int((rate * 100).rounded())) percent clean")
                }
            }
            if let evidence = pair.evidence, !evidence.isEmpty {
                HomeMixedText.make(evidence, size: 13, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                Button {
                    Haptic.light()
                    onIgnore()
                } label: { Text("Ignore").frame(maxWidth: .infinity) }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                Button {
                    Haptic.light()
                    onAdd()
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: "plus").font(.system(size: 11, weight: .bold))
                        Text("Add pair")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(busy)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.cavnarGreen.opacity(0.06)))
    }
}
