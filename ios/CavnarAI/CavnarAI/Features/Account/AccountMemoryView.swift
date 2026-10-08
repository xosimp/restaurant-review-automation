import SwiftUI
import Observation

/// Account → Memory: what Cavnar AI remembers about the restaurant, who
/// said each thing, who may read it and until when — and what left without
/// anyone asking, with the way back (memory round 9/29/26, M2 owner_lanes;
/// GET /account/memory → owner_memory.account_view). Built from the
/// identity-card kit: the hero, the lanes strip (how full each kind of
/// memory is, as meters), then the facts by kind, the archive, and the add
/// form. Every add, forget and restore saves on its own — nothing here is a
/// whole-list save (owner edits never vanish).
struct AccountMemoryView: View {
    @State private var viewModel = AccountMemoryViewModel()
    @Environment(SessionStore.self) private var sessionStore: SessionStore?
    @FocusState private var focus: Field?
    @State private var pendingDismiss: ArchivedFact?

    enum Field: Hashable { case fact }

    private var isPrincipal: Bool { sessionStore?.currentUser?.isOwner ?? false }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Memory") {
                        GlowBadge(systemImage: "brain.head.profile", size: 56)
                    } subtitle: {
                        Text(viewModel.subtitle)
                    }

                    if viewModel.isLoading && viewModel.memory == nil {
                        CavnarSkeletonBar(height: 3)
                            .padding(.vertical, 10)
                            .accessibilityLabel("Loading what Cavnar AI remembers")
                    } else if let memory = viewModel.memory {
                        if !memory.lanes.isEmpty {
                            lanes(memory.lanes)
                        }
                        facts(memory)
                        if !memory.archived.isEmpty {
                            archived(memory.archived)
                        }
                        addForm
                    }
                    if let error = viewModel.errorMessage {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Memory")
            .cavnarPostedOverlay(viewModel.posted) { viewModel.posted = nil }
            .task { await viewModel.load() }
            // A follow-up has a due date, the rest an end date: each opens
            // on its own sensible default.
            .onChange(of: viewModel.draft.kind) { _, kind in
                viewModel.draft.days = kind == "followup" ? 7 : 0
            }
            .confirmationDialog(
                "Let this note go for good?",
                isPresented: Binding(get: { pendingDismiss != nil }, set: { if !$0 { pendingDismiss = nil } }),
                titleVisibility: .visible
            ) {
                Button("Dismiss for good", role: .destructive) {
                    guard let item = pendingDismiss else { return }
                    Task { await viewModel.dismiss(item) }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("It can\u{2019}t be put back after this.")
            }
        }
    }

    // MARK: - Lanes

    /// One meter per kind: how many are kept against how many the kind
    /// holds before the oldest leaves for the archive.
    private func lanes(_ lanes: [MemoryLane]) -> some View {
        AccountSection(kicker: "How full each kind is") {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(lanes) { lane in
                    HStack(spacing: 10) {
                        Text(MemoryKind.plural(lane.kind))
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk3)
                            .frame(width: 104, alignment: .leading)
                        ConfidenceMeter(fraction: lane.fraction, tone: lane.isFull ? .warn : .good,
                                        width: nil, height: 6)
                            .frame(maxWidth: .infinity)
                        Text("\(lane.count) of \(lane.cap)")
                            .font(.cavnarNumber(14, weight: 600))
                            .foregroundStyle(lane.isFull ? Color.cavnarAmber : Color.cavnarInk2)
                            .frame(width: 64, alignment: .trailing)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(MemoryKind.plural(lane.kind)): \(lane.count) of \(lane.cap)")
                }
                Text("When a kind is full, its oldest fact moves to the archive below — never deleted.")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.vertical, 9)
        }
    }

    // MARK: - Facts

    @ViewBuilder
    private func facts(_ memory: AccountMemory) -> some View {
        if memory.facts.isEmpty {
            AccountSection(kicker: "What Cavnar AI remembers") {
                Text("Nothing remembered yet. Add something below, or tell Ask Cavnar AI \u{201C}remember that\u{2026}\u{201D}.")
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, 9)
            }
        }
        ForEach(MemoryKind.order, id: \.self) { kind in
            let rows = memory.facts.filter { $0.kind == kind }
            if !rows.isEmpty {
                AccountSection(kicker: MemoryKind.plural(kind)) {
                    ForEach(Array(rows.enumerated()), id: \.element.id) { i, fact in
                        factRow(fact, schedule: memory.scheduleLine(for: fact), showsDivider: i < rows.count - 1)
                    }
                }
            }
        }
        // A kind the server adds later still shows, under its own name.
        let other = memory.facts.filter { !MemoryKind.order.contains($0.kind) }
        if !other.isEmpty {
            AccountSection(kicker: "Other") {
                ForEach(Array(other.enumerated()), id: \.element.id) { i, fact in
                    factRow(fact, schedule: memory.scheduleLine(for: fact), showsDivider: i < other.count - 1)
                }
            }
        }
    }

    private func factRow(_ fact: MemoryFact, schedule: (text: String, checked: Bool)? = nil,
                         showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    HomeMixedText.make(fact.fact, size: 16, weight: 500, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    HomeMixedText.make(fact.detailLine(viewerIsPrincipal: isPrincipal), size: 13.5, weight: 500,
                                       color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    // A staffing rule says how the schedule holds it — or
                    // that it can't (schedule audit 10/3/26 D-14, D-38).
                    if let schedule {
                        HomeMixedText.make(schedule.text, size: 13.5, weight: 600,
                                           color: schedule.checked ? .cavnarGreen : .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 8)
                if fact.canPin && viewModel.busyId != fact.id {
                    // Pinned out of the lane's eviction (the web's Pin).
                    AccountActionChip(symbol: fact.pinned ? "pin.slash" : "pin",
                                      tone: fact.pinned ? .cavnarEmber2 : .cavnarInk3,
                                      accessibilityLabel: (fact.pinned ? "Unpin: " : "Pin: ") + fact.fact) {
                        Task { await viewModel.pin(fact, pinned: !fact.pinned) }
                    }
                }
                if fact.canForget {
                    if viewModel.busyId == fact.id {
                        CavnarShimmerLine(color: .cavnarRed).frame(width: 28)
                    } else {
                        // Personnel and money default to owners only; one
                        // tap shares it with the team (memory re-audit
                        // PEOPLE-10, /account/memory/audience).
                        if fact.audience == "principals" {
                            AccountActionChip(symbol: "person.2",
                                              accessibilityLabel: "Share with the team: \(fact.fact)") {
                                Task { await viewModel.share(fact) }
                            }
                        }
                        AccountActionChip(symbol: "xmark", tone: .cavnarRed,
                                          accessibilityLabel: "Forget: \(fact.fact)") {
                            Task { await viewModel.forget(fact) }
                        }
                    }
                }
            }
            .padding(.vertical, 10)
            .contentShape(Rectangle())
            // Long-press for every action this login has on the fact: pin,
            // which locations keep it, share, forget (parity #65).
            .contextMenu {
                if fact.canPin {
                    Button { Task { await viewModel.pin(fact, pinned: !fact.pinned) } } label: {
                        Label(fact.pinned ? "Unpin" : "Pin \u{2014} a full lane won\u{2019}t push it out",
                              systemImage: fact.pinned ? "pin.slash" : "pin")
                    }
                }
                if fact.canSetScope {
                    Button { Task { await viewModel.setScope(fact, scope: fact.scope == "org" ? "location" : "org") } } label: {
                        Label(fact.scope == "org" ? "This location only" : "Every location",
                              systemImage: fact.scope == "org" ? "mappin" : "building.2")
                    }
                }
                if fact.canForget && fact.audience == "principals" && !fact.fromLocation {
                    Button { Task { await viewModel.share(fact) } } label: {
                        Label("Share with the team", systemImage: "person.2")
                    }
                }
                if fact.canForget {
                    Button(role: .destructive) { Task { await viewModel.forget(fact) } } label: {
                        Label("Forget", systemImage: "xmark")
                    }
                }
            }
            if showsDivider { AccountRowDivider() }
        }
    }

    // MARK: - Archive

    private func archived(_ items: [ArchivedFact]) -> some View {
        AccountSection(kicker: "Left without anyone asking") {
            ForEach(Array(items.enumerated()), id: \.element.id) { i, item in
                VStack(spacing: 0) {
                    HStack(alignment: .top, spacing: 12) {
                        VStack(alignment: .leading, spacing: 4) {
                            HomeMixedText.make(item.fact, size: 15.5, weight: 500, color: .cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                            HomeMixedText.make(item.detailLine, size: 13.5, weight: 500, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 8)
                        if item.canRestore {
                            if viewModel.busyId == item.id {
                                CavnarShimmerLine(color: .cavnarEmber).frame(width: 28)
                            } else {
                                Button {
                                    Haptic.light()
                                    Task { await viewModel.restore(item) }
                                } label: {
                                    Text("Restore")
                                        .font(.cavnarBody(14, weight: 700))
                                        .foregroundStyle(Color.cavnarEmber2)
                                        .frame(minHeight: 36)
                                        .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                .accessibilityLabel("Restore: \(item.fact)")
                                // Let it go for good — asked first (the web's Dismiss).
                                AccountActionChip(symbol: "trash", tone: .cavnarRed,
                                                  accessibilityLabel: "Dismiss for good: \(item.fact)") {
                                    pendingDismiss = item
                                }
                            }
                        }
                    }
                    .padding(.vertical, 10)
                    if i < items.count - 1 { AccountRowDivider() }
                }
            }
        }
    }

    // MARK: - Add

    private var addForm: some View {
        AccountSection(kicker: "Tell Cavnar AI something") {
            VStack(alignment: .leading, spacing: 14) {
                AccountEditor(label: "What to remember", placeholder: "We close early the first Sunday of the month",
                              text: $viewModel.draft.fact, focus: $focus, field: .fact, showsDivider: false)
                choice("Kind") {
                    CavnarSegmentedControl(selection: $viewModel.draft.kind, options: MemoryKind.addable) {
                        MemoryKind.singular($0)
                    }
                }
                choice("About") {
                    AccountFlowLayout(spacing: 6) {
                        ForEach(MemoryModule.all, id: \.key) { m in
                            let on = viewModel.draft.modules.contains(m.key)
                            Button {
                                Haptic.selection()
                                if on { viewModel.draft.modules.remove(m.key) } else { viewModel.draft.modules.insert(m.key) }
                            } label: {
                                AccountChip(text: m.label, muted: !on)
                            }
                            .buttonStyle(.plain)
                            .accessibilityAddTraits(on ? .isSelected : [])
                        }
                    }
                    Text(viewModel.draft.modules.isEmpty ? "Nothing picked: the whole business."
                                                         : "Only what Cavnar AI writes about these reads it.")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk3)
                }
                choice(viewModel.draft.kind == "followup" ? "Due" : "Holds until") {
                    let options = viewModel.draft.kind == "followup" ? MemoryDate.dueOptions : MemoryDate.untilOptions
                    CavnarSegmentedControl(selection: $viewModel.draft.days, options: options) {
                        MemoryDate.label($0, due: viewModel.draft.kind == "followup")
                    }
                    if let d = viewModel.draft.dateLabel {
                        HomeMixedText.make((viewModel.draft.kind == "followup" ? "Due " : "Until ") + d,
                                           size: 13, weight: 500, color: .cavnarInk3)
                    }
                }
                choice("Who can read it") {
                    CavnarSegmentedControl(selection: $viewModel.draft.audience,
                                           options: isPrincipal ? MemoryAudience.principalOptions
                                                                : MemoryAudience.teamOptions) {
                        MemoryAudience.label($0)
                    }
                }
                Button {
                    focus = nil
                    Task { await viewModel.add() }
                } label: {
                    Group {
                        if viewModel.adding {
                            CavnarShimmerText(text: "Saving\u{2026}", color: .white)
                        } else {
                            Text("Remember this")
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
                .disabled(viewModel.adding || viewModel.draft.fact.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                // The staffing rule just saved, said back as the schedule
                // will check it — a rule read wrongly is caught now, not a
                // week later (schedule audit 10/3/26 D-14).
                if let rule = viewModel.lastScheduleRule {
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: rule.checked ? "checkmark.circle.fill" : "exclamationmark.circle")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(rule.checked ? Color.cavnarGreen : Color.cavnarAmber)
                            .padding(.top, 2)
                        HomeMixedText.make(rule.text, size: 14, weight: 600,
                                           color: rule.checked ? .cavnarInk2 : .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(.vertical, 9)
        }
    }

    private func choice<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased())
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .tracking(0.8)
                .foregroundStyle(Color.cavnarInk3)
            content()
        }
    }
}

// MARK: - Vocabulary

/// owner_memory.KINDS in the owner's words.
enum MemoryKind {
    static let order = ["constraint", "followup", "context", "preference", "goal"]
    /// The kinds the add form offers (a goal with a number is set in Goals).
    static let addable = ["constraint", "context", "preference", "followup"]

    static func singular(_ kind: String) -> String {
        switch kind {
        case "constraint": return "Rule"
        case "context": return "Context"
        case "preference": return "Preference"
        case "goal": return "Aim"
        case "followup": return "Follow-up"
        default: return kind.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    static func plural(_ kind: String) -> String {
        switch kind {
        case "constraint": return "Rules"
        case "context": return "Context"
        case "preference": return "Preferences"
        case "goal": return "Aims"
        case "followup": return "Follow-ups"
        default: return kind.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
}

/// The modules a fact can be about (owner_memory.SURFACE_MODULES' keys).
enum MemoryModule {
    static let all: [(key: String, label: String)] = [
        ("labor", "Labor"), ("food", "Food Cost"), ("reviews", "Reviews"),
        ("marketing", "Marketing"), ("intel", "Intel"), ("ops", "Operations"),
    ]

    static func label(_ key: String) -> String {
        all.first { $0.key == key }?.label ?? key.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

/// Who may read a fact (memory_context.visible).
enum MemoryAudience {
    static let principalOptions = ["team", "principals", "author"]
    static let teamOptions = ["team", "author"]

    static func label(_ audience: String) -> String {
        switch audience {
        case "principals": return "Only owners"
        case "author": return "Just you"
        default: return "Everyone"
        }
    }
}

/// The dates the add form offers as one tap, in days from today (0 = none)
/// — never a system date field, whose label is not M/D/YY.
enum MemoryDate {
    static let untilOptions = [0, 7, 30, 90]
    static let dueOptions = [1, 3, 7, 14]

    static func label(_ days: Int, due: Bool) -> String {
        switch days {
        case 0: return "No end"
        case 1: return "Tomorrow"
        case 7: return "A week"
        case 14: return "2 weeks"
        case 30: return "A month"
        case 90: return "3 months"
        default: return "\(days) days"
        }
    }

    /// yyyy-MM-dd for the server, days from `today` on the phone's calendar.
    static func iso(daysFrom today: Date, _ days: Int) -> String? {
        guard days > 0, let d = Calendar.current.date(byAdding: .day, value: days, to: today) else { return nil }
        let c = Calendar.current.dateComponents([.year, .month, .day], from: d)
        guard let y = c.year, let m = c.month, let dd = c.day else { return nil }
        return String(format: "%04d-%02d-%02d", y, m, dd)
    }
}

// MARK: - Payload

/// GET /account/memory — owner_memory.account_view. Every field lenient.
struct AccountMemory: Decodable {
    var facts: [MemoryFact] = []
    var lanes: [MemoryLane] = []
    var archived: [ArchivedFact] = []
    /// How the schedule reads each account holder's staffing rule shown,
    /// by fact id (schedule audit 10/3/26 D-14, D-38): checked on every
    /// draft and as what, or that it can't be. Empty on an older server.
    var scheduleChecks: [Int: ScheduleCheck] = [:]

    struct ScheduleCheck: Decodable {
        let checked: Bool
        let text: String
        enum CodingKeys: String, CodingKey { case checked, text }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            checked = c.setupBool(.checked) ?? false
            text = c.setupText(.text) ?? ""
        }
    }

    enum CodingKeys: String, CodingKey {
        case facts, lanes, archived
        case scheduleChecks = "schedule_checks"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        facts = ((try? c.decodeIfPresent(HomeLenientList<MemoryFact>.self, forKey: .facts)) ?? nil)?.items ?? []
        lanes = ((try? c.decodeIfPresent(HomeLenientList<MemoryLane>.self, forKey: .lanes)) ?? nil)?.items ?? []
        archived = ((try? c.decodeIfPresent(HomeLenientList<ArchivedFact>.self, forKey: .archived)) ?? nil)?.items ?? []
        var checks: [Int: ScheduleCheck] = [:]
        for (k, v) in ((try? c.decodeIfPresent([String: ScheduleCheck].self, forKey: .scheduleChecks)) ?? nil) ?? [:] {
            if let id = Int(k), !v.text.isEmpty { checks[id] = v }
        }
        scheduleChecks = checks
    }

    /// One line beside a staffing rule: how every draft checks it, or that
    /// the schedule can't — nil for a fact the schedule doesn't read.
    func scheduleLine(for fact: MemoryFact) -> (text: String, checked: Bool)? {
        scheduleChecks[fact.id].map { ($0.text, $0.checked) }
    }
}

private func memText<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> String? {
    let s = ((try? c.decodeIfPresent(String.self, forKey: key)) ?? nil)?.trimmingCharacters(in: .whitespacesAndNewlines)
    return (s?.isEmpty ?? true) ? nil : s
}

/// One remembered fact, with who said it, its type, who may read it, its
/// dates and whether this login may forget it.
struct MemoryFact: Codable, Hashable, Identifiable {
    let id: Int
    let fact: String
    var kind: String = "context"
    var author: String? = nil
    var audience: String = "team"
    var modules: [String] = []
    var createdOn: String? = nil
    var validUntilLabel: String? = nil
    var dueLabel: String? = nil
    var canForget: Bool = false
    /// Pinned out of lane eviction — a full lane won't push it out (the
    /// account holder's; at most a few of one kind).
    var pinned: Bool = false
    var canPin: Bool = false
    /// "org" (every location of the group) or "location"; a group owner
    /// may change it, on a fact kept here (memory re-audit PEOPLE-13).
    var scope: String = "location"
    var canSetScope: Bool = false
    /// Another location's organisation-wide fact, shown here.
    var fromLocation: Bool = false

    enum CodingKeys: String, CodingKey {
        case id, fact, kind, author, audience, modules, pinned, scope
        case createdOn = "created_on"
        case validUntilLabel = "valid_until_label"
        case dueLabel = "due_label"
        case canForget = "can_forget"
        case canPin = "can_pin"
        case canSetScope = "can_set_scope"
        case fromLocation = "from_location"
    }

    init(id: Int, fact: String, kind: String = "context", author: String? = nil, audience: String = "team",
         modules: [String] = [], createdOn: String? = nil, validUntilLabel: String? = nil,
         dueLabel: String? = nil, canForget: Bool = false) {
        self.id = id; self.fact = fact; self.kind = kind; self.author = author; self.audience = audience
        self.modules = modules; self.createdOn = createdOn; self.validUntilLabel = validUntilLabel
        self.dueLabel = dueLabel; self.canForget = canForget
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        fact = try c.decode(String.self, forKey: .fact)
        kind = memText(c, .kind) ?? "context"
        author = memText(c, .author)
        audience = memText(c, .audience) ?? "team"
        modules = ((try? c.decodeIfPresent(HomeLenientList<String>.self, forKey: .modules)) ?? nil)?.items ?? []
        createdOn = memText(c, .createdOn)
        validUntilLabel = memText(c, .validUntilLabel)
        dueLabel = memText(c, .dueLabel)
        canForget = ((try? c.decodeIfPresent(Bool.self, forKey: .canForget)) ?? nil) ?? false
        pinned = c.setupBool(.pinned) ?? false
        canPin = c.setupBool(.canPin) ?? false
        scope = memText(c, .scope) == "org" ? "org" : "location"
        canSetScope = c.setupBool(.canSetScope) ?? false
        fromLocation = c.setupInt(.fromLocation) != nil
    }

    /// "Erik, owner · Only owners · Labor, Food Cost · until 12/31/26 ·
    /// added 9/21/26". The audience reads "Only owners" for principals'
    /// facts and "Just you" for the author's own; "Everyone" is not said.
    func detailLine(viewerIsPrincipal: Bool) -> String {
        var parts: [String] = []
        if let author { parts.append(author) }
        if audience != "team" { parts.append(MemoryAudience.label(audience)) }
        if !modules.isEmpty { parts.append(modules.map(MemoryModule.label).joined(separator: ", ")) }
        if let due = dueLabel { parts.append("due " + due) }
        if let until = validUntilLabel { parts.append("until " + until) }
        if let on = createdOn { parts.append("added " + on) }
        if scope == "org" { parts.append(fromLocation ? "All locations \u{2014} kept at another" : "All locations") }
        if pinned { parts.append("Pinned") }
        return parts.isEmpty ? MemoryKind.singular(kind) : parts.joined(separator: " \u{00B7} ")
    }
}

/// One kind's lane: how many are kept against its cap.
struct MemoryLane: Codable, Hashable, Identifiable {
    let kind: String
    let count: Int
    let cap: Int
    var id: String { kind }

    var fraction: Double { cap > 0 ? min(1, Double(count) / Double(cap)) : 0 }
    var isFull: Bool { cap > 0 && count >= cap }
}

/// A fact that left without anyone asking — a full lane, a date passed, a
/// retracted answer — and whether this login may put it back.
struct ArchivedFact: Codable, Hashable, Identifiable {
    let id: Int
    let fact: String
    var kind: String = "context"
    var author: String? = nil
    var reasonLabel: String? = nil
    var archivedOn: String? = nil
    var canRestore: Bool = false

    enum CodingKeys: String, CodingKey {
        case id, fact, kind, author
        case reasonLabel = "reason_label"
        case archivedOn = "archived_on"
        case canRestore = "can_restore"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        fact = try c.decode(String.self, forKey: .fact)
        kind = memText(c, .kind) ?? "context"
        author = memText(c, .author)
        reasonLabel = memText(c, .reasonLabel)
        archivedOn = memText(c, .archivedOn)
        canRestore = ((try? c.decodeIfPresent(Bool.self, forKey: .canRestore)) ?? nil) ?? false
    }

    /// "Left 9/2/26 — its lane was full · Erik, owner".
    var detailLine: String {
        var head = "Left"
        if let on = archivedOn { head += " " + on }
        if let why = reasonLabel { head += " \u{2014} " + why }
        return [head, author].compactMap { $0 }.joined(separator: " \u{00B7} ")
    }
}

// MARK: - View model

@Observable
@MainActor
final class AccountMemoryViewModel {
    struct Draft: Equatable {
        var fact = ""
        var kind = "context"
        var modules: Set<String> = []
        /// Days from today for `valid_until` (or `due_on` on a follow-up);
        /// 0 is none.
        var days = 0
        var audience = "team"

        var dateLabel: String? {
            MemoryDate.iso(daysFrom: Date(), days).map { CavnarDate.mdy($0) }
        }
    }

    private struct MemoryResponse: Decodable {
        let ok: Bool
        let memory: AccountMemory
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            ok = ((try? c.decodeIfPresent(Bool.self, forKey: AnyKey("ok"))) ?? nil) ?? false
            memory = (try? AccountMemory(from: decoder)) ?? AccountMemory()
        }
    }
    private struct AnyKey: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init(_ s: String) { stringValue = s }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }
    struct AddBody: Encodable, Equatable {
        let fact: String
        let kind: String
        let modules: [String]
        var validUntil: String? = nil
        var dueOn: String? = nil
        let audience: String
        enum CodingKeys: String, CodingKey {
            case fact, kind, modules, audience
            case validUntil = "valid_until"
            case dueOn = "due_on"
        }
    }
    private struct AddResponse: Decodable {
        let ok: Bool
        let error: String?
        var evicted: Int? = nil
        /// True when the fact went to owners only because it is about
        /// someone's job or pay (owner_memory.is_private).
        var privateDefault: Bool? = nil
        /// A staffing rule said back as the schedule will check it ("Checked
        /// on every draft as: at least 2 Server PM on Sat at dinner/night.")
        /// or that it can't be (schedule audit 10/3/26 D-14). Absent for
        /// anything else, and on an older server.
        var scheduleRule: ScheduleRuleReadback? = nil
        enum CodingKeys: String, CodingKey {
            case ok, error, evicted
            case privateDefault = "private_default"
            case scheduleRule = "schedule_rule"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = c.setupBool(.ok) ?? false
            error = c.setupText(.error)
            evicted = c.setupInt(.evicted)
            privateDefault = c.setupBool(.privateDefault)
            scheduleRule = (try? c.decodeIfPresent(ScheduleRuleReadback.self, forKey: .scheduleRule)) ?? nil
        }
    }

    struct ScheduleRuleReadback: Decodable, Equatable {
        let checked: Bool
        let text: String
        enum CodingKeys: String, CodingKey { case checked, text }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            checked = c.setupBool(.checked) ?? false
            text = c.setupText(.text) ?? ""
        }
    }
    private struct FactBody: Encodable { let fact: String }
    private struct IdBody: Encodable { let id: Int }
    private struct AudienceBody: Encodable { let id: Int; let audience: String }

    var memory: AccountMemory?
    var isLoading = false
    var errorMessage: String?
    var draft = Draft()
    var adding = false
    /// The fact (or archived fact) being forgotten or restored.
    var busyId: Int?
    /// The posted check's words after a save.
    var posted: String?
    /// The last staffing rule added, as the schedule will check it — kept
    /// under the form until the next add, since the posted check fades.
    var lastScheduleRule: ScheduleRuleReadback?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    var subtitle: String {
        guard let m = memory else { return "What Cavnar AI remembers, and who said it" }
        let n = m.facts.count
        return n == 0 ? "Nothing remembered yet" : "\(n) thing\(n == 1 ? "" : "s") it keeps in mind"
    }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let r: MemoryResponse = try await client.send("/mobile/api/account/memory", hapticOnError: false)
            memory = r.memory
            errorMessage = nil
        } catch let error as APIClient.APIError {
            if memory == nil { errorMessage = error.message }
        } catch is CancellationError {
        } catch {
            if memory == nil { errorMessage = "Couldn\u{2019}t load what Cavnar AI remembers." }
        }
    }

    /// The body the add form posts — pure, so the fields it sends are
    /// pinned by a test.
    static func addBody(_ d: Draft, today: Date = Date()) -> AddBody {
        let date = MemoryDate.iso(daysFrom: today, d.days)
        return AddBody(fact: d.fact.trimmingCharacters(in: .whitespacesAndNewlines), kind: d.kind,
                       modules: MemoryModule.all.map(\.key).filter { d.modules.contains($0) },
                       validUntil: d.kind == "followup" ? nil : date,
                       dueOn: d.kind == "followup" ? date : nil,
                       audience: d.audience)
    }

    func add() async {
        let body = Self.addBody(draft)
        guard !body.fact.isEmpty, !adding else { return }
        adding = true
        errorMessage = nil
        defer { adding = false }
        do {
            let r: AddResponse = try await client.send("/mobile/api/account/memory/add", method: .post,
                                                       body: body, retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t save that."; return }
            Haptic.success()
            lastScheduleRule = (r.scheduleRule?.text.isEmpty ?? true) ? nil : r.scheduleRule
            draft = Draft()
            if r.privateDefault == true {
                posted = "Remembered for owners only \u{2014} it is about someone\u{2019}s job or pay. "
                    + "Share it from its row to tell the team."
            } else {
                posted = (r.evicted ?? 0) > 0 ? "Remembered \u{2014} the oldest of its kind moved to the archive"
                                              : "Remembered"
            }
            await load()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t save that."
        }
    }

    func forget(_ fact: MemoryFact) async {
        guard busyId == nil else { return }
        busyId = fact.id
        errorMessage = nil
        defer { busyId = nil }
        do {
            let r: APIClient.OKResponse = try await client.send("/mobile/api/account/memory/forget", method: .post,
                                                                body: FactBody(fact: fact.fact), retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t forget that."; return }
            Haptic.success()
            await load()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t forget that."
        }
    }

    /// Who reads one fact, changed to the whole team — the one-tap change
    /// beside an owners-only fact.
    func share(_ fact: MemoryFact) async {
        guard busyId == nil else { return }
        busyId = fact.id
        errorMessage = nil
        defer { busyId = nil }
        do {
            let r: APIClient.OKResponse = try await client.send("/mobile/api/account/memory/audience", method: .post,
                                                                body: AudienceBody(id: fact.id, audience: "team"),
                                                                retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t share that."; return }
            Haptic.success()
            posted = "Shared with the team"
            await load()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t share that."
        }
    }

    private struct PinBody: Encodable { let id: Int; let pinned: Bool }
    private struct ScopeBody: Encodable { let id: Int; let scope: String }

    /// Pinned out of lane eviction, or unpinned (/account/memory/pin).
    func pin(_ fact: MemoryFact, pinned: Bool) async {
        await act(fact.id, "/mobile/api/account/memory/pin", PinBody(id: fact.id, pinned: pinned),
                  done: pinned ? "Pinned \u{2014} a full lane won\u{2019}t push it out" : "Unpinned",
                  failure: "Couldn\u{2019}t save that.")
    }

    /// Kept for every location of the group, or this one only (/account/memory/scope).
    func setScope(_ fact: MemoryFact, scope: String) async {
        await act(fact.id, "/mobile/api/account/memory/scope", ScopeBody(id: fact.id, scope: scope),
                  done: scope == "org" ? "Every location reads it now" : "This location only now",
                  failure: "Couldn\u{2019}t change that.")
    }

    /// An archived fact let go for good (/account/memory/dismiss).
    func dismiss(_ item: ArchivedFact) async {
        await act(item.id, "/mobile/api/account/memory/dismiss", IdBody(id: item.id),
                  done: "Dismissed", failure: "Couldn\u{2019}t dismiss that.")
    }

    private func act<B: Encodable & Sendable>(_ id: Int, _ path: String, _ body: B, done: String, failure: String) async {
        guard busyId == nil else { return }
        busyId = id
        errorMessage = nil
        defer { busyId = nil }
        do {
            let r: APIClient.OKResponse = try await client.send(path, method: .post, body: body, retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? failure; return }
            Haptic.success()
            posted = done
            await load()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = failure
        }
    }

    func restore(_ item: ArchivedFact) async {
        guard busyId == nil else { return }
        busyId = item.id
        errorMessage = nil
        defer { busyId = nil }
        do {
            let r: APIClient.OKResponse = try await client.send("/mobile/api/account/memory/restore", method: .post,
                                                                body: IdBody(id: item.id), retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t put that back."; return }
            Haptic.success()
            posted = "Put back"
            await load()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t put that back."
        }
    }
}
