import SwiftUI

/// The fields on the phone, in keyboard order. The preheader, letter and
/// button fields are the web's (re-audit 10/8/26 W6), so the keyboard's
/// Next never lands on a field that isn't on screen.
private enum StudioField: Hashable, CaseIterable {
    case prompt, message, link, subject, headline, caption
}

/// The Campaign Studio, as a sheet (parity audit #30). One goal — typed,
/// or an idea tapped — drafts every channel at once: the text in a phone
/// bubble with its counter, the email as the server renders it, the post
/// as a feed card. One plan (goal, the audience Cavnar AI picked, one
/// photo), one review-and-send with real head counts. The web's Campaigns
/// tab, shaped for a phone: progressive, one column, the send last.
struct CampaignStudioView: View {
    let seed: StudioSeed
    var connected: MarketingChannels?
    var isOwner: Bool
    /// Whether this login may send (may_publish): a teammate who can't
    /// drafts and reviews here, and the send says it is the owner's
    /// (re-audit 10/8/26 M4).
    var canPublish: Bool = true
    /// Told after a send, so the screen under the sheet re-reads.
    var onSent: () -> Void = {}

    @State private var vm = CampaignStudioViewModel()
    @State private var reviewing = false
    @State private var started = false
    /// The channels whose editors are open (readability round 10/8/26
    /// #61): a channel shows as guests will get it; "Edit" opens its fields.
    @State private var editing: Set<StudioChannel> = []
    @FocusState private var focused: StudioField?
    /// "Replace your edits?" before Redraft all writes every channel again.
    @State private var confirmingRedraft = false

    /// The web's four idea chips, word for word.
    static let ideas: [(label: String, prompt: String)] = [
        ("Bring back guests gone 30 days", "Bring back guests who haven't been in for 30 days"),
        ("Thank your regulars", "Thank our regulars for coming back"),
        ("Promote this week\u{2019}s special", "Promote this week's special"),
        ("Welcome back first-timers", "Welcome our first-time guests back"),
    ]

    var body: some View {
        NavigationStack {
            ScrollViewReader { scroll in
                ScrollView {
                    // Review first (readability round 10/8/26 #61): the goal,
                    // then what would go and to whom — the send's own summary
                    // — then each channel as guests get it, its editor behind
                    // "Edit"; Review and send is pinned.
                    VStack(alignment: .leading, spacing: CavnarSpace.m) {
                        goalCard
                        if vm.builderOpen {
                            reviewSummary
                            planCards
                            ForEach(vm.shownChannels) { k in
                                channelCard(k).id(k)
                            }
                        }
                    }
                    .padding(CavnarSpace.gutter)
                }
                .scrollDismissesKeyboard(.interactively)
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    if vm.builderOpen {
                        sendBar
                    }
                }
                .onChange(of: focusTarget) { _, target in
                    guard let target else { return }
                    editing.insert(target)
                    withAnimation(.easeOut(duration: 0.35)) { scroll.scrollTo(target, anchor: .top) }
                    focusTarget = nil
                }
            }
            // A drafted, unsent campaign asks before Back or a swipe drops
            // it (re-audit 10/8/26 H6).
            .accountSheetChrome("Campaign Studio", isDirty: vm.hasUnsentWork)
            .confirmationDialog("Replace your edits?", isPresented: $confirmingRedraft, titleVisibility: .visible) {
                Button("Redraft every channel", role: .destructive) {
                    Task { await vm.create(redraft: true) }
                }
                Button("Keep my drafts", role: .cancel) {}
            } message: {
                Text("Cavnar AI writes the text, the email and the post again from the goal. Your photo and the audience you picked stay.")
            }
            .keyboardNavToolbar($focused)
            .cavnarEmberRefreshable { await vm.load(connected: connected) }
            .task {
                guard !started else { return }
                started = true
                await vm.start(seed, connected: connected, isOwner: isOwner)
            }
            .onChange(of: vm.photo?.id) { _, _ in
                if vm.hasDraft(.email) { vm.schedulePreview(now: true) }
            }
            .sheet(isPresented: $reviewing) {
                CampaignReviewSheet(vm: vm, onSent: onSent) { channel in
                    reviewing = false
                    focusTarget = channel
                }
            }
        }
    }

    @State private var focusTarget: StudioChannel?

    /// The goal's Create, or — with drafts on screen — Redraft all behind
    /// "Replace your edits?" (H5).
    private func createOrConfirm() {
        if vm.builderOpen && vm.hasUnsentWork {
            confirmingRedraft = true
        } else {
            Task { await vm.create(redraft: vm.builderOpen) }
        }
    }

    // MARK: - Goal

    private var goalCard: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CampaignKicker(text: "Create", tag: vm.builderOpen && !vm.goal.isEmpty ? vm.goal : nil)
            Text("What should this campaign do?")
                .cavnarText(.headline)
            TextField("Bring back guests who haven\u{2019}t visited in 30 days", text: $vm.prompt, axis: .vertical)
                .lineLimit(1...3)
                .cavnarTextFieldStyle()
                .focused($focused, equals: .prompt)
                .submitLabel(.go)
                .onSubmit { createOrConfirm() }
                .onChange(of: vm.prompt) { _, text in
                    if text.count > 280 { vm.prompt = String(text.prefix(280)) }
                }
            if let error = vm.promptError {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
            }
            // The channels the campaign is drafted for and sent on, each
            // with its reach.
            VStack(alignment: .leading, spacing: 6) {
                Text("Draft for").cavnarText(.caption)
                ScrollView(.horizontal) {
                    HStack(spacing: 8) {
                        CampaignToggleChip(label: "Text", count: vm.overview?.subscribers, isOn: vm.isOn(.text)) {
                            vm.toggle(.text)
                        }
                        CampaignToggleChip(label: "Email", count: vm.overview?.emailSubscribers, isOn: vm.isOn(.email)) {
                            vm.toggle(.email)
                        }
                        if !vm.platforms.isEmpty {
                            CampaignToggleChip(label: vm.platforms.map(\.label).joined(separator: " & "),
                                               isOn: vm.isOn(.social)) { vm.toggle(.social) }
                        }
                    }
                }
                .scrollIndicators(.hidden)
            }
            // Create is the primary until the builder is open; then the
            // pinned "Review and send" is, and this is a secondary
            // "Redraft all…" behind its confirm (re-audit 10/8/26 H5).
            if vm.builderOpen {
                Button {
                    focused = nil
                    Haptic.light()
                    createOrConfirm()
                } label: {
                    Group {
                        if vm.isBusy {
                            CavnarShimmerText(text: "Drafting\u{2026}")
                        } else {
                            Label("Redraft all\u{2026}", systemImage: "arrow.triangle.2.circlepath")
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: vm.isBusy || vm.sending))
                .disabled(vm.isBusy || vm.sending)
            } else {
                Button {
                    focused = nil
                    Task { await vm.create() }
                } label: {
                    Group {
                        if vm.isBusy {
                            CavnarShimmerText(text: "Drafting\u{2026}")
                        } else {
                            Label("Create", systemImage: "sparkles")
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: vm.isBusy || vm.sending))
                .disabled(vm.isBusy || vm.sending)
            }

            if !vm.builderOpen {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Self.ideas, id: \.label) { idea in
                        Button {
                            Haptic.light()
                            vm.prompt = idea.prompt
                            Task { await vm.create() }
                        } label: {
                            HStack(spacing: 8) {
                                Image(systemName: "sparkle").font(.cavnar(.caption))
                                    .foregroundStyle(Color.cavnarEmber2)
                                Text(idea.label).cavnarText(.label)
                                Spacer(minLength: 0)
                                Image(systemName: "arrow.up.right").font(.cavnar(.caption))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                            .padding(.horizontal, 14)
                            .frame(minHeight: 44)
                            .background(RoundedRectangle(cornerRadius: CavnarRadius.control).fill(Color.white.opacity(0.04)))
                            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(vm.isBusy || vm.sending)
                    }
                }
            }
            if let line = vm.seedError ?? vm.loadError {
                Text(line)
                    .cavnarText(.secondary, color: vm.seedError != nil ? .cavnarRedText : .cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .cavnarCard(.ai)
    }

    // MARK: - Review

    /// What would go, with real head counts, to whom, and the checks — the
    /// send's own summary (CampaignReviewSheet's lines), up front. The
    /// review sheet is still the confirm: nothing goes from here.
    private var reviewSummary: some View {
        let state = vm.checks
        let snap = vm.snapshot()
        return VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("What would go")
            if snap.lines.isEmpty {
                Text(vm.isBusy ? "Drafting\u{2026}" : "Nothing is ready to send yet.")
                    .cavnarText(.body)
            }
            ForEach(snap.lines, id: \.self) { l in
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    Image(systemName: "arrow.up.right.circle.fill")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarEmber2)
                        .accessibilityHidden(true)
                    CavnarMixedText(l, role: .label)
                }
            }
            if let label = vm.selectedSegment?.label {
                Text("Audience: \(label)").cavnarText(.secondary)
            }
            ForEach(state.lines) { c in CampaignCheckLine(ok: c.ok, text: c.text) }
            if !vm.results.isEmpty {
                CampaignResultList(vm: vm)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard(.hero)
    }

    // MARK: - Plan

    private var planCards: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            audienceCard

            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker("Photo")
                Text("One photo for the email\u{2019}s header and the post.")
                    .cavnarText(.caption)
                MarketingPhotoPicker(viewModel: vm.photos)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
    }

    private var audienceCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            CampaignKicker(text: "Audience", tag: vm.pickedByAI ? "Picked by Cavnar AI" : "Your pick", tagIsAI: vm.pickedByAI)
            if vm.segments.isEmpty {
                Text(vm.loadError == nil ? "Reading who\u{2019}s listening\u{2026}" : "Couldn\u{2019}t load the audiences.")
                    .cavnarText(.secondary)
            } else {
                Menu {
                    ForEach(vm.segments) { s in
                        Button {
                            vm.pickSegment(s.key)
                        } label: {
                            if s.key == vm.segment {
                                Label(segmentMenuLine(s), systemImage: "checkmark")
                            } else {
                                Text(segmentMenuLine(s))
                            }
                        }
                        .disabled(s.count == 0 && (s.emailCount ?? 0) == 0)
                    }
                } label: {
                    HStack(alignment: .firstTextBaseline) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(vm.selectedSegment?.label ?? "Everyone consented")
                                .cavnarText(.label)
                            HomeMixedText.make("\(vm.textReach) text \u{00B7} \(vm.emailReach) email", role: .secondary)
                        }
                        Spacer()
                        Image(systemName: "chevron.up.chevron.down")
                            .font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                    .padding(12)
                    .frame(minHeight: 44)
                    .background(RoundedRectangle(cornerRadius: CavnarRadius.control).fill(Color.cavnarPaper2))
                    .contentShape(Rectangle())
                }
                .disabled(vm.sending)
                if let help = vm.selectedSegment?.help, !help.isEmpty {
                    Text(help)
                        .cavnarText(.caption)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let back = vm.overview?.backBySegment[vm.segment] {
                    HomeMixedText.make("\(back.label) came back within 14 days", role: .caption, color: .cavnarGreen)
                }
                if let ret = vm.selectedReturn {
                    CavnarMixedText(ret.line + " \u{2014} before and after, not proof.", role: .caption, color: .cavnarInk2)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    /// "Lapsed 30 days · 31 text · 18 email" — the text count is who a text
    /// reaches NOW.
    private func segmentMenuLine(_ s: GuestSegment) -> String {
        "\(s.label) \u{00B7} \(s.reach) text \u{00B7} \(s.emailCount ?? 0) email"
    }

    // MARK: - Channel cards

    @ViewBuilder
    private func channelCard(_ k: StudioChannel) -> some View {
        let isEditing = editing.contains(k)
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Text(k.label).cavnarText(.headline)
                switch k {
                case .text: HomeMixedText.make(mktPlural(vm.textReach, "guest"), role: .caption)
                case .email: HomeMixedText.make(mktPlural(vm.emailReach, "subscriber"), role: .caption)
                case .social: EmptyView()
                }
                Spacer()
                Button {
                    Haptic.light()
                    Task { await vm.rewrite(k) }
                } label: {
                    Image(systemName: "arrow.triangle.2.circlepath")
                        .font(.cavnar(.body))
                        .foregroundStyle(Color.cavnarEmber2)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .disabled(vm.isBusy(k) || vm.sending)
                .accessibilityLabel("Rewrite the \(k.label.lowercased())")
                Button {
                    Haptic.light()
                    withAnimation(.easeOut(duration: 0.2)) {
                        if isEditing { editing.remove(k) } else { editing.insert(k) }
                    }
                } label: {
                    Text(isEditing ? "Done" : "Edit")
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .disabled(vm.sending)
                .accessibilityLabel(isEditing ? "Done editing the \(k.label.lowercased())" : "Edit the \(k.label.lowercased())")
            }
            if vm.isBusy(k) {
                CavnarWorkingOrb(state: .composing, label: k == .email ? "Writing the email\u{2026}"
                                 : (k == .text ? "Writing the text\u{2026}" : "Writing the post\u{2026}"))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
            }
            switch k {
            case .text: textChannel(editing: isEditing)
            case .email: emailChannel(editing: isEditing)
            case .social: socialChannel(editing: isEditing)
            }
            if let error = vm.error(k) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(error)
                        .cavnarText(.secondary, color: .cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    Button("Draft it again") { Task { await vm.rewrite(k) } }
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minHeight: 44)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .opacity(vm.isBusy(k) ? 0.85 : 1)
    }

    // Text: the phone a guest holds — the number it comes from (never the
    // restaurant's name), the bubble opening with "{Name}: ", the link
    // and the STOP line — and the counter; the words, the link switch and
    // its address behind "Edit".
    private func textChannel(editing: Bool) -> some View {
        let meter = vm.meter
        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            VStack(spacing: 8) {
                HStack(spacing: 8) {
                    Circle().fill(Color.cavnarInk3.opacity(0.35)).frame(width: 26, height: 26)
                        .overlay(Image(systemName: "person.fill").font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk2))
                    VStack(alignment: .leading, spacing: 1) {
                        Text(vm.sms.sender.isEmpty ? "Your texting number" : vm.sms.sender)
                            .font(.cavnarNumber(CavnarType.caption, weight: 700))
                            .foregroundStyle(Color.cavnarInk)
                        Text("Text message").cavnarText(.caption)
                    }
                    Spacer()
                }
                Text("Today").cavnarText(.caption)
                HStack {
                    VStack(alignment: .leading, spacing: 6) {
                        if vm.message.isEmpty {
                            Text("Your text shows up here as guests will see it.")
                                .foregroundStyle(Color.cavnarInk3)
                        } else {
                            Text(meter.head + vm.message).foregroundStyle(Color.cavnarInk)
                        }
                        if vm.hasLink {
                            Text(vm.sms.linkExample.isEmpty ? "cavnar.ai/g/\u{2026}" : vm.sms.linkExample)
                                .foregroundStyle(Color.cavnarBlue)
                                .underline()
                        }
                        Text("Reply STOP to unsubscribe.").foregroundStyle(Color.cavnarInk2)
                    }
                    .font(.cavnar(.secondary))
                    .padding(.horizontal, 13)
                    .padding(.vertical, 10)
                    .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Color.cavnarPaper3))
                    .frame(maxWidth: 280, alignment: .leading)
                    Spacer(minLength: 0)
                }
            }
            .padding(CavnarSpace.m)
            .background(RoundedRectangle(cornerRadius: 22, style: .continuous).fill(Color.cavnarPaper))
            .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
            .accessibilityElement(children: .combine)
            .accessibilityLabel("What your guests see")

            if editing {
                TextEditor(text: $vm.message)
                    .font(.cavnar(.body))
                    .scrollContentBackground(.hidden)
                    .frame(minHeight: 96)
                    .padding(10)
                    .background(Color.cavnarPaper2)
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                    .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                        .strokeBorder(meter.tooLong ? Color.cavnarRed : Color.clear, lineWidth: 1))
                    .focused($focused, equals: .message)
                    .disabled(vm.sending)
            }

            HStack(spacing: 8) {
                meterChip("\(meter.counted) / \(meter.max)", warn: meter.tooLong, number: true)
                meterChip(meter.parts == 1 ? "Fits one text" : "\(meter.parts) texts per guest", warn: meter.parts > 1)
                Spacer()
                if editing {
                    Toggle(isOn: $vm.linkOn) {
                        Text("Track taps").font(.cavnarBody(CavnarType.secondary, weight: 700)).foregroundStyle(Color.cavnarInk2)
                    }
                    .toggleStyle(.switch)
                    .tint(Color.cavnarEmber)
                    .fixedSize()
                }
            }
            if meter.unicode {
                Text("A dash, curly quote or emoji sends this as Unicode: 70 characters a text.")
                    .cavnarText(.caption)
            }
            if editing && vm.linkOn {
                TextField("Your menu or booking page", text: $vm.linkURL)
                    .cavnarTextFieldStyle()
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($focused, equals: .link)
            }
            if vm.error(.text) == nil, let forecast = vm.textForecast {
                CavnarMixedText(forecast, role: .caption, color: .cavnarInk2)
            }
        }
    }

    private func meterChip(_ text: String, warn: Bool, number: Bool = false) -> some View {
        Text(text)
            .font(number ? .cavnarNumber(CavnarType.caption, weight: 700) : .cavnarBody(CavnarType.caption, weight: 700))
            .foregroundStyle(warn ? Color.cavnarAmber : Color.cavnarInk2)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(Capsule().fill(warn ? Color.cavnarAmber.opacity(0.12) : Color.white.opacity(0.05)))
    }

    // Email: the subject and headline as the inbox shows them; the server's
    // render at phone width and the six fields behind "Edit".
    private func emailChannel(editing: Bool) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            if !editing {
                if vm.hasDraft(.email) {
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        Text(vm.subject.isEmpty ? "No subject yet" : vm.subject)
                            .cavnarText(.label)
                            .fixedSize(horizontal: false, vertical: true)
                        if !vm.preheader.isEmpty {
                            Text(vm.preheader).cavnarText(.secondary).lineLimit(2)
                        }
                        if !vm.headline.isEmpty {
                            Text(vm.headline).cavnarText(.body, color: .cavnarInk).lineLimit(2)
                        }
                    }
                    .padding(CavnarSpace.s)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                    .accessibilityElement(children: .combine)
                } else {
                    Text("The email shows here as guests get it, once it\u{2019}s drafted.")
                        .cavnarText(.secondary)
                }
            } else {
                // The phone reviews the subject and the headline — what the
                // inbox shows — and reads the letter; the full email editor
                // and its rendered preview are the web's (re-audit 10/8/26
                // W6: six fields and a 520pt render on a phone).
                labeledField("Subject", text: $vm.subject, field: .subject, limit: 140)
                labeledField("Headline", text: $vm.headline, field: .headline, limit: 120)
                if vm.hasDraft(.email) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Letter").cavnarText(.caption)
                        Text(vm.letter.trimmingCharacters(in: .whitespacesAndNewlines))
                            .cavnarText(.body)
                            .lineLimit(8)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                CavnarWebLinkRow(title: "The whole email",
                                 subtitle: "The letter, the button and how it looks, on the web",
                                 path: "marketing/newsletter", actionLabel: "Open on the web")
            }
        }
        .onChange(of: vm.subject) { _, _ in vm.schedulePreview() }
        .onChange(of: vm.headline) { _, _ in vm.schedulePreview() }
        .onChange(of: vm.preheader) { _, _ in vm.schedulePreview() }
        .onChange(of: vm.buttonLabel) { _, _ in vm.schedulePreview() }
        .onChange(of: vm.buttonURL) { _, _ in vm.schedulePreview() }
    }

    private func labeledField(_ label: String, text: Binding<String>, field: StudioField, limit: Int,
                              placeholder: String = "", url: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).cavnarText(.caption)
            TextField(placeholder, text: text)
                .cavnarTextFieldStyle()
                .keyboardType(url ? .URL : .default)
                .textInputAutocapitalization(url ? .never : .sentences)
                .focused($focused, equals: field)
                .disabled(vm.sending)
                .onChange(of: text.wrappedValue) { _, v in
                    if v.count > limit { text.wrappedValue = String(v.prefix(limit)) }
                }
        }
    }

    // Social: where it goes, then the post as a feed shows it; the caption
    // field behind "Edit".
    private func socialChannel(editing: Bool) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            HStack(spacing: 8) {
                ForEach(vm.platforms, id: \.key) { p in
                    CampaignToggleChip(label: p.label, isOn: !vm.platformOff.contains(p.key)) { vm.togglePlatform(p.key) }
                }
            }
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 8) {
                    Circle().fill(Color.cavnarEmber.opacity(0.35)).frame(width: 26, height: 26)
                        .overlay(Image(systemName: "fork.knife").font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarInk))
                    Text("Your page").cavnarText(.label)
                    Spacer()
                }
                .padding(10)
                ZStack {
                    Color.cavnarPaper3
                    if let photo = vm.photo, let url = URL(string: photo.url) {
                        AsyncImage(url: url) { image in
                            image.resizable().aspectRatio(contentMode: .fill)
                        } placeholder: { Color.cavnarPaper3 }
                    } else {
                        Text("Add a photo above").cavnarText(.caption)
                    }
                }
                .frame(height: editing ? 220 : 160)
                .clipped()
                Text(vm.caption.isEmpty ? "Your caption shows here." : vm.caption)
                    .font(.cavnar(.secondary))
                    .foregroundStyle(vm.caption.isEmpty ? Color.cavnarInk3 : Color.cavnarInk)
                    .lineLimit(editing ? 6 : 3)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .background(Color.cavnarPaper)
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).strokeBorder(Color.cavnarPaper3, lineWidth: 1))

            if editing {
                TextEditor(text: $vm.caption)
                    .font(.cavnar(.body))
                    .scrollContentBackground(.hidden)
                    .frame(minHeight: 96)
                    .padding(10)
                    .background(Color.cavnarPaper2)
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                    .focused($focused, equals: .caption)
                    .disabled(vm.sending)
            }
            if vm.needPhoto {
                Text(vm.photos.recentMedia.isEmpty ? "Instagram needs a photo: take or add one above."
                     : "Instagram needs a photo: tap one of your photos above to use it.")
                    .cavnarText(.caption, color: .cavnarAmber)
            }
        }
    }

    // MARK: - Send

    /// Review and send, pinned in thumb reach (readability round #61): it
    /// opens the send's own confirm — the two-press send is unchanged.
    private var sendBar: some View {
        let state = vm.checks
        let note: String? = vm.outcomeUnknown
            ? "The answer was lost, so the send stays off \u{2014} check Campaigns sent before sending again."
            : (canPublish ? nil : "Only the owner can send a campaign \u{2014} ask them to send it from Campaigns.")
        return CavnarPinnedBar(note: note) {
            Button {
                focused = nil
                Haptic.light()
                reviewing = true
            } label: {
                Text(state.ready.isEmpty ? "Review and send" : "Review \u{00B7} " + state.label)
                    .frame(maxWidth: .infinity)
                    .lineLimit(2)
                    .multilineTextAlignment(.center)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: state.ready.isEmpty || vm.sending || vm.outcomeUnknown
                                                  || !canPublish))
            .disabled(state.ready.isEmpty || vm.sending || vm.outcomeUnknown || !canPublish)
        }
    }
}

/// The send's outcome, one line per channel, with the follow-ups an email
/// offers — each confirmed, since each sends again.
struct CampaignResultList: View {
    let vm: CampaignStudioViewModel
    @State private var pending: (id: UUID, retry: Bool, count: Int)?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(vm.results) { line in
                VStack(alignment: .leading, spacing: 4) {
                    CampaignCheckLine(ok: line.good, text: line.text)
                    HStack(spacing: 14) {
                        if let n = line.retryCount {
                            followButton("Retry \(n) failed", busy: line.busy) { pending = (line.id, true, n) }
                        }
                        if let n = line.newCount {
                            followButton("Send to the \(mktPlural(n, "new subscriber"))", busy: line.busy) {
                                pending = (line.id, false, n)
                            }
                        }
                    }
                    .padding(.leading, 21)
                }
            }
        }
        .accessibilityElement(children: .contain)
        .confirmationDialog(pending.map { $0.retry ? "Send it again to the \(mktPlural($0.count, "guest")) it failed to reach?"
                                                   : "Send it to the \(mktPlural($0.count, "new subscriber"))?" } ?? "",
                            isPresented: Binding(get: { pending != nil }, set: { if !$0 { pending = nil } }),
                            titleVisibility: .visible) {
            if let p = pending {
                Button(p.retry ? "Retry \(p.count)" : "Send to \(p.count)") {
                    Task { await vm.followUp(p.id, retry: p.retry) }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Nobody it already reached is emailed twice.")
        }
    }

    private func followButton(_ title: String, busy: Bool, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            Group {
                if busy { CavnarShimmerText(text: "Sending\u{2026}", color: .cavnarEmber2) }
                else { Text(title).font(.cavnarBody(CavnarType.secondary, weight: 700)).foregroundStyle(Color.cavnarEmber2) }
            }
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(busy)
    }
}

/// One review-and-send for every channel (the web's two-press send bar, as
/// a phone confirm): the checks, what goes with real head counts, the
/// mailing address when the law needs it, and one Send. What it shows is
/// what goes — nothing behind the sheet can change while it is open.
struct CampaignReviewSheet: View {
    let vm: CampaignStudioViewModel
    var onSent: () -> Void
    /// "Edit it" on a flagged send: back to the Studio, at that channel.
    var onEdit: (StudioChannel) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var didSend = false

    var body: some View {
        @Bindable var vm = vm
        let state = vm.checks
        let snap = vm.snapshot()
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    CavnarKicker("Review and send")
                    Text(didSend ? "What went out" : "Ready to send")
                        .cavnarText(.headline)
                    if !didSend {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(snap.lines, id: \.self) { l in
                                HStack(alignment: .firstTextBaseline, spacing: 8) {
                                    Image(systemName: "arrow.up.right.circle.fill")
                                        .font(.cavnar(.caption))
                                        .foregroundStyle(Color.cavnarEmber2)
                                        .accessibilityHidden(true)
                                    CavnarMixedText(l, role: .label)
                                }
                            }
                            if let label = vm.selectedSegment?.label {
                                Text("Audience: \(label)")
                                    .cavnarText(.secondary)
                            }
                        }
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .cavnarCard()
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(state.lines) { c in CampaignCheckLine(ok: c.ok, text: c.text) }
                        }
                        if vm.addressNeeded {
                            addressField(vm: vm)
                        }
                    }
                    if !vm.results.isEmpty {
                        CampaignResultList(vm: vm)
                            .padding(14)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .cavnarCard()
                    }
                    if !didSend || vm.results.contains(where: { !$0.good }) && !vm.outcomeUnknown && !state.ready.isEmpty {
                        // Said before the press, at a size to read (re-audit
                        // 10/8/26 M14): it sat under the button in Ink3.
                        if !didSend {
                            Text("A text or an email reaches every guest in the audience and can\u{2019}t be recalled. "
                                 + (vm.sendingNow ? "" : "Texts are queued and go at \(vm.opensAt)."))
                                .cavnarText(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Button {
                            Haptic.light()
                            Task {
                                // A second press while the first is in
                                // flight sends nothing and reports nothing.
                                guard await vm.send(vm.snapshot()) else { return }
                                didSend = true
                                onSent()
                            }
                        } label: {
                            Group {
                                if vm.sending {
                                    CavnarShimmerText(text: "Sending\u{2026}")
                                } else {
                                    Text(state.ready.isEmpty ? "Nothing ready to send" : state.label + " \u{2192}")
                                        .multilineTextAlignment(.center)
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: state.ready.isEmpty || vm.sending))
                        .disabled(state.ready.isEmpty || vm.sending)
                    }
                    if didSend {
                        Button {
                            dismiss()
                        } label: {
                            Text("Done").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Send")
            .interactiveDismissDisabled(vm.sending)
            .sheet(item: $vm.gateFlag) { flag in
                SendGateSheet(flag: flag, onEdit: {
                    // Back to the Studio at that channel: any other flagged
                    // channel's result line still says so there.
                    let k: StudioChannel = flag.channel == "email" ? .email : .text
                    vm.clearGateFlags()
                    onEdit(k)
                }, onDiscard: {
                    vm.discard(flag.channel == "email" ? .email : .text)
                })
            }
        }
        .presentationDetents([.medium, .large])
    }

    @ViewBuilder
    private func addressField(vm: CampaignStudioViewModel) -> some View {
        @Bindable var vm = vm
        VStack(alignment: .leading, spacing: 6) {
            Text("Mailing address \u{2014} the law prints it on every email")
                .font(.cavnarBody(CavnarType.caption, weight: 700))
                .foregroundStyle(Color.cavnarInk2)
            if vm.isOwner {
                TextField("Street, city, state and ZIP", text: $vm.mailingAddress)
                    .cavnarTextFieldStyle()
                    .textContentType(.fullStreetAddress)
                    .onChange(of: vm.mailingAddress) { _, v in
                        if v.count > 200 { vm.mailingAddress = String(v.prefix(200)) }
                    }
            } else {
                Text("Only the account owner can add it. Ask them to send the first email, or to add the address under Account.")
                    .font(.cavnarBody(CavnarType.secondary))
                    .foregroundStyle(Color.cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
