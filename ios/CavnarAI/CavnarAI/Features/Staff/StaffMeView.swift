import SwiftUI
import UIKit

// Me (employee audit wave 2, I3 — UX-31, UX-17, H14, M11, V9–V11, C10):
// the employee's own settings as Account rows. My availability and What
// I'd like open sheets; schedule texts and reminders are state switches
// (never a native Toggle for a network setting); language, calendar,
// docs and help open sheets; Sign out is red text at the foot.

/// `StaffMeView(store: StaffPortalStore)` — the Me tab, whole: it brings its
/// own scroll view and ember pull-to-refresh (the container's contract for
/// Me). Calls use the environment's StaffSessionStore.
struct StaffMeView: View {
    let store: StaffPortalStore

    init(store: StaffPortalStore) { self.store = store }

    enum Sheet: String, Identifiable {
        case availability, preferences, language, calendar, email, changePin, locations
        case docs, help, messages, deleteAccount
        var id: String { rawValue }
    }

    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.scenePhase) private var scenePhase
    @State private var me: StaffLoad<StaffMeDetails> = .loading
    @State private var prefs: StaffLoad<StaffPreferencesState> = .loading
    @State private var notices: StaffLoad<StaffNotificationSettings> = .loading
    @State private var languages: StaffLoad<StaffLanguages> = .loading
    @State private var availability: StaffAvailabilityRecord?
    @State private var textsOn = false
    @State private var textsBusy = false
    @State private var textsError: String?
    @State private var remindersOn = true
    @State private var remindersBusy = false
    @State private var remindersError: String?
    @State private var sheet: Sheet?

    var body: some View {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Me")
                            .font(.cavnarHeadline(CavnarType.section))
                            .foregroundStyle(Color.cavnarInk)
                            .accessibilityAddTraits(.isHeader)
                        if let m = me.value {
                            Text([m.name, m.restaurant].filter { !$0.isEmpty }.joined(separator: " \u{00B7} "))
                                .font(.cavnarBody(CavnarType.body))
                                .foregroundStyle(Color.cavnarInk2)
                        }
                    }
                    if let failure = me.failure {
                        StaffUI.failedCard(failure) { Task { await reload() } }
                    }
                    weekSection
                    noticesSection
                    languageSection
                    accountSection
                    helpSection
                    AccountSection(kicker: "Leaving") {
                        AccountActionRow(label: "Delete my account",
                                         detail: "Removes your login here. Your manager is told.",
                                         symbol: "xmark", tone: .cavnarRed, showsDivider: false) {
                            sheet = .deleteAccount
                        }
                    }
                    Button {
                        Haptic.light()
                        // An explicit sign-out leaves no copy of this
                        // person's week on a shared phone (I2's StaffCache).
                        StaffCache.purgeAll()
                        staff.signOut()
                    } label: {
                        Text("Sign out")
                            .font(.cavnarBody(CavnarType.body, weight: 700))
                            .foregroundStyle(Color.cavnarRed)
                            .frame(maxWidth: .infinity, minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .padding(.top, 4)
                }
                .padding(.horizontal, 20)
                .padding(.top, 12)
                .padding(.bottom, 40)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .cavnarEmberRefreshable { await reload() }
        }
        .task { await reload() }
        .onChange(of: scenePhase) { _, phase in if phase == .active { Task { await reload() } } }
        .sheet(item: $sheet, onDismiss: { Task { await reload() } }) { which in
            sheetView(which).environment(staff)
        }
    }

    @ViewBuilder
    private func sheetView(_ which: Sheet) -> some View {
        switch which {
        case .availability: StaffAvailabilitySheet()
        case .preferences: StaffPreferencesSheet()
        case .language: StaffLanguageSheet()
        case .calendar: StaffCalendarSheet()
        case .email: StaffEmailEditView(store: staff)
        case .changePin: StaffChangePinView(store: staff)
        case .locations: StaffLocationSwitcherView(store: staff)
        case .docs: StaffDocsSheet()
        case .help: StaffHelpSheet()
        case .messages: StaffMessageThreadView(store: store)
        case .deleteAccount: StaffDeleteAccountView(store: staff)
        }
    }

    // MARK: Sections

    private var weekSection: some View {
        AccountSection(kicker: "Your week") {
            AccountNavRow(label: "My availability",
                          value: availability.map(StaffAvailabilityDraft.rowSummary)) { sheet = .availability }
            AccountNavRow(label: "What I\u{2019}d like", value: prefsSummary, showsDivider: false) { sheet = .preferences }
        }
    }

    private var prefsSummary: String? {
        guard let p = prefs.value else { return nil }
        var parts: [String] = []
        if p.preferredDayparts.contains("morning") { parts.append("Days") }
        if p.preferredDayparts.contains("night") { parts.append("Nights") }
        if let h = p.desiredHours { parts.append("\(Int(h.rounded()))h") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    @ViewBuilder
    private var noticesSection: some View {
        AccountSection(kicker: "Notices") {
            AccountKVRow(label: "This phone") {
                if let m = me.value {
                    AccountPill(text: m.pushOn ? "Getting notices" : "Not set up", on: m.pushOn)
                } else {
                    AccountValue(text: "\u{2014}", tone: .cavnarInk3)
                }
            }
            switch notices {
            case .loaded(let n):
                AccountSwitchRow(label: "Shift and task reminders",
                                 detail: ["Before your shift starts and before a critical task is due.", n.quietLine ?? ""]
                                    .filter { !$0.isEmpty }.joined(separator: " "),
                                 isOn: Binding(get: { remindersOn }, set: { on in
                                     remindersOn = on
                                     Task { await saveReminders(on) }
                                 }),
                                 busy: remindersBusy, showsDivider: textsRowShown)
                if let remindersError { StaffUI.errorLine(remindersError).padding(.bottom, 8) }
            case .failed:
                AccountKVRow(label: "Reminders", showsDivider: textsRowShown) {
                    StaffTextButton(title: "Try again") { Task { await reload() } }
                }
            case .loading:
                AccountKVRow(label: "Reminders", showsDivider: textsRowShown) {
                    CavnarSkeletonBar(height: 3).frame(width: 60)
                }
            }
            textsRow
        }
    }

    /// Hidden when texts can't go (H14): a switch that promises a text the
    /// restaurant can't send is worse than none.
    private var textsRowShown: Bool {
        if case .failed = prefs { return true }
        return prefs.value?.smsAvailable == true
    }

    @ViewBuilder
    private var textsRow: some View {
        switch prefs {
        case .loaded(let p) where p.smsAvailable:
            AccountSwitchRow(label: "Schedule texts",
                             detail: p.consentText ?? "Text me about my schedule and my requests. Msg & data rates may apply. Reply STOP to stop.",
                             isOn: Binding(get: { textsOn }, set: { on in
                                 textsOn = on
                                 Task { await saveTexts(on, version: p.consentVersion ?? 2) }
                             }),
                             busy: textsBusy, showsDivider: false)
            if let textsError { StaffUI.errorLine(textsError).padding(.bottom, 8) }
        case .failed:
            AccountKVRow(label: "Schedule texts", showsDivider: false) {
                StaffTextButton(title: "Try again") { Task { await reload() } }
            }
        default:
            EmptyView()
        }
    }

    private var languageSection: some View {
        AccountSection(kicker: "Language & calendar") {
            AccountNavRow(label: "Language", value: languages.value?.currentName) { sheet = .language }
            AccountNavRow(label: "Add shifts to my calendar", showsDivider: false) { sheet = .calendar }
        }
    }

    private var accountSection: some View {
        AccountSection(kicker: "Account") {
            AccountNavRow(label: "Email", value: me.value.map { $0.email.isEmpty ? "Add" : $0.email }) { sheet = .email }
            AccountKVRow(label: "Phone") {
                AccountValue(text: me.value.map { $0.hasPhone ? $0.phoneMasked : "Not on file" } ?? "\u{2014}",
                             isNumber: me.value?.hasPhone == true,
                             tone: me.value?.hasPhone == true ? .cavnarInk : .cavnarInk3)
                    .accessibilityLabel(me.value?.hasPhone == true
                                        ? "Phone ending \(String(me.value?.phoneMasked.suffix(4) ?? ""))" : "No phone on file")
            }
            if let locations = me.value?.locations, locations.count > 1 {
                AccountNavRow(label: "Locations", value: "\(locations.count) places") { sheet = .locations }
            }
            AccountNavRow(label: "Change PIN", showsDivider: false) { sheet = .changePin }
        }
    }

    private var helpSection: some View {
        AccountSection(kicker: "Help") {
            AccountNavRow(label: "Docs & house rules") { sheet = .docs }
            AccountNavRow(label: "Message your manager") { sheet = .messages }
            AccountNavRow(label: "How the app works", showsDivider: false) { sheet = .help }
        }
    }

    // MARK: Saves

    private func saveTexts(_ on: Bool, version: Int) async {
        textsBusy = true
        textsError = nil
        defer { textsBusy = false }
        do {
            let r: StaffPreferencesState = try await staff.authed(
                "/staff/api/preferences", method: .post,
                body: StaffTextsConsentBody(scheduleTexts: on, consentVersion: version))
            textsOn = r.scheduleTexts
            Haptic.success()
        } catch {
            textsOn = !on
            textsError = StaffErrorText.message(error, fallback: "Couldn\u{2019}t save that.")
        }
    }

    private struct RemindersBody: Encodable { let reminders: Bool }

    private func saveReminders(_ on: Bool) async {
        remindersBusy = true
        remindersError = nil
        defer { remindersBusy = false }
        do {
            let r: StaffNotificationSettings = try await staff.authed(
                "/staff/api/notifications", method: .post, body: RemindersBody(reminders: on))
            remindersOn = r.reminders
            notices = .loaded(r)
            Haptic.success()
        } catch {
            remindersOn = !on
            remindersError = StaffErrorText.message(error, fallback: "Couldn\u{2019}t save that.")
        }
    }

    // MARK: Load

    private func reload() async {
        async let m: StaffMeEnvelope = staff.authed("/staff/api/me")
        async let p: StaffPreferencesState = staff.authed("/staff/api/preferences")
        async let n: StaffNotificationSettings = staff.authed("/staff/api/notifications")
        async let l: StaffLanguages = staff.authed("/staff/api/language")
        async let a: StaffAvailabilityRecord = staff.authed("/staff/api/availability")
        do { me = .loaded(try await m.employee) } catch {
            if me.value == nil { me = .failed(StaffErrorText.message(error, fallback: "Your details didn\u{2019}t load.")) }
        }
        do {
            let v = try await p
            prefs = .loaded(v)
            if !textsBusy { textsOn = v.scheduleTexts }
        } catch {
            if prefs.value == nil { prefs = .failed(StaffErrorText.message(error)) }
        }
        do {
            let v = try await n
            notices = .loaded(v)
            if !remindersBusy { remindersOn = v.reminders }
        } catch {
            if notices.value == nil { notices = .failed(StaffErrorText.message(error)) }
        }
        do { languages = .loaded(try await l) } catch {
            if languages.value == nil { languages = .failed(StaffErrorText.message(error)) }
        }
        availability = try? await a
    }
}

// MARK: - Language (V11)

/// `StaffLanguageSheet()` — the manager-approved brief and focus line
/// are translated into this language; everything else stays as written.
struct StaffLanguageSheet: View {
    @Environment(StaffSessionStore.self) private var staff
    @State private var load: StaffLoad<StaffLanguages> = .loading
    @State private var saving: String?
    @State private var error: String?
    @State private var saved = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    StaffUI.note("Your manager\u{2019}s approved brief and tonight\u{2019}s focus read in this language. Everything else stays as written.")
                    switch load {
                    case .loading:
                        StaffUI.loading("Loading languages")
                    case .failed(let message):
                        StaffUI.failedCard(message) { Task { await reload() } }
                    case .loaded(let langs):
                        AccountSection(kicker: "Language") {
                            ForEach(Array(langs.languages.enumerated()), id: \.element.id) { i, choice in
                                Button {
                                    Task { await choose(choice.code) }
                                } label: {
                                    AccountKVRow(label: choice.name, showsDivider: i < langs.languages.count - 1) {
                                        if saving == choice.code {
                                            StaffBusyLabel(text: "Saving", color: .cavnarInk3)
                                                .font(.cavnarBody(CavnarType.secondary))
                                        } else if choice.code == langs.language {
                                            Image(systemName: "checkmark")
                                                .font(.system(size: 14, weight: .bold))
                                                .foregroundStyle(Color.cavnarEmber)
                                        }
                                    }
                                    .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                .disabled(saving != nil)
                                .accessibilityAddTraits(choice.code == langs.language ? .isSelected : [])
                            }
                        }
                        if let error { StaffUI.errorLine(error) }
                        if saved { CavnarInlinePosted(label: "Saved") { saved = false } }
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Language")
        }
        .task { await reload() }
    }

    private struct LanguageBody: Encodable { let language: String }

    private func reload() async {
        do { load = .loaded(try await staff.authed("/staff/api/language")) } catch {
            load = .failed(StaffErrorText.message(error, fallback: "Languages didn\u{2019}t load."))
        }
    }

    private func choose(_ code: String) async {
        guard load.value?.language != code else { return }
        saving = code
        error = nil
        defer { saving = nil }
        do {
            let r: StaffLanguages = try await staff.authed("/staff/api/language", method: .post, body: LanguageBody(language: code))
            load = .loaded(r)
            Haptic.success()
            saved = true
        } catch {
            self.error = StaffErrorText.message(error)
        }
    }
}

// MARK: - Calendar link (M11)

/// `StaffCalendarSheet()` — subscribe the phone's calendar to my own
/// published shifts, copy the link, or replace or turn off a link shared by
/// mistake (each named before it happens).
struct StaffCalendarSheet: View {
    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.openURL) private var openURL
    @State private var load: StaffLoad<StaffCalendarLink> = .loading
    @State private var busy: String?
    @State private var error: String?
    @State private var copied = false
    @State private var confirmReset = false
    @State private var confirmOff = false
    @State private var turnedOff = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if turnedOff {
                        CavnarEmptyHearth(title: "Calendar link turned off",
                                          message: "Calendars that used it stop updating. Get a new link any time.",
                                          ctaLabel: "Get a new link") { Task { await reset() } }
                    } else {
                        switch load {
                        case .loading:
                            StaffUI.loading("Loading your calendar link")
                        case .failed(let message):
                            StaffUI.failedCard(message) { Task { await reload() } }
                        case .loaded(let link):
                            content(link)
                        }
                    }
                    if let error { StaffUI.errorLine(error) }
                }
                .padding(20)
            }
            .accountSheetChrome("Your calendar")
        }
        .task { await reload() }
        .confirmationDialog("Get a new link?", isPresented: $confirmReset, titleVisibility: .visible) {
            Button("Get a new link") { Task { await reset() } }
            Button("Not now", role: .cancel) {}
        } message: {
            Text("The old link stops working in any calendar that uses it — subscribe again with the new one.")
        }
        .confirmationDialog("Turn off your calendar link?", isPresented: $confirmOff, titleVisibility: .visible) {
            Button("Turn it off", role: .destructive) { Task { await revoke() } }
            Button("Not now", role: .cancel) {}
        } message: {
            Text("Calendars that use it stop showing your shifts.")
        }
    }

    @ViewBuilder
    private func content(_ link: StaffCalendarLink) -> some View {
        StaffUI.note(link.note ?? "Your published shifts, in your phone\u{2019}s calendar. Changes show up within a few hours.")
        Button {
            if let url = URL(string: link.webcalURL) { openURL(url) }
        } label: {
            Text("Subscribe in Calendar").frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarPrimaryButtonStyle())
        Button {
            UIPasteboard.general.string = link.url
            Haptic.success()
            copied = true
        } label: {
            Text("Copy link").frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarSecondaryButtonStyle())
        if copied { CavnarInlinePosted(label: "Copied") { copied = false } }
        if let fetched = link.lastFetchedAt, let date = CavnarDate.timestamp(fetched) {
            HomeMixedText.make("A calendar last checked it \(CavnarDate.mdyTime(date)).",
                               size: CavnarType.caption, color: .cavnarInk3)
        }
        StaffUI.note("Anyone with this link can see your shifts. Shared it by mistake? Get a new one.")
        HStack(spacing: 18) {
            StaffTextButton(title: "Get a new link", busy: busy == "reset", busyTitle: "Getting one") { confirmReset = true }
            StaffTextButton(title: "Turn it off", tone: .cavnarRed, busy: busy == "off", busyTitle: "Turning off") {
                confirmOff = true
            }
        }
    }

    private func reload() async {
        do { load = .loaded(try await staff.authed("/staff/api/calendar-link")) } catch {
            load = .failed(StaffErrorText.message(error, fallback: "Your calendar link didn\u{2019}t load."))
        }
    }

    private func reset() async {
        busy = "reset"
        error = nil
        defer { busy = nil }
        do {
            load = .loaded(try await staff.authed("/staff/api/calendar-link/reset", method: .post, body: StaffEmptyBody()))
            turnedOff = false
            Haptic.success()
        } catch {
            self.error = StaffErrorText.message(error)
        }
    }

    private func revoke() async {
        busy = "off"
        error = nil
        defer { busy = nil }
        do {
            let r: StaffOKResponse = try await staff.authed("/staff/api/calendar-link/revoke", method: .post,
                                                           body: StaffEmptyBody())
            if r.ok { turnedOff = true; Haptic.success() } else { error = r.error ?? "That didn\u{2019}t go through." }
        } catch {
            self.error = StaffErrorText.message(error)
        }
    }
}

// MARK: - Docs, house rules and asking them (V9, V10)

/// `StaffDocsSheet()` — the house rules, the docs for my roles, my own
/// certificates, and an Ask box that answers only from those lines and
/// cites them; anything else is "Ask your manager", which opens the thread
/// with the question in it.
struct StaffDocsSheet: View {
    @Environment(StaffSessionStore.self) private var staff
    @State private var load: StaffLoad<StaffDocsPayload> = .loading
    @State private var openDoc: Int?
    @State private var question = ""
    @State private var asking = false
    @State private var answer: StaffAskAnswer?
    @State private var askError: String?
    @State private var messaging: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    switch load {
                    case .loading:
                        StaffUI.loading("Loading your docs")
                    case .failed(let message):
                        StaffUI.failedCard(message) { Task { await reload() } }
                    case .loaded(let docs):
                        if docs.canAsk { askBox }
                        if docs.isEmpty {
                            CavnarEmptyHearth(title: "No house rules yet",
                                              message: "When your manager adds the house rules or a how-to, it shows up here.",
                                              ctaLabel: "Message your manager") { messaging = "" }
                        } else {
                            docsList(docs)
                        }
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Docs & house rules")
        }
        .task { await reload() }
        .sheet(item: Binding(get: { messaging.map(DraftBox.init) }, set: { messaging = $0?.text })) { box in
            StaffMessageThreadView(draft: box.text).environment(staff)
        }
    }

    private struct DraftBox: Identifiable {
        let text: String
        var id: String { "draft:" + text }
    }

    private var askBox: some View {
        VStack(alignment: .leading, spacing: 10) {
            StaffUI.header("Ask the house rules")
            TextField("\u{201C}What\u{2019}s the rule on phones on the floor?\u{201D}", text: $question, axis: .vertical)
                .lineLimit(1...4)
                .cavnarTextFieldStyle()
                .onChange(of: question) { _, v in if v.count > 300 { question = String(v.prefix(300)) } }
            Button {
                Task { await ask() }
            } label: {
                Group {
                    if asking { StaffBusyLabel(text: "Looking it up") } else { Text("Ask") }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: asking || trimmedQuestion.isEmpty))
            .disabled(asking || trimmedQuestion.isEmpty)
            if let askError { StaffUI.errorLine(askError) }
            if let answer { answerCard(answer) }
        }
    }

    private var trimmedQuestion: String { question.trimmingCharacters(in: .whitespacesAndNewlines) }

    private func answerCard(_ a: StaffAskAnswer) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HomeMixedText.make(a.answer, size: CavnarType.body, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            if !a.sources.isEmpty {
                Text("FROM")
                    .font(.cavnarBody(CavnarType.kicker, weight: 700))
                    .tracking(1.0)
                    .foregroundStyle(Color.cavnarInk3)
                ForEach(a.sources) { s in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(s.source).font(.cavnarBody(CavnarType.caption, weight: 700)).foregroundStyle(Color.cavnarInk3)
                        HomeMixedText.make("\u{201C}\(s.line)\u{201D}", size: CavnarType.secondary, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
            if a.suggestMessage {
                StaffTextButton(title: "Ask your manager") { messaging = trimmedQuestion }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard(a.answered ? .ai : .card)
    }

    private func docsList(_ docs: StaffDocsPayload) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            if let rules = docs.houseRules {
                VStack(alignment: .leading, spacing: 8) {
                    StaffUI.header(rules.title.isEmpty ? "House rules" : rules.title)
                    HomeMixedText.make(rules.body, size: CavnarType.body, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                    if let at = rules.updatedAt {
                        HomeMixedText.make("Updated \(CavnarDate.mdyLocal(at))", size: CavnarType.caption, color: .cavnarInk3)
                    }
                }
                .cavnarCard()
            }
            if !docs.docs.isEmpty {
                StaffUI.header("For your role")
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(docs.docs) { d in
                        CavnarDropdown(title: d.title, subtitle: d.kindLabel,
                                       isExpanded: Binding(get: { openDoc == d.id }, set: { openDoc = $0 ? d.id : nil })) {
                            HomeMixedText.make(d.body, size: CavnarType.body, color: .cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                                .textSelection(.enabled)
                        }
                    }
                }
                .cavnarCard()
            }
            if !docs.certifications.isEmpty {
                AccountSection(kicker: "Your certificates") {
                    ForEach(Array(docs.certifications.enumerated()), id: \.element.id) { i, c in
                        AccountKVRow(label: c.cert.prefix(1).uppercased() + c.cert.dropFirst(),
                                     showsDivider: i < docs.certifications.count - 1) {
                            VStack(alignment: .trailing, spacing: 2) {
                                TonePill(text: c.chip.text, tone: c.chip.tone)
                                if let line = c.line {
                                    HomeMixedText.make(line, size: CavnarType.caption, color: .cavnarInk3)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private func reload() async {
        do { load = .loaded(try await staff.authed("/staff/api/docs")) } catch {
            load = .failed(StaffErrorText.message(error, fallback: "Your docs didn\u{2019}t load."))
        }
    }

    private func ask() async {
        let q = trimmedQuestion
        guard !q.isEmpty else { return }
        asking = true
        askError = nil
        answer = nil
        defer { asking = false }
        do {
            answer = try await staff.authed("/staff/api/ask", method: .post, body: StaffQuestionBody(question: q))
        } catch let e as APIClient.APIError {
            // 429 still offers the thread (suggest_message).
            if let refused = e.decodeBody(StaffAskAnswer.self), refused.suggestMessage {
                answer = StaffAskAnswer(answered: false, answer: e.message, suggestMessage: true)
            } else {
                askError = StaffErrorText.message(e)
            }
        } catch {
            askError = StaffErrorText.message(error)
        }
    }
}

// MARK: - Help

/// `StaffHelpSheet()` — how the app works, in the app's own words,
/// and the way to a person.
struct StaffHelpSheet: View {
    @Environment(StaffSessionStore.self) private var staff
    @State private var messaging = false

    private static let topics: [(String, String)] = [
        ("Can\u{2019}t make a shift?",
         "Open it on Today and choose Give up this shift or Swap this shift. You\u{2019}re still on it until your manager approves and someone picks it up."),
        ("Days away?", "Ask for time off on Requests. Your manager answers, and an approved range stays off the schedule."),
        ("Your usual week?", "Set My availability on Me \u{2014} the days and hours you can work. The next schedule reads it."),
        ("Picking up shifts", "Open shifts you can take are on Requests, under Waiting on you."),
        ("Notices", "Reminders come before your shift and before a critical task. Between 10pm and 8am they arrive silently."),
    ]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    ForEach(Self.topics, id: \.0) { topic in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(topic.0)
                                .font(.cavnarBody(CavnarType.body, weight: 700))
                                .foregroundStyle(Color.cavnarInk)
                                .accessibilityAddTraits(.isHeader)
                            StaffUI.note(topic.1, color: .cavnarInk2)
                        }
                    }
                    Button {
                        messaging = true
                    } label: {
                        Text("Message your manager").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .padding(.top, 6)
                    if let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String {
                        HomeMixedText.make("Cavnar AI \(version)", size: CavnarType.caption, color: .cavnarInk3)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("How the app works")
        }
        .sheet(isPresented: $messaging) {
            StaffMessageThreadView().environment(staff)
        }
    }
}
