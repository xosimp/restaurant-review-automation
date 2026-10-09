import SwiftUI
import Observation

/// Alerts & digest → "Just for you" and "This location or all of them"
/// (memory round 9/29/26, M2 owner_layers — GET /account/preferences).
///
/// A login's own choices only ever take pushes away from its own phones —
/// the restaurant's alert settings above still apply to everyone: push on
/// or off, the alert types muted on this login's phone, its own quiet
/// hours, and whether it gets this location's morning brief. Every change
/// saves on its own (POST /account/preferences/mine), never a whole-screen
/// save. Which location's settings apply to every location (the group
/// owner's "Use everywhere", POST /account/preferences/apply-to-all) is a
/// web setting now — it rewrites other locations' rules (iOS readability
/// round [24]); the view model keeps `applyToAll` for it.
struct AccountMyNotifications: View {
    @State private var viewModel = AccountPreferencesViewModel()
    @State private var showingMuted = false

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.l) {
            if let prefs = viewModel.prefs {
                mine(prefs)
                // "Tonight's service" on the Lock Screen (parity audit #94).
                ServiceActivitySettingsSection()
            } else if viewModel.isLoading {
                CavnarSkeletonBar(height: 3)
                    .accessibilityLabel("Loading your own notification settings")
            }
            if let error = viewModel.errorMessage {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .task { await viewModel.load() }
        .sheet(isPresented: $showingMuted) {
            AccountMutedTypesSheet(viewModel: viewModel)
        }
    }

    // MARK: - Just for you

    private func mine(_ p: AccountPreferences) -> some View {
        AccountSection(kicker: "Just for you") {
            AccountSwitchRow(
                label: "Push to my phone",
                detail: "Only your own phones. The restaurant\u{2019}s alert settings still apply to everyone else.",
                isOn: Binding(get: { p.mine.pushEnabled },
                              set: { on in Task { await viewModel.saveMine(.pushEnabled(on)) } }),
                busy: viewModel.saving == "push_enabled",
                optimistic: false
            )
            // Types this login is sent a lot of and never opens (per login,
            // not the restaurant's sum) — one tap mutes it for them alone.
            ForEach(viewModel.neverOpened) { n in
                VStack(alignment: .leading, spacing: 6) {
                    CavnarMixedText("You never open \u{201C}\(n.label)\u{201D} on your phone (\(n.delivered) in \(n.days) days) \u{2014} mute it just for you?",
                                    role: .body)
                    Button {
                        Haptic.light()
                        Task { await viewModel.mute(n.alertType) }
                    } label: {
                        Text("Mute it for me")
                            .cavnarText(.label, color: .cavnarEmber2)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .disabled(viewModel.saving != nil)
                }
                .padding(.vertical, 9)
                AccountRowDivider()
            }
            AccountNavRow(label: "Muted on my phone",
                          value: p.mine.mutedTypes.isEmpty ? "None" : "\(p.mine.mutedTypes.count)",
                          valueIsNumber: !p.mine.mutedTypes.isEmpty) { showingMuted = true }
            AccountSwitchRow(
                label: "My own quiet hours",
                detail: p.mine.hasQuietHours
                    ? "Pushes to your phone wait from \(AccountPreferences.clock(p.mine.quietStart)) until \(AccountPreferences.clock(p.mine.quietEnd))."
                    : "On top of the restaurant\u{2019}s quiet hours, never instead of them.",
                isOn: Binding(get: { p.mine.hasQuietHours },
                              set: { on in Task { await viewModel.setQuietHours(on) } }),
                busy: viewModel.saving == "quiet",
                optimistic: false,
                showsDivider: p.mine.hasQuietHours || p.mine.morningBrief != nil
            )
            if p.mine.hasQuietHours {
                AccountKVRow(label: "From") {
                    DatePicker("", selection: viewModel.quietBinding(start: true), displayedComponents: .hourAndMinute)
                        .labelsHidden().tint(Color.cavnarEmber)
                }
                AccountKVRow(label: "Until", showsDivider: p.mine.morningBrief != nil) {
                    DatePicker("", selection: viewModel.quietBinding(start: false), displayedComponents: .hourAndMinute)
                        .labelsHidden().tint(Color.cavnarEmber)
                }
            }
            if let brief = p.mine.morningBrief {
                AccountSwitchRow(
                    label: "My morning brief",
                    detail: "This location\u{2019}s brief, sent to you.",
                    isOn: Binding(get: { brief },
                                  set: { on in Task { await viewModel.saveMine(.morningBrief(on)) } }),
                    busy: viewModel.saving == "morning_brief",
                    optimistic: false,
                    showsDivider: false
                )
            }
        }
    }
}

/// "Muted on my phone": every alert type this login may mute, each its own
/// switch that saves at once. Health, safety, an issue assigned to them and
/// a cover they are asked for can never be muted, and are not listed.
struct AccountMutedTypesSheet: View {
    let viewModel: AccountPreferencesViewModel

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Muted on my phone") {
                        GlowBadge(systemImage: "bell.slash", size: 56)
                    } subtitle: {
                        Text("Only your own phones. Everyone else still gets them.")
                    }
                    if let p = viewModel.prefs {
                        let types = p.checklist(extra: viewModel.neverOpened.map { ($0.alertType, $0.label) })
                        AccountSection(kicker: "Send these to my phone") {
                            if types.isEmpty {
                                Text("Nothing to choose from yet.")
                                    .cavnarText(.body).padding(.vertical, 9)
                            }
                            ForEach(Array(types.enumerated()), id: \.element.type) { i, t in
                                AccountSwitchRow(
                                    label: t.label,
                                    isOn: Binding(get: { !p.mine.mutedTypes.contains(t.type) },
                                                  set: { on in Task { await on ? viewModel.unmute(t.type)
                                                                              : viewModel.mute(t.type) } }),
                                    busy: viewModel.saving == "mute:" + t.type,
                                    optimistic: false,
                                    showsDivider: i < types.count - 1
                                )
                            }
                        }
                        Text("Health and safety mentions, an issue assigned to you and a cover you\u{2019}re asked for always reach you.")
                            .cavnarText(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let error = viewModel.errorMessage {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Muted on my phone")
        }
    }
}

// MARK: - Payload

/// GET /account/preferences. Every field lenient.
struct AccountPreferences: Decodable {
    struct Mine: Decodable, Equatable {
        var pushEnabled = true
        var mutedTypes: [String] = []
        var quietStart: String? = nil
        var quietEnd: String? = nil
        /// Nil when the server could not say (no team-access row).
        var morningBrief: Bool? = nil

        enum CodingKeys: String, CodingKey {
            case pushEnabled = "push_enabled"
            case mutedTypes = "push_muted_types"
            case quietStart = "quiet_start"
            case quietEnd = "quiet_end"
            case morningBrief = "morning_brief"
        }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            pushEnabled = ((try? c.decodeIfPresent(Bool.self, forKey: .pushEnabled)) ?? nil) ?? true
            mutedTypes = ((try? c.decodeIfPresent(HomeLenientList<String>.self, forKey: .mutedTypes)) ?? nil)?.items ?? []
            quietStart = ((try? c.decodeIfPresent(String.self, forKey: .quietStart)) ?? nil).flatMap { $0.isEmpty ? nil : $0 }
            quietEnd = ((try? c.decodeIfPresent(String.self, forKey: .quietEnd)) ?? nil).flatMap { $0.isEmpty ? nil : $0 }
            morningBrief = (try? c.decodeIfPresent(Bool.self, forKey: .morningBrief)) ?? nil
        }

        var hasQuietHours: Bool { quietStart != nil && quietEnd != nil }
    }

    struct Resolved: Decodable {
        var source: String? = nil
        init(from decoder: Decoder) throws {
            let c = try? decoder.container(keyedBy: K.self)
            source = ((try? c?.decodeIfPresent(String.self, forKey: .source)) ?? nil)?.lowercased()
        }
        enum K: String, CodingKey { case source }
    }

    struct AlertType: Decodable, Hashable {
        let type: String
        let label: String
    }

    struct Location: Decodable, Hashable {
        let id: Int
        var name: String? = nil
    }

    var mine = Mine()
    var location: [String: Resolved] = [:]
    var canApplyToAll = false
    var locations: [Location] = []
    var unmutable: Set<String> = []
    var alertTypes: [AlertType] = []

    enum CodingKeys: String, CodingKey {
        case mine, location, locations
        case canApplyToAll = "can_apply_to_all"
        case unmutable = "unmutable_types"
        case alertTypes = "alert_types"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        mine = ((try? c.decodeIfPresent(Mine.self, forKey: .mine)) ?? nil) ?? Mine()
        location = ((try? c.decodeIfPresent([String: Resolved].self, forKey: .location)) ?? nil) ?? [:]
        canApplyToAll = ((try? c.decodeIfPresent(Bool.self, forKey: .canApplyToAll)) ?? nil) ?? false
        locations = ((try? c.decodeIfPresent([Location].self, forKey: .locations)) ?? nil) ?? []
        unmutable = Set(((try? c.decodeIfPresent([String].self, forKey: .unmutable)) ?? nil) ?? [])
        alertTypes = ((try? c.decodeIfPresent([AlertType].self, forKey: .alertTypes)) ?? nil) ?? []
    }

    var locationCount: Int { locations.count }
    /// Sources are worth saying only in a group.
    var showsSources: Bool { locations.count > 1 && !sharedSettings.isEmpty }

    /// The shared settings this screen names, each with where it comes
    /// from (preferences.resolve: "all locations" / "this location" /
    /// "default") and the keys "Use everywhere" sends.
    var sharedSettings: [AccountPreferencesViewModel.SharedSetting] {
        let rows: [(String, String, [String])] = [
            ("quiet", "Quiet hours", ["alert_quiet_start", "alert_quiet_end"]),
            ("brief", "Morning brief", ["morning_brief_enabled", "morning_brief_hour"]),
            ("level", "How much to hear", ["briefing_level"]),
            ("cap", "Alerts per day", ["alert_max_per_day"]),
            ("voice", "Brand voice", ["voice_notes", "sign_off_name"]),
            ("never", "Never-say list", ["never_say"]),
        ]
        return rows.compactMap { id, title, keys in
            let sources = keys.compactMap { location[$0]?.source }
            guard !sources.isEmpty else { return nil }
            // Any key set here alone makes the whole setting this location's.
            let source = sources.contains("this location") ? "this location"
                : (sources.allSatisfy { $0 == "all locations" } ? "all locations" : (sources.first ?? "default"))
            return .init(id: id, title: title, keys: keys, source: source)
        }
    }

    /// The muted-types checklist: the server's labelled types, less the
    /// ones no login can mute; types the owner muted or never opens are
    /// kept even when the server sent no list (an older server).
    func checklist(extra: [(String, String)] = []) -> [AlertType] {
        var out = alertTypes.filter { !unmutable.contains($0.type) }
        var seen = Set(out.map(\.type))
        for (t, l) in extra where !seen.contains(t) && !unmutable.contains(t) {
            out.append(AlertType(type: t, label: l)); seen.insert(t)
        }
        // A muted type the server no longer labels stays listed so it can be
        // unmuted — but never by its raw key (re-audit L5).
        for t in mine.mutedTypes where !seen.contains(t) {
            out.append(AlertType(type: t, label: "An alert type no longer listed")); seen.insert(t)
        }
        return out
    }

    /// "9pm" / "9:30pm" from "21:00" / "21:30".
    static func clock(_ hm: String?) -> String {
        guard let hm, let d = hmDate(hm) else { return "—" }
        let t = CavnarDate.time(d)
        return t.hasSuffix(":00am") || t.hasSuffix(":00pm") ? t.replacingOccurrences(of: ":00", with: "") : t
    }

    static func hmDate(_ hm: String) -> Date? {
        let parts = hm.split(separator: ":").compactMap { Int($0) }
        guard parts.count >= 2 else { return nil }
        return Calendar.current.date(bySettingHour: parts[0], minute: parts[1], second: 0, of: Date())
    }

    static func hm(_ date: Date) -> String {
        let c = Calendar.current.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }
}

/// /notifications/engagement → `mine`: alert types delivered to this
/// login's phones and never opened by them (labelled by the server).
struct NeverOpenedType: Decodable, Identifiable, Hashable {
    let alertType: String
    let label: String
    let delivered: Int
    var days: Int = 60
    var id: String { alertType }

    enum CodingKeys: String, CodingKey {
        case label, delivered, days
        case alertType = "alert_type"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        alertType = try c.decode(String.self, forKey: .alertType)
        label = ((try? c.decodeIfPresent(String.self, forKey: .label)) ?? nil) ?? alertType
        delivered = ((try? c.decodeIfPresent(Int.self, forKey: .delivered)) ?? nil) ?? 0
        days = ((try? c.decodeIfPresent(Int.self, forKey: .days)) ?? nil) ?? 60
    }
}

// MARK: - View model

@Observable
@MainActor
final class AccountPreferencesViewModel {
    struct SharedSetting: Identifiable, Hashable {
        let id: String
        let title: String
        let keys: [String]
        let source: String

        var sourceLabel: String {
            switch source {
            case "all locations": return "All locations"
            case "this location": return "This location"
            default: return "Default"
            }
        }
    }

    enum Change {
        case pushEnabled(Bool)
        case morningBrief(Bool)
    }

    /// POST /account/preferences/mine — only the keys that changed.
    struct MineBody: Encodable, Equatable {
        var pushEnabled: Bool? = nil
        var mutedTypes: [String]? = nil
        var quietStart: String?? = nil
        var quietEnd: String?? = nil
        var morningBrief: Bool? = nil

        enum CodingKeys: String, CodingKey {
            case pushEnabled = "push_enabled"
            case mutedTypes = "push_muted_types"
            case quietStart = "quiet_start"
            case quietEnd = "quiet_end"
            case morningBrief = "morning_brief"
        }

        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encodeIfPresent(pushEnabled, forKey: .pushEnabled)
            try c.encodeIfPresent(mutedTypes, forKey: .mutedTypes)
            // A quiet time sent as null clears it; absent leaves it.
            if let q = quietStart { try c.encode(q, forKey: .quietStart) }
            if let q = quietEnd { try c.encode(q, forKey: .quietEnd) }
            try c.encodeIfPresent(morningBrief, forKey: .morningBrief)
        }
    }

    private struct MineResponse: Decodable {
        let ok: Bool
        let error: String?
        var mine: AccountPreferences.Mine? = nil
    }
    private struct EngagementResponse: Decodable {
        let ok: Bool
        var mine: HomeLenientListDecodable<NeverOpenedType>? = nil
    }
    private struct ApplyBody: Encodable { let keys: [String] }
    private struct ApplyResponse: Decodable {
        let ok: Bool
        let error: String?
        var locations: [JSONValue]? = nil
    }

    var prefs: AccountPreferences?
    var neverOpened: [NeverOpenedType] = []
    var isLoading = false
    var errorMessage: String?
    /// Which save is in flight ("push_enabled", "quiet", "mute:<type>", "apply").
    var saving: String?
    var appliedNote: String?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            prefs = try await client.send("/mobile/api/account/preferences", hapticOnError: false)
        } catch let error as APIClient.APIError {
            if prefs == nil { errorMessage = error.message }
        } catch {}
        if let e: EngagementResponse = try? await client.send("/mobile/api/notifications/engagement",
                                                              hapticOnError: false) {
            let muted = Set(prefs?.mine.mutedTypes ?? [])
            neverOpened = (e.mine?.items ?? []).filter { !muted.contains($0.alertType) }
        }
    }

    func saveMine(_ change: Change) async {
        switch change {
        case .pushEnabled(let on): await post(MineBody(pushEnabled: on), busy: "push_enabled")
        case .morningBrief(let on): await post(MineBody(morningBrief: on), busy: "morning_brief")
        }
    }

    func mute(_ type: String) async {
        var list = prefs?.mine.mutedTypes ?? []
        guard !list.contains(type) else { return }
        list.append(type)
        await post(MineBody(mutedTypes: list), busy: "mute:" + type)
        neverOpened.removeAll { prefs?.mine.mutedTypes.contains($0.alertType) ?? false }
    }

    func unmute(_ type: String) async {
        let list = (prefs?.mine.mutedTypes ?? []).filter { $0 != type }
        await post(MineBody(mutedTypes: list), busy: "mute:" + type)
    }

    /// On: 10pm to 7am to start, adjustable below. Off: both cleared.
    func setQuietHours(_ on: Bool) async {
        if on {
            await post(MineBody(quietStart: .some("22:00"), quietEnd: .some("07:00")), busy: "quiet")
        } else {
            await post(MineBody(quietStart: .some(nil), quietEnd: .some(nil)), busy: "quiet")
        }
    }

    /// The From / Until pickers: the wheel moves freely and the end saves
    /// once it has been still for a moment (re-audit L3: every tick of the
    /// wheel posted, and each answer snapped the wheel back mid-spin).
    var quietDraft: [Bool: Date] = [:]
    @ObservationIgnored private var quietSaveTask: [Bool: Task<Void, Never>] = [:]

    func quietBinding(start: Bool) -> Binding<Date> {
        Binding(
            get: {
                if let draft = self.quietDraft[start] { return draft }
                let hm = start ? self.prefs?.mine.quietStart : self.prefs?.mine.quietEnd
                return hm.flatMap(AccountPreferences.hmDate) ?? Date()
            },
            set: { date in
                self.quietDraft[start] = date
                self.quietSaveTask[start]?.cancel()
                self.quietSaveTask[start] = Task { @MainActor in
                    try? await Task.sleep(for: .milliseconds(Self.quietDebounceMs))
                    guard !Task.isCancelled else { return }
                    let hm = AccountPreferences.hm(date)
                    await self.post(start ? MineBody(quietStart: .some(hm)) : MineBody(quietEnd: .some(hm)),
                                    busy: "quiet")
                    // The server's value is the one shown from here on.
                    if self.quietDraft[start] == date { self.quietDraft[start] = nil }
                }
            })
    }

    static let quietDebounceMs = 800

    private func post(_ body: MineBody, busy: String) async {
        saving = busy
        errorMessage = nil
        defer { saving = nil }
        do {
            let r: MineResponse = try await client.send("/mobile/api/account/preferences/mine", method: .post,
                                                        body: body, retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t save that."; return }
            if let mine = r.mine {
                // The server's word on what is in force — the morning brief
                // is only in the reply when it was sent.
                var merged = mine
                if body.morningBrief == nil { merged.morningBrief = prefs?.mine.morningBrief }
                prefs?.mine = merged
            }
            Haptic.selection()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t save that."
        }
    }

    func applyToAll(_ setting: SharedSetting) async {
        saving = "apply"
        errorMessage = nil
        appliedNote = nil
        defer { saving = nil }
        do {
            let r: ApplyResponse = try await client.send("/mobile/api/account/preferences/apply-to-all",
                                                         method: .post, body: ApplyBody(keys: setting.keys),
                                                         retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t apply that."; return }
            Haptic.success()
            let n = r.locations?.count ?? prefs?.locationCount ?? 0
            appliedNote = "\(setting.title): every location now uses this one\u{2019}s (\(n) location\(n == 1 ? "" : "s"))"
            await load()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t apply that."
        }
    }
}

/// A list read element by element (HomeLenientList's rule) for types that
/// are only Decodable.
struct HomeLenientListDecodable<Element: Decodable>: Decodable {
    let items: [Element]

    init(from decoder: Decoder) throws {
        guard var list = try? decoder.unkeyedContainer() else { items = []; return }
        var out: [Element] = []
        while !list.isAtEnd {
            let before = list.currentIndex
            if let e = try? list.decode(Element.self) {
                out.append(e)
            } else {
                _ = try? list.decode(JSONValue.self)
            }
            if list.currentIndex == before { break }
        }
        items = out
    }
}
