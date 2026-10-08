import SwiftUI

// The rules-sheet settings that save the moment they change, apart from
// Save rules (iOS parity, 10/7/26): the weekdays the restaurant is closed,
// the dining-section count and the kitchen's stations. The web saves each
// of these on its own too (its Advanced tab, the stations card), so a
// change here never waits on — or rides along with — the rest of the form.
// All three go to POST /mobile/api/labor/rules, the account owner's route
// (strategy_routes._do_compliance_set): `can_edit` on the rules gates them.

// MARK: - Payloads

/// GET labor/rules `closures`: the weekdays the restaurant does not trade
/// and its closed dates (the dates are edited on Account, one at a time).
struct RulesClosures: Decodable, Equatable {
    var closedWeekdays: [String] = []
    var closedDates: [String] = []

    enum CodingKeys: String, CodingKey {
        case closedWeekdays = "closed_weekdays"
        case closedDates = "closed_dates"
    }

    init(closedWeekdays: [String] = [], closedDates: [String] = []) {
        self.closedWeekdays = closedWeekdays
        self.closedDates = closedDates
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        closedWeekdays = c.setupTexts(.closedWeekdays)
        closedDates = c.setupTexts(.closedDates)
    }
}

/// GET labor/rules `kitchen_stations` (kitchen_stations.py): the roles that
/// are the kitchen, its stations, what each shift needs, who can work each
/// station, and the roster's roles and kitchen people to choose from.
struct KitchenStations: Decodable, Equatable {
    var roles: [String] = []
    var stations: [String] = []
    var needs: [Need] = []
    var skills: [String: [String]] = [:]
    var active = false
    var rosterRoles: [String] = []
    var kitchenPeople: [String] = []

    struct Need: Decodable, Equatable, Hashable, Identifiable {
        var station: String
        var daypart: String
        var days: [String]
        var count: Int
        var id: String { "\(station)|\(daypart)|\(days.joined(separator: ","))" }

        enum CodingKeys: String, CodingKey { case station, daypart, days, count }

        init(station: String, daypart: String = "all", days: [String] = [], count: Int = 1) {
            self.station = station; self.daypart = daypart; self.days = days; self.count = count
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            station = c.setupText(.station) ?? ""
            daypart = c.setupText(.daypart) ?? "all"
            days = c.setupTexts(.days)
            count = max(1, c.setupInt(.count) ?? 1)
        }

        /// "Nights (after 3pm) · Every day · 2 cooks" — the web's line.
        var line: String {
            let when = KitchenStations.dayparts.first { $0.key == daypart }?.label ?? daypart
            let on = days.isEmpty ? "Every day" : days.joined(separator: ", ")
            return "\(when) \u{00B7} \(on) \u{00B7} \(count) cook\(count == 1 ? "" : "s")"
        }
    }

    enum CodingKeys: String, CodingKey {
        case roles, stations, needs, skills, active
        case rosterRoles = "roster_roles"
        case kitchenPeople = "kitchen_people"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        roles = c.setupTexts(.roles)
        stations = c.setupTexts(.stations)
        needs = c.setupList(Need.self, .needs).filter { !$0.station.isEmpty }
        skills = ((try? c.decodeIfPresent([String: [String]].self, forKey: .skills)) ?? nil) ?? [:]
        active = c.setupBool(.active) ?? false
        rosterRoles = c.setupTexts(.rosterRoles)
        kitchenPeople = c.setupTexts(.kitchenPeople)
    }

    /// The web's three choices for when a station is needed (ST_PART).
    static let dayparts: [(key: String, label: String)] = [
        ("night", "Nights (after 3pm)"), ("morning", "Mornings (before 3pm)"), ("all", "All day"),
    ]

    /// The stations a cook is trained on — matched without regard to case,
    /// as the server keys them.
    func skills(of person: String) -> Set<String> {
        let key = person.lowercased()
        var out: [String] = skills[person] ?? []
        for (name, have) in skills where name.lowercased() == key { out = have }
        return Set(out)
    }

    /// The roles to choose the kitchen from: the roster's, then any chosen
    /// role the roster no longer has.
    var roleChoices: [String] {
        var out = rosterRoles
        for r in roles where !out.contains(where: { $0.caseInsensitiveCompare(r) == .orderedSame }) { out.append(r) }
        return out
    }
}

/// One station change (kitchen_stations.apply_edit): applied on the server
/// to what is stored now, so the phone never sends a whole list back over
/// someone else's change.
enum StationEdit: Encodable, Equatable {
    case roles([String])
    case addStation(String)
    case removeStation(String)
    case addNeed(station: String, daypart: String, days: [String], count: Int)
    case removeNeed(station: String, daypart: String, days: [String])
    case skill(person: String, station: String, on: Bool)

    enum CodingKeys: String, CodingKey { case op, roles, name, station, daypart, days, count, person, on }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .roles(let roles):
            try c.encode("roles", forKey: .op)
            try c.encode(roles, forKey: .roles)
        case .addStation(let name):
            try c.encode("add_station", forKey: .op)
            try c.encode(name, forKey: .name)
        case .removeStation(let name):
            try c.encode("remove_station", forKey: .op)
            try c.encode(name, forKey: .name)
        case .addNeed(let station, let daypart, let days, let count):
            try c.encode("add_need", forKey: .op)
            try c.encode(station, forKey: .station)
            try c.encode(daypart, forKey: .daypart)
            try c.encode(days, forKey: .days)
            try c.encode(count, forKey: .count)
        case .removeNeed(let station, let daypart, let days):
            try c.encode("remove_need", forKey: .op)
            try c.encode(station, forKey: .station)
            try c.encode(daypart, forKey: .daypart)
            try c.encode(days, forKey: .days)
        case .skill(let person, let station, let on):
            try c.encode("skill", forKey: .op)
            try c.encode(person, forKey: .person)
            try c.encode(station, forKey: .station)
            try c.encode(on, forKey: .on)
        }
    }
}

/// POST labor/rules `{closed_weekdays: [...]}` — the route takes the whole
/// list, so it is the server's list with the one day changed.
struct ClosedWeekdaysBody: Encodable {
    let closedWeekdays: [String]
    enum CodingKeys: String, CodingKey { case closedWeekdays = "closed_weekdays" }
}

/// POST labor/rules `{section_count: n}`; null is no cap.
struct SectionCountBody: Encodable {
    let sectionCount: Int?
    enum CodingKeys: String, CodingKey { case sectionCount = "section_count" }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        // Always sent: a null clears the cap; an absent key would change nothing.
        try c.encode(sectionCount, forKey: .sectionCount)
    }
}

/// POST labor/rules `{station_edit: {op, …}}`.
struct StationEditBody: Encodable {
    let stationEdit: StationEdit
    enum CodingKeys: String, CodingKey { case stationEdit = "station_edit" }
}

/// What POST labor/rules answers for these three: only the keys the save
/// changed are present.
struct QuickRulesResponse: Decodable {
    var ok = false
    var error: String?
    var closures: RulesClosures?
    var sectionCount: Int?
    var floorCapConflicts: [FloorCapConflict]?
    var kitchenStations: KitchenStations?

    enum CodingKeys: String, CodingKey {
        case ok, error, closures
        case sectionCount = "section_count"
        case floorCapConflicts = "floor_cap_conflicts"
        case kitchenStations = "kitchen_stations"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.setupBool(.ok) ?? false
        error = c.setupText(.error)
        closures = (try? c.decodeIfPresent(RulesClosures.self, forKey: .closures)) ?? nil
        sectionCount = c.setupInt(.sectionCount)
        floorCapConflicts = c.contains(.floorCapConflicts) ? c.setupList(FloorCapConflict.self, .floorCapConflicts) : nil
        kitchenStations = (try? c.decodeIfPresent(KitchenStations.self, forKey: .kitchenStations)) ?? nil
    }
}

// MARK: - The saves

extension TeamSetupStore {
    static let weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    /// The server's closed weekdays with one day switched, in week order.
    nonisolated static func closedWeekdays(_ base: [String], toggling day: String) -> [String] {
        let on = base.contains(day)
        let next = on ? base.filter { $0 != day } : base + [day]
        return weekdays.filter { next.contains($0) }
    }

    /// The server's closed weekdays with one day set closed (or open), in
    /// week order — what the owner's tap meant, applied to the list as it
    /// stands now rather than the copy on screen.
    nonisolated static func closedWeekdays(_ base: [String], setting day: String, closed: Bool) -> [String] {
        let next = closed ? base + [day] : base.filter { $0 != day }
        return weekdays.filter { next.contains($0) }
    }

    /// GET labor/rules, read for its closures alone.
    private struct ClosuresOnly: Decodable {
        var closures: RulesClosures?
        enum CodingKeys: String, CodingKey { case closures }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            closures = (try? c.decodeIfPresent(RulesClosures.self, forKey: .closures)) ?? nil
        }
    }

    /// Closed (or open again) on one weekday — saved at once, and the
    /// server's list adopted. The list is read again first and the one day
    /// switched on THAT (re-audit 10/8/26 #11): the save writes the whole
    /// list, and toggling the copy loaded when the sheet opened undid a day
    /// another login or the web closed since. No fresh list, no write.
    func toggleClosedWeekday(_ day: String) async {
        guard closedDayBusy == nil else { return }
        // What the tap means, from the day as the owner saw it.
        let wantClosed = !closedWeekdays.contains(day)
        closedDayBusy = day
        closedDaysError = nil
        defer { closedDayBusy = nil }
        do {
            let fresh: ClosuresOnly = try await client.send("/mobile/api/labor/rules", hapticOnError: false)
            guard let current = fresh.closures else {
                closedDaysError = "Couldn\u{2019}t read the closed days just now \u{2014} nothing was changed."
                Haptic.error()
                return
            }
            closedWeekdays = current.closedWeekdays
            if current.closedWeekdays.contains(day) == wantClosed {
                // Somebody already made that change: nothing to write.
                Haptic.success()
                return
            }
            let r: QuickRulesResponse = try await client.send(
                "/mobile/api/labor/rules", method: .post,
                body: ClosedWeekdaysBody(closedWeekdays: Self.closedWeekdays(current.closedWeekdays, setting: day,
                                                                             closed: wantClosed)),
                hapticOnError: false, retryTransient: false)
            guard r.ok, let cl = r.closures else {
                closedDaysError = r.error ?? "Couldn\u{2019}t save the closed days."
                Haptic.error()
                return
            }
            closedWeekdays = cl.closedWeekdays
            Haptic.success()
        } catch let error as APIClient.APIError {
            closedDaysError = error.message
            Haptic.error()
        } catch {
            closedDaysError = "Couldn\u{2019}t save the closed days."
            Haptic.error()
        }
    }

    /// The dining-section count (nil: no cap). One save at a time, the
    /// latest value last — a stepper tapped quickly ends on what it shows.
    func saveSectionCount(_ n: Int?) async {
        if sectionCountSaving {
            sectionCountPending = .some(n)
            return
        }
        sectionCountSaving = true
        defer { sectionCountSaving = false }
        var next: Int?? = .some(n)
        while let want = next {
            sectionCountPending = nil
            sectionCountError = nil
            sectionCountNote = nil
            do {
                let r: QuickRulesResponse = try await client.send(
                    "/mobile/api/labor/rules", method: .post, body: SectionCountBody(sectionCount: want),
                    hapticOnError: false, retryTransient: false)
                if r.ok {
                    sectionCount = r.sectionCount
                    if let conflicts = r.floorCapConflicts { floorCapConflicts = conflicts }
                    sectionCountNote = "Saved \u{00B7} the next draft uses it"
                } else {
                    sectionCountError = r.error ?? "Couldn\u{2019}t save the dining sections."
                }
            } catch let error as APIClient.APIError {
                sectionCountError = error.message
            } catch {
                sectionCountError = "Couldn\u{2019}t save the dining sections."
            }
            next = sectionCountPending
        }
    }

    /// One station change, saved at once; the server's stations adopted.
    @discardableResult
    func sendStationEdit(_ edit: StationEdit) async -> Bool {
        guard !stationBusy else { return false }
        stationBusy = true
        stationError = nil
        stationNote = nil
        defer { stationBusy = false }
        do {
            let r: QuickRulesResponse = try await client.send(
                "/mobile/api/labor/rules", method: .post, body: StationEditBody(stationEdit: edit),
                hapticOnError: false, retryTransient: false)
            guard r.ok else {
                stationError = r.error ?? "Couldn\u{2019}t save."
                Haptic.error()
                return false
            }
            if let ks = r.kitchenStations { kitchenStations = ks }
            stationNote = "Saved"
            Haptic.success()
            return true
        } catch let error as APIClient.APIError {
            stationError = error.message
        } catch {
            stationError = "Couldn\u{2019}t save."
        }
        Haptic.error()
        return false
    }
}

// MARK: - Closed weekdays

/// The weekdays the restaurant does not open — a chip a day, each tap
/// saved on its own (the web's "Closed" chips on the rules card).
struct RulesClosedDaysSection: View {
    let store: TeamSetupStore
    let canEdit: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: "Closed")
            Text("Days you do not open; the draft leaves them empty."
                 + (canEdit ? " Each day saves as you tap it." : ""))
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            AccountFlowLayout(spacing: 6) {
                ForEach(TeamSetupStore.weekdays, id: \.self) { day in
                    let closed = store.closedWeekdays.contains(day)
                    Button {
                        Haptic.selection()
                        Task { await store.toggleClosedWeekday(day) }
                    } label: {
                        AccountChip(text: String(day.prefix(3)), muted: !closed)
                            .opacity(store.closedDayBusy == day ? 0.5 : 1)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(!canEdit || store.closedDayBusy != nil)
                    .accessibilityLabel(day)
                    .accessibilityValue(closed ? "Closed" : "Open")
                    .accessibilityAddTraits(closed ? .isSelected : [])
                }
            }
            if store.closedDayBusy != nil {
                CavnarShimmerText(text: "Saving\u{2026}", color: .cavnarInk3)
            }
            if let error = store.closedDaysError {
                Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("A closed date \u{2014} a holiday, a private event \u{2014} is set in Account \u{2192} Profile \u{2192} Hours & closures.")
                .font(.cavnarBody(12.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - Dining sections

/// The most front-of-house people on the floor at once (the section cap),
/// saved on each step, and the way into the sections' names.
struct RulesDiningSectionsSection: View {
    let store: TeamSetupStore
    let canEdit: Bool
    let onOpenNames: () -> Void

    @State private var count = 0
    @State private var synced = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountSection(kicker: "Dining sections") {
                HStack(alignment: .center, spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Dining sections").font(.cavnarBody(16)).foregroundStyle(Color.cavnarInk3)
                        Text("The most front-of-house people on the floor at once. Step down to No cap for none.")
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk3.opacity(0.8))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 8)
                    Group {
                        if count == 0 {
                            Text("No cap").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarInk2)
                        } else {
                            Text("\(count)").font(.cavnarNumber(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                        }
                    }
                    .fixedSize()
                    Stepper("Dining sections", value: $count, in: 0...30)
                        .labelsHidden()
                        .fixedSize()
                        .tint(Color.cavnarEmber)
                        .disabled(!canEdit)
                        .accessibilityValue(count == 0 ? "No cap" : "\(count) sections")
                }
                .frame(minHeight: AccountKVRow<EmptyView>.rowHeight)
                .padding(.vertical, 9)
                AccountRowDivider()
                AccountNavRow(label: "Section names", value: nil, showsDivider: false, action: onOpenNames)
            }
            if store.sectionCountSaving {
                CavnarShimmerText(text: "Saving\u{2026}", color: .cavnarInk3)
            } else if let error = store.sectionCountError {
                Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let note = store.sectionCountNote {
                Text(note).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarGreen)
            }
        }
        .onAppear {
            count = store.sectionCount ?? 0
            synced = true
        }
        .onChange(of: store.sectionCount) { _, v in
            // The server's value once nothing is waiting to be saved.
            if !store.sectionCountSaving { count = v ?? 0 }
        }
        .onChange(of: count) { _, v in
            guard synced, canEdit, v != (store.sectionCount ?? 0) || store.sectionCountSaving else { return }
            Haptic.selection()
            Task { await store.saveSectionCount(v == 0 ? nil : v) }
        }
        .onChange(of: store.sectionCountError) { _, e in
            // A refused value goes back to what is stored.
            if e != nil, !store.sectionCountSaving { count = store.sectionCount ?? 0 }
        }
    }
}

// MARK: - Kitchen stations

/// The row on the rules sheet that opens the stations editor.
struct RulesKitchenSection: View {
    let store: TeamSetupStore
    let onOpen: () -> Void

    private var summary: String {
        let ks = store.kitchenStations
        if ks.roles.isEmpty { return "Not set up" }
        let n = ks.stations.count
        return n == 0 ? "No stations" : "\(n) station\(n == 1 ? "" : "s")"
    }

    var body: some View {
        AccountSection(kicker: "Kitchen") {
            AccountNavRow(label: "Kitchen stations", value: summary, showsDivider: false, action: onOpen)
        }
    }
}

/// Which stations a shift needs and which cooks can work each — the web's
/// stations card, phone-shaped: every change is one station edit, saved at
/// once and answered with the server's stations.
struct KitchenStationsSheet: View {
    let store: TeamSetupStore
    let canEdit: Bool

    @State private var newStation = ""
    @State private var removing: String?
    @State private var needStation = ""
    @State private var needPart = "night"
    @State private var needCount = 1
    @State private var needDays: Set<String> = []
    @FocusState private var stationFocused: Bool

    private var ks: KitchenStations { store.kitchenStations }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("Which stations each shift needs and which cooks can work each. The draft staffs them; every change here saves as you make it.")
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    if !canEdit {
                        Text("Only the account owner can change the kitchen stations.")
                            .font(.cavnarBody(13.5, weight: 600))
                            .foregroundStyle(Color.cavnarAmber)
                    }
                    rolesStep
                    if !ks.roles.isEmpty {
                        stationsStep
                        if !ks.stations.isEmpty {
                            needsStep
                            skillsStep
                        }
                    }
                    status
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.immediately)
            .accountSheetChrome("Kitchen stations")
        }
        .confirmationDialog(removing.map { "Remove the \($0) station, its requirements and who is trained on it?" } ?? "",
                            isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }),
                            titleVisibility: .visible) {
            if let name = removing {
                Button("Remove \(name)", role: .destructive) {
                    Task { await store.sendStationEdit(.removeStation(name)) }
                    removing = nil
                }
            }
            Button("Keep it", role: .cancel) { removing = nil }
        }
        .onAppear { if needStation.isEmpty { needStation = ks.stations.first ?? "" } }
        .onChange(of: ks.stations) { _, list in
            if !list.contains(needStation) { needStation = list.first ?? "" }
        }
    }

    // MARK: Steps

    private var rolesStep: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: "Which roles are the kitchen")
            let choices = ks.roleChoices
            if choices.isEmpty {
                Text("Roles appear here once your roster has them.")
                    .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3).italic()
            } else {
                AccountFlowLayout(spacing: 6) {
                    ForEach(choices, id: \.self) { role in
                        let on = ks.roles.contains { $0.caseInsensitiveCompare(role) == .orderedSame }
                        Button {
                            Haptic.selection()
                            var next = ks.roles.filter { $0.caseInsensitiveCompare(role) != .orderedSame }
                            if !on { next.append(role) }
                            if next.isEmpty {
                                store.stationError = "Keep at least one kitchen role."
                                return
                            }
                            Task { await store.sendStationEdit(.roles(next)) }
                        } label: {
                            AccountChip(text: role, muted: !on).frame(minHeight: 44).contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(!canEdit || store.stationBusy)
                        .accessibilityAddTraits(on ? .isSelected : [])
                    }
                }
            }
            if ks.roles.isEmpty {
                Text("Pick the role (or roles) your cooks are under on the schedule, then add the stations.")
                    .font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var stationsStep: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: "Stations")
            if ks.stations.isEmpty {
                Text("None yet. Add each station a cook can be on: Saut\u{00E9}, Grill, Fry, Pantry, Prep\u{2026}")
                    .font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(ks.stations.enumerated()), id: \.element) { i, name in
                        let last = i == ks.stations.count - 1
                        if canEdit {
                            AccountActionRow(label: name, symbol: "xmark", tone: .cavnarRed, showsDivider: !last) {
                                removing = name
                            }
                        } else {
                            AccountKVRow(label: name, showsDivider: !last) { EmptyView() }
                        }
                    }
                }
                .accountCard()
            }
            if canEdit {
                HStack(spacing: 10) {
                    TextField("Add a station, e.g. Grill", text: $newStation)
                        .cavnarTextFieldStyle()
                        .textInputAutocapitalization(.words)
                        .autocorrectionDisabled()
                        .focused($stationFocused)
                        .submitLabel(.done)
                        .onSubmit { addStation() }
                        .onChange(of: newStation) { _, v in if v.count > 40 { newStation = String(v.prefix(40)) } }
                    Button("Add") { addStation() }
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: trimmedStation.isEmpty || store.stationBusy))
                        .disabled(trimmedStation.isEmpty || store.stationBusy)
                        .fixedSize()
                }
            }
        }
    }

    private var trimmedStation: String { newStation.trimmingCharacters(in: .whitespacesAndNewlines) }

    private func addStation() {
        let name = trimmedStation
        guard !name.isEmpty else { return }
        Task {
            if await store.sendStationEdit(.addStation(name)) {
                newStation = ""
                if needStation.isEmpty { needStation = name }
            }
        }
    }

    private var needsStep: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: "What each shift needs")
            if ks.needs.isEmpty {
                Text("Nothing required yet. Say which stations must be staffed, and when.")
                    .font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(ks.needs.enumerated()), id: \.element.id) { i, need in
                        HStack(alignment: .center, spacing: 12) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(need.station).font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarInk)
                                HomeMixedText.make(need.line, size: 13, color: .cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer(minLength: 8)
                            if canEdit {
                                AccountActionChip(symbol: "xmark", tone: .cavnarRed,
                                                  accessibilityLabel: "Remove \(need.station), \(need.line)") {
                                    Task {
                                        await store.sendStationEdit(.removeNeed(station: need.station, daypart: need.daypart,
                                                                                days: need.days))
                                    }
                                }
                                .disabled(store.stationBusy)
                            }
                        }
                        .padding(.vertical, 9)
                        if i < ks.needs.count - 1 { AccountRowDivider() }
                    }
                }
                .accountCard()
            }
            if canEdit { addNeedForm }
        }
    }

    private var addNeedForm: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 0) {
                AccountKVRow(label: "Station") {
                    menu(current: needStation.isEmpty ? "Pick one" : needStation,
                         options: ks.stations.map { ($0, $0) }) { needStation = $0 }
                }
                AccountKVRow(label: "When") {
                    menu(current: KitchenStations.dayparts.first { $0.key == needPart }?.label ?? needPart,
                         options: KitchenStations.dayparts.map { ($0.key, $0.label) }) { needPart = $0 }
                }
                AccountKVRow(label: "Cooks", showsDivider: false) {
                    HStack(spacing: 10) {
                        Text("\(needCount)").font(.cavnarNumber(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                        Stepper("Cooks", value: $needCount, in: 1...4)
                            .labelsHidden()
                            .fixedSize()
                            .tint(Color.cavnarEmber)
                            .accessibilityValue("\(needCount) cook\(needCount == 1 ? "" : "s")")
                    }
                }
            }
            .accountCard()
            Text("On (none picked means every day)")
                .font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
            AccountFlowLayout(spacing: 6) {
                ForEach(TeamSetupStore.weekdays, id: \.self) { day in
                    let on = needDays.contains(day)
                    Button {
                        Haptic.selection()
                        if on { needDays.remove(day) } else { needDays.insert(day) }
                    } label: {
                        AccountChip(text: String(day.prefix(3)), muted: !on).frame(minHeight: 44).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(day)
                    .accessibilityAddTraits(on ? .isSelected : [])
                }
            }
            Button {
                Haptic.light()
                let days = TeamSetupStore.weekdays.filter { needDays.contains($0) }
                Task {
                    if await store.sendStationEdit(.addNeed(station: needStation, daypart: needPart, days: days,
                                                            count: needCount)) {
                        needDays = []
                        needCount = 1
                    }
                }
            } label: { Text("Add requirement").frame(maxWidth: .infinity) }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: needStation.isEmpty || store.stationBusy))
                .disabled(needStation.isEmpty || store.stationBusy)
        }
    }

    private var skillsStep: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: "Who can work each station")
            if ks.kitchenPeople.isEmpty {
                Text("Nobody on your roster is under \(ks.roles.joined(separator: " or ")) yet.")
                    .font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(ks.kitchenPeople.enumerated()), id: \.element) { i, person in
                        let have = ks.skills(of: person)
                        VStack(alignment: .leading, spacing: 6) {
                            Text(person).font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarInk)
                            AccountFlowLayout(spacing: 6) {
                                ForEach(ks.stations, id: \.self) { station in
                                    let on = have.contains(station)
                                    Button {
                                        Haptic.selection()
                                        Task { await store.sendStationEdit(.skill(person: person, station: station, on: !on)) }
                                    } label: {
                                        AccountChip(text: station, muted: !on).frame(minHeight: 44).contentShape(Rectangle())
                                    }
                                    .buttonStyle(.plain)
                                    .disabled(!canEdit || store.stationBusy)
                                    .accessibilityLabel("\(person) on \(station)")
                                    .accessibilityAddTraits(on ? .isSelected : [])
                                }
                            }
                        }
                        .padding(.vertical, 9)
                        if i < ks.kitchenPeople.count - 1 { AccountRowDivider() }
                    }
                }
                .accountCard()
            }
        }
    }

    @ViewBuilder
    private var status: some View {
        if store.stationBusy {
            CavnarShimmerText(text: "Saving\u{2026}", color: .cavnarInk3)
        } else if let error = store.stationError {
            Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                .fixedSize(horizontal: false, vertical: true)
        } else if let note = store.stationNote {
            Text(note).font(.cavnarBody(14)).foregroundStyle(Color.cavnarGreen)
        }
    }

    private func menu(current: String, options: [(String, String)], onPick: @escaping (String) -> Void) -> some View {
        Menu {
            ForEach(options, id: \.0) { code, label in
                Button {
                    Haptic.selection()
                    onPick(code)
                } label: { Text(label) }
            }
        } label: {
            HStack(spacing: 6) {
                Text(current)
                    .font(.cavnarBody(15, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
    }
}
