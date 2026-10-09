import SwiftUI

/// Opened from Account's "Profile & details" row. Option A ("identity
/// card") from the account-sheet design review: the sheet opens on who
/// this restaurant is — monogram tile, name, location — with the admin-set
/// facts as chips, then the contact fields, hours and auto-approve.
/// Restaurant identity fields (name/location/neighborhood/vibe/known-for)
/// stay admin-managed because client_api.py matches several of them by
/// exact string (AI query construction, competitor lookups).
///
/// iOS readability round [74]/[25]: the brand voice, never-say list, menu
/// highlights, sign-off, reply language, time zone and the restaurant
/// profile are set on the web (one row). What's left saves one way each:
/// auto-approve saves on every switch; the contact fields save from a
/// pinned Save bar that appears once something changed, and Back or a
/// swipe-down with unsaved changes asks before throwing them away.
struct AccountProfileDetailView: View {
    let viewModel: AccountViewModel
    let profile: AccountProfile
    @Environment(\.dismiss) private var dismiss
    @Environment(SessionStore.self) private var sessionStore
    @State private var showingUpdateEmail = false
    @State private var showingLocationSwitcher = false
    @State private var locations = LocationSwitcherViewModel()

    @State private var ownerName: String
    @State private var ownerPhone: String
    /// The contact fields as last saved — what "changed" is measured against.
    @State private var savedOwnerName: String
    @State private var savedOwnerPhone: String
    @State private var showingHours = false
    @State private var autoApproveEnabled: Bool
    @State private var autoApprovePaused: Bool
    @State private var autoApproveCap: Int
    @State private var autoApproveEarned: Bool
    @State private var autoApprove4star: Bool
    @State private var showingAutoApproveMore = false

    private enum Field: Hashable { case ownerName, ownerPhone }

    static let languageOptions: [(value: String, label: String)] = [
        ("", "Match the review"), ("en", "English"), ("es", "Spanish"), ("fr", "French"),
        ("it", "Italian"), ("pt", "Portuguese"), ("de", "German"),
    ]
    static let capOptions = [1, 3, 5, 10, 20]
    @FocusState private var focusedField: Field?
    @State private var postedLabel: String?

    // Same 7-zone list as the admin Client Settings page and the backend's
    // own accepted-values whitelist (mobile_api.py's mobile_update_profile).
    static let timezoneOptions: [(value: String, label: String)] = [
        ("America/New_York", "Eastern (New York)"),
        ("America/Chicago", "Central (Chicago)"),
        ("America/Denver", "Mountain (Denver)"),
        ("America/Phoenix", "Arizona (Phoenix)"),
        ("America/Los_Angeles", "Pacific (Los Angeles)"),
        ("America/Anchorage", "Alaska (Anchorage)"),
        ("Pacific/Honolulu", "Hawaii (Honolulu)"),
    ]

    init(viewModel: AccountViewModel, profile: AccountProfile) {
        self.viewModel = viewModel
        self.profile = profile
        _ownerName  = State(initialValue: profile.ownerName ?? "")
        _ownerPhone = State(initialValue: PhoneFormat.display(profile.ownerPhone))
        _savedOwnerName = State(initialValue: profile.ownerName ?? "")
        _savedOwnerPhone = State(initialValue: PhoneFormat.display(profile.ownerPhone))
        let auto = viewModel.summary?.reviews
        _autoApproveEnabled = State(initialValue: auto?.enabled ?? false)
        _autoApprovePaused = State(initialValue: auto?.paused ?? false)
        _autoApproveCap = State(initialValue: auto?.dailyCap ?? 5)
        _autoApproveEarned = State(initialValue: auto?.earned ?? false)
        _autoApprove4star = State(initialValue: auto?.include4star ?? false)
    }

    private var isOwner: Bool { sessionStore.currentUser?.isOwner == true }

    /// Something typed that isn't saved yet.
    private var isDirty: Bool {
        ownerName.trimmingCharacters(in: .whitespaces) != savedOwnerName.trimmingCharacters(in: .whitespaces)
            || ownerPhone.filter(\.isNumber) != savedOwnerPhone.filter(\.isNumber)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    hero
                    chips
                    contactSection
                    hoursSection
                    autoApproveSection
                    webSection
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("Restaurant", isDirty: isDirty)
            .keyboardDoneToolbar { focusedField = nil }
            .cavnarPostedOverlay(postedLabel) { postedLabel = nil }
            // The one Save on this sheet, in thumb reach, only while there
            // is something to save.
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if isDirty || viewModel.isSavingProfile || viewModel.saveProfileError != nil {
                    CavnarPinnedBar(note: viewModel.saveProfileError, noteTone: .error) {
                        Button {
                            if isDirty {
                                Haptic.light()
                                ownerName = savedOwnerName
                                ownerPhone = savedOwnerPhone
                            }
                            viewModel.saveProfileError = nil
                        } label: {
                            Text(isDirty ? "Undo" : "Dismiss").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                        Button { Task { await saveContact() } } label: {
                            Group {
                                if viewModel.isSavingProfile {
                                    CavnarShimmerText(text: "Saving…")
                                } else {
                                    Text("Save changes")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSavingProfile || !isDirty))
                        .disabled(viewModel.isSavingProfile || !isDirty)
                    }
                }
            }
            .sheet(isPresented: $showingUpdateEmail) {
                UpdateEmailSheet(viewModel: viewModel)
            }
            .sheet(isPresented: $showingLocationSwitcher) {
                LocationSwitcherView { Task { await viewModel.load() } }
            }
            .sheet(isPresented: $showingHours) {
                AccountHoursSheet(viewModel: viewModel, profile: viewModel.summary?.profile ?? profile)
            }
            .task {
                if isOwner { await locations.load() }
            }
        }
    }

    /// Saves the contact fields that changed, and only those: the route
    /// writes just the keys sent, so a web edit to the brand voice, menu,
    /// language or time zone is never put back from this sheet (re-audit A2).
    private func saveContact() async {
        focusedField = nil
        let nameChanged = ownerName.trimmingCharacters(in: .whitespaces) != savedOwnerName.trimmingCharacters(in: .whitespaces)
        let phoneChanged = ownerPhone.filter(\.isNumber) != savedOwnerPhone.filter(\.isNumber)
        await viewModel.updateProfile(ownerName: nameChanged ? ownerName : nil,
                                      ownerPhone: phoneChanged ? ownerPhone : nil)
        if viewModel.saveProfileSucceeded {
            Haptic.success()
            savedOwnerName = ownerName
            savedOwnerPhone = ownerPhone
            postedLabel = "Saved"
        }
    }

    // MARK: - Identity

    private var initials: String {
        let words = profile.restaurantName.split(separator: " ")
        return String(words.prefix(2).compactMap { $0.first }).uppercased()
    }

    private var hero: some View {
        AccountHero(title: profile.restaurantName) {
            GlowBadge(systemImage: "building.2", size: 64, monogram: initials)
        } subtitle: {
            Text(subtitleLine)
        }
    }

    /// Onboarding data for Gia Mia had `neighborhood` re-stating the exact
    /// city/state `locationName` already shows ("St. Charles, IL" +
    /// "St. Charles, Illinois — downtown First Street Plaza"), so the hero
    /// read "St.Charles, IL - St. Charles, Illinois - downtown First
    /// St...". Fixed the source data, but this strips a repeated leading
    /// city/state clause from `neighborhood` before ever joining the two,
    /// so a future restaurant entered the same way can't reproduce it.
    private var subtitleLine: String {
        var parts: [String] = []
        if let loc = profile.locationName, !loc.isEmpty { parts.append(loc) }
        if let nb = profile.neighborhood, !nb.isEmpty {
            let detail = Self.stripCityOverlap(from: nb, cityLine: profile.locationName)
            if !detail.isEmpty { parts.append(detail) }
        }
        return parts.joined(separator: " · ")
    }

    private static func stripCityOverlap(from neighborhood: String, cityLine: String?) -> String {
        guard let cityLine, !cityLine.isEmpty else { return neighborhood }
        let city = cityLine.split(separator: ",").first.map(String.init) ?? cityLine
        guard !city.isEmpty else { return neighborhood }
        for separator in [" — ", " – ", " - "] {
            guard let range = neighborhood.range(of: separator) else { continue }
            let head = String(neighborhood[..<range.lowerBound])
            guard head.localizedCaseInsensitiveContains(city) else { return neighborhood }
            let tail = String(neighborhood[range.upperBound...]).trimmingCharacters(in: .whitespaces)
            return tail.isEmpty ? neighborhood : tail.prefix(1).uppercased() + tail.dropFirst()
        }
        return neighborhood
    }

    /// Known-for reads as one chip per thing ("Wood-fired pizza & house
    /// pasta" → two chips), not one long one.
    private var factChips: [String] {
        var out: [String] = []
        if let vibe = profile.vibe, !vibe.isEmpty { out.append(vibe) }
        if let knownFor = profile.knownFor {
            let parts = knownFor
                .replacingOccurrences(of: " and ", with: " & ")
                .split(whereSeparator: { $0 == "&" || $0 == "," })
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            out.append(contentsOf: parts.map { $0.prefix(1).uppercased() + $0.dropFirst() })
        }
        return out
    }

    // Chips are collapsed to just the first one at rest — a wall of orange
    // pills under the restaurant name was too much before you've even
    // reached the editable fields. Tapping the "+N" chip expands the rest;
    // tapping the trailing chip again (now "Less") collapses back. (The
    // "Set during onboarding" chip is gone — iOS readability round.)
    @State private var chipsExpanded = false

    @ViewBuilder
    private var chips: some View {
        let all = factChips
        if !all.isEmpty {
            AccountFlowLayout(spacing: 6) {
                AccountChip(text: all[0])
                if all.count > 1 {
                    if chipsExpanded {
                        ForEach(all.dropFirst().indices, id: \.self) { i in
                            AccountChip(text: all[i])
                        }
                        chipToggle(label: "Less", systemImage: "chevron.up", expand: false)
                    } else {
                        chipToggle(label: "+\(all.count - 1)", systemImage: "chevron.down", expand: true)
                    }
                }
            }
        }
    }

    private func chipToggle(label: String, systemImage: String, expand: Bool) -> some View {
        Button {
            Haptic.light()
            withAnimation(.easeOut(duration: 0.2)) { chipsExpanded = expand }
        } label: {
            HStack(spacing: 3) {
                Text(label)
                Image(systemName: systemImage).font(.cavnar(.tag)).accessibilityHidden(true)
            }
            .font(.cavnarBody(CavnarType.secondary, weight: 700))
            .foregroundStyle(Color.cavnarInk2)
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
            .background(Color.white.opacity(0.05))
            .overlay(Capsule().strokeBorder(Color.white.opacity(0.1), lineWidth: 1))
            .clipShape(Capsule())
            .cavnarHitTarget()
            .padding(.vertical, -6)
        }
        .buttonStyle(.plain)
    }

    // MARK: - Contact

    private var contactSection: some View {
        AccountSection(kicker: "Contact") {
            // The owner's name signs guest copy: an owner's to change
            // (mobile_update_profile refuses anyone else's edit).
            AccountField(label: "Owner", text: $ownerName, focus: $focusedField, field: .ownerName)
                .disabled(!isOwner)
            // The owner's phone is theirs too — the server refuses a
            // teammate's change to it (re-audit M15).
            AccountField(label: "Phone", text: $ownerPhone, focus: $focusedField, field: .ownerPhone, keyboardType: .phonePad, isNumber: true)
                .disabled(!isOwner)
                .onChange(of: ownerPhone) { _, v in let f = PhoneFormat.typing(v); if f != v { ownerPhone = f } }
            if !isOwner {
                Text("Only the account owner can change the owner\u{2019}s name and phone.")
                    .cavnarText(.caption, color: .cavnarInk2)
                    .padding(.vertical, 6)
            }
            // Shares AccountFieldRow's exact label/value/reserved-underline
            // footprint (see AccountDisplayRow's own doc comment) — Email
            // isn't edited inline (it opens its own sheet), but it sits in
            // this same card next to Owner/Phone and needs to measure the
            // same height as they do. The sheet changes THIS login's email,
            // so the row shows this login's, said as such (re-audit M14).
            Button {
                Haptic.light()
                showingUpdateEmail = true
            } label: {
                AccountDisplayRow(label: "Your sign-in email",
                                  value: viewModel.summary?.account.email ?? profile.ownerEmail ?? "\u{2014}",
                                  showsDivider: isOwner) {
                    AccountDisclosureChip()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if isOwner {
                // AccountKVRow, not a hand-rolled HStack — Locations is a
                // single-line "tap to go elsewhere" row, the same family
                // as Sign-in's Password/2FA rows, so it gets their exact
                // shared row height instead of improvising its own.
                Button {
                    Haptic.light()
                    showingLocationSwitcher = true
                } label: {
                    AccountKVRow(label: "Locations", showsDivider: false) {
                        HStack(spacing: 10) {
                            if !locations.locations.isEmpty {
                                Text("\(locations.locations.count)").cavnarText(.figureS, color: .cavnarInk2)
                            }
                            AccountDisclosureChip()
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: - Hours

    /// "Open 6 days · closes 10pm · 2 closed dates" — the week as it runs,
    /// not a count of stored keys ("7 days set", re-audit L9). Closed dates
    /// counted from today on; "Hours not set" when nothing is stored.
    static func hoursSummary(_ p: AccountProfile, today: String = AccountHoursSheet.isoDay(Date())) -> String {
        func decode(_ json: String?) -> [String: String] {
            guard let json, let data = json.data(using: .utf8),
                  let dict = try? JSONSerialization.jsonObject(with: data) as? [String: String] else { return [:] }
            return dict
        }
        let days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        let opens = decode(p.openTimesJson), closes = decode(p.closeTimesJson)
        var parts: [String] = []
        if opens.isEmpty && closes.isEmpty {
            parts.append("Hours not set")
        } else {
            let drafts = HoursDayDraft.drafts(days: days, opens: opens, closes: closes)
            let open = days.compactMap { drafts[$0] }.filter { !$0.closed }
            parts.append(open.count == 7 ? "Open every day" : "Open \(open.count) day\(open.count == 1 ? "" : "s")")
            let closeTimes = Set(open.compactMap { HoursFormat.parse($0.originalClose).map { HoursFormat.format(HoursFormat.date($0)) } })
            if closeTimes.count == 1, let t = closeTimes.first {
                parts.append("closes " + t.replacingOccurrences(of: ":00", with: ""))
            }
        }
        let upcoming = (p.closures ?? []).filter { !$0.isEmpty && $0 >= today }.count
        if upcoming > 0 { parts.append("\(upcoming) closed date\(upcoming == 1 ? "" : "s")") }
        return parts.joined(separator: " \u{00B7} ")
    }

    /// One row, the week in words; the kicker says what it is once.
    private var hoursSection: some View {
        AccountSection(kicker: "Hours") {
            Button {
                Haptic.light()
                showingHours = true
            } label: {
                HStack(spacing: CavnarSpace.s) {
                    CavnarMixedText(Self.hoursSummary(viewModel.summary?.profile ?? profile), role: .body)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    AccountDisclosureChip()
                }
                .padding(.vertical, 9)
                .frame(minHeight: 48)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("Opens your hours and closed dates")
        }
    }

    // MARK: - On the web

    /// The time zone and reply language as set — read here, changed on the web.
    private var webSubtitle: String {
        let current = viewModel.summary?.profile ?? profile
        // One short line, no nested brackets (re-audit L10): "English
        // replies · Central time".
        let zoneLabel = Self.timezoneOptions.first { $0.value == current.timezone }?.label ?? current.timezone
        let zone = zoneLabel.components(separatedBy: " (").first ?? zoneLabel
        let code = current.responseLanguage ?? ""
        let language = code.isEmpty ? "Replies match the review"
            : "\(Self.languageOptions.first { $0.value == code }?.label ?? code) replies"
        return "\(language) \u{00B7} \(zone) time"
    }

    private var webSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            CavnarWebLinkRow(title: "Voice, time zone & profile", subtitle: webSubtitle,
                             path: "account/restaurant",
                             actionLabel: isOwner ? "Edit on the web" : "Open on the web")
        }
        .accountCard()
    }

    // MARK: - Auto-approve

    // Saves on change — it's a rule with real consequences, so flipping it
    // should land immediately and visibly, same as 2FA's own Turn on/off.
    /// A save the server refused (a teammate's 403 owner_only, a dropped
    /// connection) puts every switch back to what is saved — the screen
    /// never shows a rule that isn't the one running (re-audit 10/8/26, #13).
    private func saveAutoApprove() {
        guard isOwner else { resyncAutoApprove(); return }
        Task {
            if await viewModel.saveAutoApprove(enabled: autoApproveEnabled, paused: autoApprovePaused,
                                               dailyCap: autoApproveCap, earned: autoApproveEarned,
                                               include4star: autoApprove4star) {
                Haptic.success()
            } else {
                Haptic.error()
                resyncAutoApprove()
            }
        }
    }

    /// The switches as saved (the account summary's reviews block).
    private func resyncAutoApprove() {
        let saved = Self.savedAutoApprove(viewModel.summary?.reviews)
        autoApproveEnabled = saved.enabled
        autoApprovePaused = saved.paused
        autoApproveCap = saved.dailyCap
        autoApproveEarned = saved.earned
        autoApprove4star = saved.include4star
    }

    /// What the switches read when nothing is being changed: the saved rule,
    /// or all off at the server's default cap.
    static func savedAutoApprove(_ reviews: AutoApproveSettings?) -> AccountViewModel.AutoApproveBody {
        AccountViewModel.AutoApproveBody(enabled: reviews?.enabled ?? false, paused: reviews?.paused ?? false,
                                         dailyCap: reviews?.dailyCap ?? 5, earned: reviews?.earned ?? false,
                                         include4star: reviews?.include4star ?? false)
    }

    /// The rule in words, as the web's card says it (parity #2: this read
    /// "Only drafted 5-star replies, never anything lower" while 4-star
    /// replies were going out too).
    private var autoApproveRuleLine: String {
        let bands = autoApprove4star ? "Drafted 5- and 4-star replies" : "Only drafted 5-star replies"
        let floor = autoApprove4star ? "never 3 stars or below, and anything sensitive still waits for you."
                                     : "never anything lower."
        return "\(bands), \(floor) \(viewModel.summary?.reviews.approvedToday ?? 0) auto-approved today."
    }

    /// Auto-approve publishes replies with nobody reading them first, so
    /// only the account owner changes it (client_api._do_auto_approve, 403
    /// owner_only); everyone else reads the rule as it stands.
    @ViewBuilder
    private var autoApproveSection: some View {
        if isOwner {
            autoApproveControls
        } else {
            AccountSection(kicker: "Auto-approve") {
                VStack(alignment: .leading, spacing: 4) {
                    CavnarMixedText(!autoApproveEnabled ? "Off \u{2014} every reply waits for someone to read it."
                                    : autoApprovePaused ? "Paused \u{2014} nothing posts on its own until it resumes."
                                    : autoApproveRuleLine, role: .body)
                    Text("Only the account owner can change auto-approve.")
                        .cavnarText(.caption, color: .cavnarInk2)
                }
                .padding(.vertical, 9)
            }
        }
    }

    /// The main switch and Paused; the 4-star band, the daily cap and
    /// "Extend it as you earn it" behind More (iOS readability round [74]).
    private var autoApproveControls: some View {
        AccountSection(kicker: "Auto-approve") {
            AccountSwitchRow(
                label: autoApprove4star ? "Post 5- and 4-star replies automatically" : "Post 5-star replies automatically",
                isOn: Binding(get: { autoApproveEnabled }, set: { on in autoApproveEnabled = on; saveAutoApprove() }),
                busy: viewModel.isSavingAutoApprove,
                showsDivider: autoApproveEnabled
            )
            if autoApproveEnabled {
                AccountSwitchRow(
                    label: "Paused",
                    detail: autoApprovePaused ? "Nothing posts on its own until you resume." : nil,
                    isOn: Binding(get: { autoApprovePaused }, set: { paused in autoApprovePaused = paused; saveAutoApprove() }),
                    busy: viewModel.isSavingAutoApprove,
                    showsDivider: true
                )
                if !autoApprovePaused {
                    CavnarMixedText(autoApproveRuleLine, role: .secondary)
                        .padding(.vertical, 9)
                }
                CavnarMoreToggle(hiddenCount: 3, total: nil, isExpanded: $showingAutoApproveMore)
                if showingAutoApproveMore {
                    AccountSwitchRow(
                        label: "Include 4-star reviews",
                        detail: "Same ceiling, same rule: anything sensitive still waits for you. Never 3 stars or below.",
                        isOn: Binding(get: { autoApprove4star }, set: { on in autoApprove4star = on; saveAutoApprove() }),
                        busy: viewModel.isSavingAutoApprove,
                        showsDivider: true
                    )
                    AccountKVRow(label: "Daily cap") {
                        Picker("", selection: Binding(get: { autoApproveCap }, set: { cap in
                            Haptic.selection(); autoApproveCap = cap; saveAutoApprove()
                        })) {
                            ForEach(Self.capOptions, id: \.self) { Text("\($0) a day").tag($0) }
                        }
                        .tint(Color.cavnarEmber)
                    }
                    AccountSwitchRow(
                        label: "Extend it as you earn it",
                        detail: "Once you've approved 10 replies on a star band in 30 days and edited at most 1 in 10, that band goes out on its own too \u{2014} 3\u{2605} at most, never lower.",
                        isOn: Binding(get: { autoApproveEarned }, set: { on in autoApproveEarned = on; saveAutoApprove() }),
                        busy: viewModel.isSavingAutoApprove,
                        showsDivider: false
                    )
                }
            }
            if let error = viewModel.autoApproveError {
                Text(error).cavnarText(.secondary, color: .cavnarRedText).padding(.bottom, 6)
            }
        }
    }
}
