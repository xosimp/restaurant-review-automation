import SwiftUI

/// Scheduling setup → Labor standards (schedule audit 10/3/26 D-25): how
/// much work one person does in an hour here — guests per server-hour, bar
/// tickets per bartender-hour, tickets per cook-hour — measured from the
/// punches and the ticket archive, with the owner's own figure over it.
/// A standard of the owner's sizes that role's people per shift by the
/// day's demand; a measured one is reported only. One role at a time: a
/// save sends that role alone, never the whole set back.
struct LaborStandardsSection: View {
    @Bindable var store: TeamSetupStore
    @State private var expanded = false
    @State private var editing: String?
    @State private var lunch = ""
    @State private var dinner = ""
    @FocusState private var focused: String?

    private var payload: LaborStandardsPayload? { store.standards }

    /// Families with a standard of either kind, in a steady order.
    private var shown: [String] {
        (payload?.standards.keys.sorted() ?? [])
    }

    /// Families the archive can measure that have no standard yet.
    private var others: [String] {
        (payload?.families ?? []).filter { payload?.standards[$0] == nil }
    }

    var body: some View {
        CavnarDropdown(
            title: "Labor standards",
            subtitle: subtitle,
            tone: .neutral,
            isExpanded: $expanded,
            onExpand: { Task { await store.loadStandards() } }
        ) {
            VStack(alignment: .leading, spacing: 14) {
                Text("How much work one person does in an hour here \u{2014} guests per server-hour, bar tickets per bartender-hour, tickets per cook-hour. Measured from your punches and tickets. Set your own and the draft sizes that role by it and the day\u{2019}s demand.")
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)

                if store.isLoadingStandards && payload == nil {
                    CavnarSkeletonBar(height: 3)
                        .padding(.vertical, 8)
                        .accessibilityLabel("Loading the labor standards")
                } else if let error = store.standardsError, payload == nil {
                    Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                } else if payload != nil {
                    if shown.isEmpty {
                        Text("Nothing measured yet \u{2014} it needs six shifts of a role with tickets on file. You can still set your own below.")
                            .font(.cavnar(.secondary))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    ForEach(shown, id: \.self) { family in familyCard(family) }
                    if let family = editing, payload?.standards[family] == nil {
                        editor(family)
                    }
                    if payload?.canEdit == true && !others.isEmpty && editing == nil {
                        SetupSelectMenu(current: "Set a standard for another role",
                                        options: others.map { ($0, Self.label($0)) }) { family in
                            startEditing(family)
                        }
                    }
                    if let error = store.standardsError {
                        Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if payload?.canEdit != true {
                        Text("Only the account owner sets the labor standards.")
                            .font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
            }
        }
    }

    private var subtitle: String {
        guard let p = payload else { return "Work per person-hour, measured" }
        let yours = p.standards.values.filter { $0.values.contains(where: \.isYours) }.count
        let measured = p.standards.count - yours
        if p.standards.isEmpty { return "Nothing measured yet" }
        var parts: [String] = []
        if measured > 0 { parts.append("\(measured) measured") }
        if yours > 0 { parts.append("\(yours) yours") }
        return parts.joined(separator: " \u{00B7} ")
    }

    private func familyCard(_ family: String) -> some View {
        let parts = payload?.standards[family] ?? [:]
        let isYours = parts.values.contains(where: \.isYours)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(Self.label(family))
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarInk)
                AccountChip(text: isYours ? "Yours" : "Measured", muted: !isYours)
                Spacer(minLength: 0)
            }
            ForEach(["morning", "night"], id: \.self) { part in
                if let s = parts[part] {
                    HomeMixedText.make(s.text ?? Self.fallbackText(s, part: part, family: family), size: CavnarType.secondary,
                                       color: s.isYours ? .cavnarInk2 : .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if editing == family {
                editor(family)
            } else if payload?.canEdit == true {
                HStack(spacing: 18) {
                    Button {
                        startEditing(family)
                    } label: {
                        Text(isYours ? "Change yours" : "Set yours")
                            .font(.cavnarBody(CavnarType.secondary, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                            .frame(minHeight: 32)
                    }
                    .buttonStyle(.plain)
                    if isYours {
                        Button {
                            Task { await store.setStandard(family: family, lunch: nil, dinner: nil, remove: true) }
                        } label: {
                            Text("Back to measured")
                                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                .foregroundStyle(Color.cavnarInk3)
                                .frame(minHeight: 32)
                        }
                        .buttonStyle(.plain)
                        .disabled(store.standardBusy == family)
                    }
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
            .fill(Color.cavnarPaper2.opacity(0.5)))
    }

    private func editor(_ family: String) -> some View {
        let unit = payload?.units[family] ?? "work"
        return VStack(alignment: .leading, spacing: 10) {
            if payload?.standards[family] == nil {
                Text(Self.label(family))
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarInk)
            }
            HStack(spacing: 10) {
                field("Lunch", text: $lunch, id: "\(family)|lunch")
                field("Dinner", text: $dinner, id: "\(family)|dinner")
            }
            Text("\(unit.capitalized) per \(Self.unitWho(family))-hour. Leave one blank to keep it as it is.")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
            HStack(spacing: 12) {
                Button {
                    focused = nil
                    let l = Self.number(lunch), d = Self.number(dinner)
                    Task {
                        if await store.setStandard(family: family, lunch: l, dinner: d) { editing = nil }
                    }
                } label: {
                    Group {
                        if store.standardBusy == family { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Save") }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: Self.number(lunch) == nil && Self.number(dinner) == nil))
                .disabled(store.standardBusy != nil || (Self.number(lunch) == nil && Self.number(dinner) == nil))
                Button {
                    focused = nil
                    editing = nil
                } label: {
                    Text("Cancel").font(.cavnarBody(CavnarType.secondary, weight: 700)).foregroundStyle(Color.cavnarInk3)
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func field(_ label: String, text: Binding<String>, id: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label.uppercased())
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .tracking(0.8)
                .foregroundStyle(Color.cavnarInk3)
            TextField("\u{2014}", text: text)
                .font(.cavnarNumber(CavnarType.body, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .keyboardType(.decimalPad)
                .multilineTextAlignment(.center)
                .focused($focused, equals: id)
                .frame(height: 34)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.cavnarPaper2))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(focused == id ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1))
        }
    }

    private func startEditing(_ family: String) {
        let parts = payload?.standards[family] ?? [:]
        lunch = parts["morning"]?.isYours == true ? parts["morning"]?.perHour.map(Self.figure) ?? "" : ""
        dinner = parts["night"]?.isYours == true ? parts["night"]?.perHour.map(Self.figure) ?? "" : ""
        editing = family
    }

    static func number(_ text: String) -> Double? {
        let t = text.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, let v = NumberFormatter.cavnarDecimal.number(from: t)?.doubleValue ?? Double(t), v > 0 else {
            return nil
        }
        return v
    }

    static func figure(_ v: Double) -> String {
        v == v.rounded() ? String(Int(v)) : String(format: "%.1f", v)
    }

    /// The role a family's standard is about, in the owner's words.
    static func label(_ family: String) -> String {
        family == "line_cook" ? "Cooks" : family.replacingOccurrences(of: "_", with: " ").capitalized + "s"
    }

    static func unitWho(_ family: String) -> String {
        family == "line_cook" ? "cook" : family.replacingOccurrences(of: "_", with: " ")
    }

    static func fallbackText(_ s: LaborStandard, part: String, family: String) -> String {
        "\(s.perHour.map(figure) ?? "\u{2014}") \(s.unit ?? "") per \(unitWho(family))-hour at \(SetupWords.daypart(part))"
    }
}
