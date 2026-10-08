import Foundation
import Observation

/// The three channels one goal drafts for.
enum StudioChannel: String, CaseIterable, Identifiable, Hashable {
    case text, email, social
    var id: String { rawValue }
    var label: String {
        switch self {
        case .text: return "Text"
        case .email: return "Email"
        case .social: return "Social"
        }
    }
}

/// How the Studio is opened: a goal typed or tapped, an Opportunity Feed
/// card (its goal, its channels, its key — every send names it as
/// `rec_key`), the drafted win-back, a past campaign used again or
/// improved, or a Content-tab text or email type handed over. Every entry
/// starts from a clean draft (the web's cpResetDraft).
struct StudioSeed: Identifiable {
    let id = UUID()
    var prompt: String = ""
    /// The channels to draft; nil = every channel that can reach someone.
    var channels: [StudioChannel]? = nil
    var recKey: String? = nil
    /// Draft at once (an idea, a card) rather than wait for Create.
    var autoCreate = false
    var winback: GuestWinback.Draft? = nil
    var reuseText: GuestCampaign? = nil
    var reuseEmail: GuestNewsletter? = nil
    /// "Improve with Cavnar AI" rather than "Use again".
    var improve = false
    /// A saved text draft's words (the old quiet-night guest text, a saved
    /// Re-engagement text): the Studio opens with them in the text box.
    var savedText: String? = nil
    /// A saved Weekly email's words: the Studio opens with them as the
    /// letter (the web's cpUseEmailText).
    var savedEmail: String? = nil

    /// A saved text or email draft opened from Drafts — with its words,
    /// as the web's _mktDraftToStudio opens it; nil for a post, which goes
    /// to the composer. "Tuesday night guest text" reads as the goal
    /// "Fill Tuesday dinner".
    static func savedDraft(_ draft: MarketingDraft) -> StudioSeed? {
        guard let channel = MarketingContentType.guestChannel(of: draft.contentType ?? "") else { return nil }
        if channel == "email" {
            var seed = StudioSeed(channels: [.email])
            seed.savedEmail = draft.body
            return seed
        }
        var seed = StudioSeed(prompt: goal(fromTopic: draft.topic ?? ""), channels: [.text])
        seed.savedText = draft.body
        return seed
    }

    static func goal(fromTopic topic: String) -> String {
        let suffix = " night guest text"
        let t = topic.trimmingCharacters(in: .whitespaces)
        guard t.hasSuffix(suffix) else { return t }
        let day = String(t.dropLast(suffix.count))
        guard !day.isEmpty, day.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "_" }) else { return t }
        return "Fill \(day) dinner"
    }
}

/// One line of the send's outcome, told straight, with the follow-up a line
/// can offer: retry the failed emails, or send to the new subscribers.
struct StudioResultLine: Identifiable, Equatable {
    let id = UUID()
    var good: Bool
    var text: String
    var newsletterId: Int? = nil
    var retryCount: Int? = nil
    var newCount: Int? = nil
    var busy = false
}

/// A check on the review sheet: a channel that can go, or why it can't.
struct StudioCheck: Identifiable, Equatable {
    let ok: Bool
    let text: String
    var id: String { text }
}

/// Exactly what one press sends — taken when the review sheet opens, so
/// what the owner read is what goes (CS-4).
struct StudioSnapshot: Equatable {
    var segment: String
    var text: CampaignTextSendBody?
    var winback: (id: Int, body: WinbackSendBody)?
    var email: NewsletterSendBody?
    var posts: [StudioPostBody] = []
    var sigs: [StudioChannel: String] = [:]
    var lines: [String] = []

    static func == (a: StudioSnapshot, b: StudioSnapshot) -> Bool {
        a.segment == b.segment && a.text == b.text && a.email == b.email && a.posts == b.posts
            && a.winback?.id == b.winback?.id && a.winback?.body == b.winback?.body
    }

    var isEmpty: Bool { text == nil && winback == nil && email == nil && posts.isEmpty }
}

/// The Campaign Studio on the phone (parity audit #30): one goal drafts the
/// text, the email and the post at once, one audience, one photo, one
/// review-and-send. The web's `_cp` state and cpPaint rules, as a model.
@Observable
@MainActor
final class CampaignStudioViewModel {
    // MARK: Context
    var overview: GuestOverview?
    var segments: [GuestSegment] = []
    var segmentDefaults: [String: String] = [:]
    var connected = MarketingChannels()
    private var connectedKnown = false
    /// The account's owner login — the only one who may set the mailing
    /// address every email prints (the server's rule, MB-14).
    var isOwner = false
    let photos: MarketingComposeViewModel
    var isLoading = false
    var loadError: String?
    /// A card that can reach nobody: said instead of drafting.
    var seedError: String?

    // MARK: Plan
    var prompt = ""
    /// The goal the drafts on screen were written from.
    private(set) var activePrompt = ""
    var promptError: String?
    private(set) var goal = ""
    private(set) var type = "general"
    private(set) var targetDay: String?
    private(set) var planned = false
    /// "Picked by Cavnar AI" — only once a plan chose the audience.
    private(set) var pickedByAI = false
    private(set) var segment = "all"
    private(set) var returns: [SegmentReturn] = []
    private var recKey: String?
    private var recFor: String?
    private(set) var winbackRef: (id: Int, segment: String)?
    private(set) var builderOpen = false

    // MARK: Channels
    private(set) var on: Set<StudioChannel> = []
    private var chanSet = false
    private(set) var busy: Set<StudioChannel> = []
    private(set) var errors: [StudioChannel: String] = [:]
    private var seq: [StudioChannel: Int] = [:]
    private var refs: [StudioChannel: Int] = [:]
    private var contentLogId: Int?

    // MARK: Drafts
    var message = ""
    var linkOn = false
    var linkURL = ""
    var subject = ""
    var preheader = ""
    var headline = ""
    var letter = ""
    var buttonLabel = ""
    var buttonURL = ""
    var mailingAddress = ""
    private(set) var previewHTML: String?
    private var previewTask: Task<Void, Never>?
    var caption = ""
    private(set) var platformOff: Set<String> = []

    // MARK: Send
    private(set) var sending = false
    var results: [StudioResultLine] = []
    /// Every channel the send gate held back, in order: a text AND an
    /// email can both be flagged by one send, and each gets its sheet.
    private(set) var gateFlags: [SendGateFlag] = []
    /// The flag on screen; dismissing it shows the next one.
    var gateFlag: SendGateFlag? {
        get { gateFlags.first }
        set {
            if let newValue {
                if !gateFlags.contains(newValue) { gateFlags.insert(newValue, at: 0) }
            } else if !gateFlags.isEmpty {
                gateFlags.removeFirst()
            }
        }
    }
    func clearGateFlags() { gateFlags = [] }
    /// The audience the owner picked, which a plan answering later never
    /// replaces.
    private(set) var ownerPickedSegment = false
    /// The connection dropped mid-send: the outcome is unknown, and the
    /// send stays off rather than inviting a blind second press.
    private(set) var outcomeUnknown = false
    private struct SentMark { let sig: String; let seg: String; let at: Date }
    private var sentMarks: [StudioChannel: SentMark] = [:]

    private let client: APIClient

    init(client: APIClient = .shared, photos: MarketingComposeViewModel? = nil) {
        self.client = client
        self.photos = photos ?? MarketingComposeViewModel(client: client)
    }

    // MARK: - Derived

    var selectedSegment: GuestSegment? { segments.first { $0.key == segment } }
    /// Everyone in the audience who joined by text.
    var textAudience: Int { selectedSegment?.count ?? 0 }
    /// Who a text reaches NOW (the three-day spacing out, CS-5).
    var textReach: Int { selectedSegment?.reach ?? 0 }
    var emailReach: Int { selectedSegment?.emailCount ?? 0 }
    var photo: MarketingMedia? { photos.media }
    var sms: GuestOverview.SMS { overview?.sms ?? GuestOverview.SMS() }
    var meter: SMSMeter { SMSMeter.measure(message: message, hasLink: hasLink, sms: sms) }
    var hasLink: Bool { linkOn && !linkURL.trimmingCharacters(in: .whitespaces).isEmpty }
    var minDays: Int { overview?.minDaysBetween ?? 3 }
    /// False outside the sending window: a text then waits for it to open.
    var sendingNow: Bool { overview?.sendingNow != false }
    var opensAt: String { SMSWindow.opens(overview?.window) ?? "sending hours open" }
    var isBusy: Bool { !busy.isEmpty }

    /// The connected accounts a post can go to. A caption for Instagram
    /// reads right on Facebook; Google gets its own kind of post, so it is
    /// the destination only when neither of the others is connected.
    var platforms: [(key: String, label: String)] {
        var out: [(key: String, label: String)] = []
        if connected.instagram { out.append(("instagram", "Instagram")) }
        if connected.facebook { out.append(("facebook", "Facebook")) }
        if out.isEmpty, connected.google { out.append(("google", "Google")) }
        return out
    }

    var selectedPlatforms: [(key: String, label: String)] { platforms.filter { !platformOff.contains($0.key) } }
    var socialType: String { platforms.first?.key == "google" ? "google_promo" : "instagram_post" }
    var needPhoto: Bool {
        !platformOff.contains("instagram") && photo == nil && platforms.first?.key == "instagram"
    }

    func isOn(_ k: StudioChannel) -> Bool { on.contains(k) }
    func isBusy(_ k: StudioChannel) -> Bool { busy.contains(k) }
    func error(_ k: StudioChannel) -> String? { errors[k] }
    /// A channel the restaurant can use at all (social needs an account).
    func isAvailable(_ k: StudioChannel) -> Bool { k != .social || !platforms.isEmpty }
    /// The channels shown as cards.
    var shownChannels: [StudioChannel] { StudioChannel.allCases.filter { on.contains($0) && isAvailable($0) } }

    func hasDraft(_ k: StudioChannel) -> Bool {
        switch k {
        case .text: return !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        case .email: return !letter.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        case .social: return !caption.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    var design: NewsletterDesign {
        NewsletterDesign(headline: trimmed(headline), preheader: trimmed(preheader), buttonLabel: trimmed(buttonLabel),
                         buttonUrl: trimmed(buttonURL), imageMediaId: photo?.id)
    }

    /// The card this draft began on, while its goal is still the card's.
    var effectiveRecKey: String { (recKey != nil && activePrompt == recFor) ? (recKey ?? "") : "" }

    /// What a channel would send now, as one string: a channel whose
    /// signature matches what it last sent is not offered again (CS-4).
    func signature(_ k: StudioChannel) -> String {
        switch k {
        case .text: return [message, segment, hasLink ? linkURL : ""].joined(separator: "\u{1}")
        case .email:
            let d = design
            return [subject, letter, d.headline, d.preheader, d.buttonLabel, d.buttonUrl,
                    String(d.imageMediaId ?? 0), segment].joined(separator: "\u{1}")
        case .social:
            return [caption, selectedPlatforms.map(\.key).joined(separator: ","), String(photo?.id ?? 0)]
                .joined(separator: "\u{1}")
        }
    }

    func justSent(_ k: StudioChannel) -> Bool { sentMarks[k]?.sig == signature(k) }

    /// Minutes since this channel last went to the audience picked now,
    /// inside half an hour; 0 otherwise.
    func recentMinutes(_ k: StudioChannel, now: Date = Date()) -> Int {
        guard let s = sentMarks[k], k == .social || s.seg == segment else { return 0 }
        let mins = now.timeIntervalSince(s.at) / 60
        return mins < 30 ? max(1, Int(mins.rounded())) : 0
    }

    var addressNeeded: Bool { on.contains(.email) && emailReach > 0 && !(overview?.mailingAddressSet ?? false) }
    var addressOK: Bool {
        (overview?.mailingAddressSet ?? false)
            || (isOwner && mailingAddress.replacingOccurrences(of: " ", with: "").count >= 8)
    }

    /// The checks and what can go, the web's cpPaint rules: each channel
    /// goes only when it can, and only when it differs from what it last
    /// sent; the label names the head counts.
    var checks: (lines: [StudioCheck], ready: Set<StudioChannel>, label: String) {
        var ck: [StudioCheck] = [], label: [String] = [], ready: Set<StudioChannel> = []
        if on.contains(.text) {
            let n = textReach, nAll = textAudience, m = meter
            if nAll == 0 { ck.append(.init(ok: false, text: "Nobody here has joined by text")) }
            else if n == 0 { ck.append(.init(ok: false, text: "Everyone here was texted in the last \(minDays) days")) }
            else if m.tooLong { ck.append(.init(ok: false, text: "Shorten the text by \(m.over) to send it")) }
            else if !sendingNow { ck.append(.init(ok: false, text: "Texts wait until \(opensAt)")) }
            else {
                ck.append(.init(ok: true, text: nAll > n
                    ? "\(mktPlural(nAll - n, "guest")) texted in the last \(minDays) days left out"
                    : "Nobody texted twice in \(minDays) days"))
            }
            let mins = recentMinutes(.text)
            if justSent(.text) { ck.append(.init(ok: true, text: "This text is on its way")) }
            else if mins > 0 { ck.append(.init(ok: false, text: "You texted this audience \(mktPlural(mins, "minute")) ago")) }
            if n > 0, hasDraft(.text), !m.tooLong, !busy.contains(.text), !justSent(.text) {
                ready.insert(.text)
                label.append("Text \(n)" + (sendingNow ? "" : " at \(opensAt)"))
            }
        }
        if on.contains(.email) {
            let m = emailReach
            if m == 0 { ck.append(.init(ok: false, text: "Nobody here is on your email list")) }
            else if !addressOK {
                ck.append(.init(ok: false, text: isOwner ? "Add your mailing address"
                                : "The account owner adds the mailing address before the first email"))
            } else { ck.append(.init(ok: true, text: "Unsubscribe link and address on every email")) }
            let mins = recentMinutes(.email)
            if justSent(.email) { ck.append(.init(ok: true, text: "This email is on its way")) }
            else if mins > 0 { ck.append(.init(ok: false, text: "You emailed this audience \(mktPlural(mins, "minute")) ago")) }
            if m > 0, addressOK, !trimmed(subject).isEmpty, hasDraft(.email), !busy.contains(.email), !justSent(.email) {
                ready.insert(.email)
                label.append("Email \(m)")
            }
        }
        if on.contains(.social), !platforms.isEmpty {
            let sel = selectedPlatforms
            if sel.isEmpty { ck.append(.init(ok: false, text: "Pick where to post")) }
            else if needPhoto { ck.append(.init(ok: false, text: "Instagram needs a photo")) }
            else { ck.append(.init(ok: true, text: "Posts to " + sel.map(\.label).joined(separator: " and "))) }
            let mins = recentMinutes(.social)
            if justSent(.social) { ck.append(.init(ok: true, text: "Posted")) }
            else if mins > 0 { ck.append(.init(ok: false, text: "You posted \(mktPlural(mins, "minute")) ago")) }
            if !sel.isEmpty, !needPhoto, hasDraft(.social), !busy.contains(.social), !justSent(.social) {
                ready.insert(.social)
                label.append("Post to " + sel.map(\.label).joined(separator: " & "))
            }
        }
        return (ck, ready, label.joined(separator: " \u{00B7} "))
    }

    /// What the past texts to this audience brought back, when measured.
    var selectedReturn: SegmentReturn? { returns.first { $0.segment == segment } }

    /// "~4 may come back within 14 days · as 12% of past texts to them did"
    /// — this audience's own measured rate times its reach, or nothing (CS-8).
    var textForecast: String? {
        let n = textReach
        guard n > 0 else { return nil }
        var bits: [String] = []
        if let back = overview?.backBySegment[segment] {
            bits.append("~\(Int((Double(n) * back.pct / 100).rounded())) may come back within 14 days \u{00B7} as \(back.label) of past texts to them did")
        }
        if hasLink, let tap = overview?.tapBySegment[segment] {
            bits.append("~\(Int((Double(n) * tap.pct / 100).rounded())) taps")
        }
        return bits.isEmpty ? nil : bits.joined(separator: " \u{00B7} ")
    }

    // MARK: - Load

    private struct SegmentsResponse: Decodable {
        let segments: [GuestSegment]
        let defaults: [String: String]
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            segments = ((try? c.decodeIfPresent([GuestSegment].self, forKey: .segments)) ?? nil) ?? []
            defaults = ((try? c.decodeIfPresent([String: String].self, forKey: .defaults)) ?? nil) ?? [:]
        }
        enum CodingKeys: String, CodingKey { case segments, defaults }
    }

    private struct ChannelsResponse: Decodable { let channels: MarketingChannels? }

    /// Who's listening, the audiences and the accounts a post can go to —
    /// read together.
    func load(connected known: MarketingChannels? = nil) async {
        if let known { connected = known; connectedKnown = true }
        isLoading = true
        loadError = nil
        defer { isLoading = false }
        async let ov: GuestOverview? = try? client.send("/mobile/api/guest-overview", hapticOnError: false)
        async let segs: SegmentsResponse? = try? client.send("/mobile/api/guest-segments", hapticOnError: false)
        async let chans: ChannelsResponse? = fetchChannels()
        async let media: Void = photos.loadMedia()
        let (o, s, c, _) = await (ov, segs, chans, media)
        if let o { overview = o }
        if let s { segments = s.segments; segmentDefaults = s.defaults }
        if let ch = c?.channels { connected = ch; connectedKnown = true }
        if o == nil || s == nil {
            loadError = "Couldn\u{2019}t load who\u{2019}s listening, so sending waits until it does. Pull to try again."
        }
        if !chanSet { on = defaultChannels() }
    }

    private func fetchChannels() async -> ChannelsResponse? {
        guard !connectedKnown else { return nil }
        return try? await client.send("/mobile/api/marketing", hapticOnError: false)
    }

    /// Text and email when someone can be reached on them (both when nobody
    /// can, so the goal still drafts), and the post when an account is on.
    private func defaultChannels() -> Set<StudioChannel> {
        var out: Set<StudioChannel> = []
        if (overview?.subscribers ?? 0) > 0 { out.insert(.text) }
        if (overview?.emailSubscribers ?? 0) > 0 { out.insert(.email) }
        if !platforms.isEmpty { out.insert(.social) }
        if !out.contains(.text) && !out.contains(.email) { out.formUnion([.text, .email]) }
        return out
    }

    /// A card's channels, only the ones that can reach someone: a text with
    /// nobody to text, an email with no list, a post with no account is
    /// never drafted (the web's mktOppChans).
    func reachable(_ wanted: [StudioChannel]) -> Set<StudioChannel> {
        var out: Set<StudioChannel> = []
        for k in wanted {
            switch k {
            case .text: if (overview?.subscribers ?? 0) > 0 { out.insert(k) }
            case .email: if (overview?.emailSubscribers ?? 0) > 0 { out.insert(k) }
            case .social: if !platforms.isEmpty { out.insert(k) }
            }
        }
        return out
    }

    static func noChannelLine(_ wanted: [StudioChannel]) -> String {
        if wanted == [.social] { return "Connect Instagram, Facebook or Google first \u{2014} Account \u{2192} Connections." }
        if wanted == [.email] { return "Nobody is on your email list yet." }
        if wanted == [.text] { return "Nobody can be texted yet." }
        return "Nobody can be texted or emailed yet, and no account is connected to post to."
    }

    // MARK: - Entry points

    /// Opens the Studio for one seed: reads the context, starts clean, and
    /// drafts at once when the seed says so.
    func start(_ seed: StudioSeed, connected known: MarketingChannels?, isOwner: Bool) async {
        self.isOwner = isOwner
        await load(connected: known)
        apply(seed)
        if seed.autoCreate, seedError == nil { await create() }
        else if seed.improve, let k = on.first { await draft(k, plan: false) }
    }

    func apply(_ seed: StudioSeed) {
        seedError = nil
        if let w = seed.winback {
            resetDraft()
            winbackRef = (w.id, w.segment ?? "lapsed_30")
            planned = true; pickedByAI = true
            on = [.text]; chanSet = true
            prompt = "Bring back guests " + (w.segmentLabel ?? "").lowercased()
            activePrompt = prompt
            goal = "Win back guests who drifted away"; type = "win_back"; targetDay = nil
            segment = w.segment ?? "lapsed_30"
            message = w.message
            builderOpen = true
            return
        }
        if let c = seed.reuseText {
            resetDraft()
            type = "general"; targetDay = nil; planned = true
            on = [.text]; chanSet = true
            segment = c.segment ?? "all"
            goal = seed.improve ? "Improve a past campaign" : "Send it again"
            prompt = String((seed.improve ? "A better version of this text: " : "Again: ").appending(c.message).prefix(280))
            activePrompt = prompt
            message = c.message
            builderOpen = true
            return
        }
        if let text = seed.savedEmail {
            // The web's cpUseEmailText: the letter as it was saved, the
            // email alone, no subject yet (the preview splits a
            // "SUBJECT LINE: … BODY: …" draft into the two).
            resetDraft()
            type = "general"; targetDay = nil; planned = true
            on = [.email]; chanSet = true
            prompt = "Your weekly email"
            activePrompt = prompt
            goal = "Send your weekly email"
            letter = text
            builderOpen = true
            schedulePreview(now: true)
            return
        }
        if let text = seed.savedText {
            // The saved text's words, with its goal ("Fill Tuesday dinner")
            // for a Rewrite or a fresh Create.
            resetDraft()
            on = [.text]; chanSet = true
            prompt = String(seed.prompt.prefix(280))
            activePrompt = prompt
            message = text
            builderOpen = true
            return
        }
        if let n = seed.reuseEmail {
            resetDraft()
            type = "general"; targetDay = nil; planned = true
            on = [.email]; chanSet = true
            segment = n.segment ?? "all"
            goal = seed.improve ? "Improve a past campaign" : "Send it again"
            prompt = String((seed.improve ? "A better version of this email: \(n.subject). \(n.body.prefix(180))"
                                          : "Again: \(n.subject)").prefix(280))
            activePrompt = prompt
            subject = n.subject; letter = n.body
            headline = n.design.headline; preheader = n.design.preheader
            buttonLabel = n.design.buttonLabel; buttonURL = n.design.buttonUrl
            if let mid = n.design.imageMediaId {
                if let known = photos.recentMedia.first(where: { $0.id == mid }) { photos.select(known) }
                else if let url = n.imageURL { photos.select(MarketingMedia(id: mid, token: "", url: url)) }
            }
            builderOpen = true
            schedulePreview(now: true)
            return
        }
        prompt = String(seed.prompt.prefix(280))
        if let key = seed.recKey, !key.isEmpty { recKey = key; recFor = prompt }
        if let wanted = seed.channels {
            let can = reachable(wanted)
            if can.isEmpty {
                seedError = Self.noChannelLine(wanted)
                return
            }
            on = can
            chanSet = true
        }
    }

    // MARK: - Create and draft

    /// Every channel on drafts at once from the goal; the first answer with
    /// a plan sets the goal, tone, audience and weekday.
    func create() async {
        guard !isBusy, !sending else { return }
        let goalText = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !goalText.isEmpty else {
            promptError = "Say what you want it to do \u{2014} e.g. fill Thursday dinner"
            return
        }
        promptError = nil
        var chans = shownChannels
        if chans.isEmpty { on.insert(.text); chans = [.text] }
        let keepRec = recKey, keepFor = recFor
        resetDraft()
        recKey = keepRec; recFor = keepFor
        activePrompt = String(goalText.prefix(280))
        chanSet = true
        builderOpen = true
        await withTaskGroup(of: Void.self) { group in
            for k in chans {
                group.addTask { await self.draft(k, plan: true) }
            }
        }
    }

    func rewrite(_ k: StudioChannel) async {
        guard !activePrompt.isEmpty, !busy.contains(k), !sending else { return }
        await draft(k, plan: false)
    }

    private struct TextDraftBody: Encodable {
        let prompt: String
        var type: String? = nil
        var runAsJob = true
        enum CodingKeys: String, CodingKey { case prompt, type; case runAsJob = "async" }
    }

    private struct EmailDraftBody: Encodable {
        let prompt: String
        var runAsJob = true
        enum CodingKeys: String, CodingKey { case prompt; case runAsJob = "async" }
    }

    private struct SocialDraftBody: Encodable {
        let type: String
        let topic: String
        var runAsJob = true
        enum CodingKeys: String, CodingKey { case type, topic; case runAsJob = "async" }
    }

    /// One channel's draft, polled as an AI job. An answer to an older
    /// request for the same channel is dropped (CS-16).
    func draft(_ k: StudioChannel, plan: Bool) async {
        let my = (seq[k] ?? 0) + 1
        seq[k] = my
        busy.insert(k)
        errors[k] = nil
        let goalText = activePrompt
        do {
            switch k {
            case .text:
                let started: APIClient.AIJobAnswer<StudioTextDraft> = try await client.send(
                    "/mobile/api/guest-campaign/draft", method: .post,
                    body: TextDraftBody(prompt: goalText, type: plan ? nil : type),
                    timeout: MarketingViewModel.generationTimeout, retryTransient: false)
                let d = try await client.resolveAIJob(started)
                guard seq[k] == my else { return }
                busy.remove(k)
                guard d.ok, let text = d.message else {
                    errors[k] = d.error ?? "Couldn\u{2019}t draft this one."
                    return
                }
                refs[.text] = d.draftRef
                if let r = d.returnsBySegment { returns = SegmentReturn.list(from: r) }
                applyPlan(plan, segment: d.segment, type: d.type, goal: d.goal, targetDay: d.targetDay)
                message = text
            case .email:
                let started: APIClient.AIJobAnswer<StudioEmailDraft> = try await client.send(
                    "/mobile/api/guest-newsletter/draft", method: .post,
                    body: EmailDraftBody(prompt: goalText),
                    timeout: MarketingViewModel.generationTimeout, retryTransient: false)
                let d = try await client.resolveAIJob(started)
                guard seq[k] == my else { return }
                busy.remove(k)
                guard d.ok, !d.body.isEmpty else {
                    errors[k] = d.error ?? "Couldn\u{2019}t draft this one."
                    return
                }
                refs[.email] = d.draftRef
                applyPlan(plan, segment: d.segment, type: d.type, goal: d.goal, targetDay: d.targetDay)
                subject = d.subject; preheader = d.preheader; headline = d.headline
                letter = d.body; buttonLabel = d.buttonLabel
                if trimmed(buttonURL).isEmpty, !d.buttonUrl.isEmpty { buttonURL = d.buttonUrl }
                schedulePreview(now: true)
            case .social:
                let started: APIClient.AIJobAnswer<StudioSocialDraft> = try await client.send(
                    "/mobile/api/marketing/generate-content", method: .post,
                    body: SocialDraftBody(type: socialType, topic: goalText),
                    timeout: MarketingViewModel.generationTimeout, retryTransient: false)
                let d = try await client.resolveAIJob(started)
                guard seq[k] == my else { return }
                busy.remove(k)
                guard let text = d.content, !text.isEmpty else {
                    errors[k] = d.error ?? "Couldn\u{2019}t draft this one."
                    return
                }
                refs[.social] = d.draftRef
                contentLogId = d.contentLogId
                caption = text
            }
        } catch is CancellationError {
            if seq[k] == my { busy.remove(k) }
        } catch let error as APIClient.APIError {
            guard seq[k] == my else { return }
            busy.remove(k)
            errors[k] = error.message
        } catch {
            guard seq[k] == my else { return }
            busy.remove(k)
            errors[k] = "Couldn\u{2019}t reach Cavnar AI."
        }
    }

    private func applyPlan(_ plan: Bool, segment seg: String?, type t: String?, goal g: String?, targetDay td: String?) {
        guard plan, !planned, let seg, !seg.isEmpty else { return }
        planned = true
        type = t ?? "general"
        goal = g ?? ""
        targetDay = td
        // The owner's own pick stands: a plan answering after it fills in
        // the goal and the day, never the audience (re-audit 10/8/26).
        guard !ownerPickedSegment else { return }
        segment = seg
        pickedByAI = true
    }

    /// A fresh start for every way in, so nothing of the last campaign
    /// rides along (CS-16, CS-9). Drafts still in flight are dropped.
    func resetDraft() {
        for k in StudioChannel.allCases { seq[k] = (seq[k] ?? 0) + 1 }
        busy = []; errors = [:]
        type = "general"; goal = ""; targetDay = nil; planned = false; pickedByAI = false; ownerPickedSegment = false
        winbackRef = nil; segment = "all"; refs = [:]; contentLogId = nil
        recKey = nil; recFor = nil
        message = ""; linkOn = false; linkURL = ""
        subject = ""; preheader = ""; headline = ""; letter = ""; buttonLabel = ""; buttonURL = ""
        previewTask?.cancel(); previewHTML = nil
        caption = ""
        photos.clearMedia()
        results = []; gateFlags = []; outcomeUnknown = false
    }

    // MARK: - Owner choices

    func toggle(_ k: StudioChannel) {
        guard !sending else { return }
        chanSet = true
        if on.contains(k) {
            on.remove(k)
        } else {
            on.insert(k)
            if builderOpen, !activePrompt.isEmpty, !hasDraft(k), !busy.contains(k) {
                Task { await draft(k, plan: false) }
            }
        }
    }

    func pickSegment(_ key: String) {
        guard !sending else { return }
        if let w = winbackRef, key != w.segment { winbackRef = nil }   // their own campaign now
        segment = key
        pickedByAI = false
        ownerPickedSegment = true
    }

    func togglePlatform(_ key: String) {
        guard !sending else { return }
        if platformOff.contains(key) { platformOff.remove(key) } else { platformOff.insert(key) }
    }

    /// Drops one channel's draft — the "Discard" on a flagged send.
    func discard(_ k: StudioChannel) {
        switch k {
        case .text: message = ""
        case .email: letter = ""; subject = ""; previewHTML = nil
        case .social: caption = ""
        }
        errors[k] = nil
    }

    // MARK: - The email's preview

    /// The server renders the email exactly as a guest gets it; asked again
    /// a beat after the owner stops typing.
    func schedulePreview(now: Bool = false) {
        previewTask?.cancel()
        previewTask = Task { [weak self] in
            if !now { try? await Task.sleep(for: .milliseconds(450)) }
            guard !Task.isCancelled else { return }
            await self?.refreshPreview()
        }
    }

    private struct PreviewBody: Encodable {
        let subject: String
        let body: String
        let design: NewsletterDesign
    }

    func refreshPreview() async {
        guard hasDraft(.email) else { previewHTML = nil; return }
        let sent = (subject, letter)
        guard let d: NewsletterPreview = try? await client.send(
            "/mobile/api/guest-newsletter/preview", method: .post,
            body: PreviewBody(subject: trimmed(subject), body: letter, design: design), hapticOnError: false),
              d.ok, let html = d.html else { return }
        guard sent == (subject, letter) || !Task.isCancelled else { return }
        previewHTML = html
        if trimmed(subject).isEmpty, let s = d.subject { subject = s }
        // A Weekly email arrives as "SUBJECT LINE: … BODY: …": the server
        // splits it, and the letter box keeps just the letter.
        if letter.trimmingCharacters(in: .whitespaces).lowercased().hasPrefix("subject"), let b = d.body { letter = b }
        if d.mailingAddress { overview?.mailingAddressSet = true }
    }

    // MARK: - Send

    /// Exactly the requests that would go now — the web's cpSnapshot.
    func snapshot() -> StudioSnapshot {
        let ready = checks.ready
        var snap = StudioSnapshot(segment: segment)
        let rk = effectiveRecKey
        if ready.contains(.text) {
            let hold = !sendingNow
            if let w = winbackRef {
                snap.winback = (w.id, WinbackSendBody(message: message, hold: hold))
            } else {
                snap.text = CampaignTextSendBody(message: message, segment: segment, type: type,
                                                 targetDay: targetDay ?? "", linkUrl: hasLink ? trimmed(linkURL) : "",
                                                 hold: hold, recKey: rk, draftRef: refs[.text])
            }
            snap.sigs[.text] = signature(.text)
            snap.lines.append(sendingNow ? "Text \(mktPlural(textReach, "guest"))"
                                         : "Text \(mktPlural(textReach, "guest")) at \(opensAt)")
        }
        if ready.contains(.email) {
            var body = NewsletterSendBody(subject: trimmed(subject), body: letter, design: design, segment: segment,
                                          recKey: rk, draftRef: refs[.email])
            if !(overview?.mailingAddressSet ?? false), isOwner, !trimmed(mailingAddress).isEmpty {
                body.mailingAddress = trimmed(mailingAddress)
            }
            snap.email = body
            snap.sigs[.email] = signature(.email)
            snap.lines.append("Email \(mktPlural(emailReach, "guest"))")
        }
        if ready.contains(.social) {
            for p in selectedPlatforms {
                snap.posts.append(StudioPostBody(platform: p.key, caption: caption, topic: activePrompt,
                                                 mediaId: photo?.id, imageURL: photo?.url ?? "", recKey: rk,
                                                 contentLogId: contentLogId, draftRef: refs[.social]))
            }
            snap.sigs[.social] = signature(.social)
            snap.lines.append("Post to " + selectedPlatforms.map(\.label).joined(separator: " and "))
        }
        return snap
    }

    /// Sends the snapshot the owner confirmed, one channel after another,
    /// each line told straight. Nothing sent can be taken back. False when
    /// it did not run — a second press while the first is in flight, or
    /// nothing to send — so the caller never reports a send that wasn't.
    @discardableResult
    func send(_ snap: StudioSnapshot) async -> Bool {
        guard !sending, !snap.isEmpty else { return false }
        sending = true
        results = []
        gateFlags = []
        var anyOk = false
        defer { sending = false }

        if let w = snap.winback {
            let ok = await sendText(path: "/mobile/api/guest-winback/\(w.id)/send", body: w.body, snap: snap)
            if ok { anyOk = true; winbackRef = nil }
        } else if let body = snap.text {
            if await sendText(path: "/mobile/api/guest-campaign/send", body: body, snap: snap) { anyOk = true }
        }
        if let body = snap.email {
            if await sendEmail(body, snap: snap) { anyOk = true }
        }
        for post in snap.posts {
            if await sendPost(post, snap: snap) { anyOk = true }
        }
        if anyOk {
            Haptic.success()
            // The counts moved: who can be texted now, the history.
            async let ov: GuestOverview? = try? client.send("/mobile/api/guest-overview", hapticOnError: false)
            async let segs: SegmentsResponse? = try? client.send("/mobile/api/guest-segments", hapticOnError: false)
            let (o, s) = await (ov, segs)
            if let o { overview = o }
            if let s { segments = s.segments }
        }
        return true
    }

    private func mark(_ k: StudioChannel, _ snap: StudioSnapshot) {
        if let sig = snap.sigs[k] { sentMarks[k] = SentMark(sig: sig, seg: snap.segment, at: Date()) }
    }

    private func line(_ good: Bool, _ text: String, newsletterId: Int? = nil, retry: Int? = nil, new: Int? = nil) {
        results.append(StudioResultLine(good: good, text: text, newsletterId: newsletterId, retryCount: retry,
                                        newCount: new))
    }

    private func sendText(path: String, body: any Encodable, snap: StudioSnapshot) async -> Bool {
        do {
            let r: CampaignSendResult = try await client.send(path, method: .post, body: body, retryTransient: false)
            return handleText(r, snap: snap)
        } catch let error as APIClient.APIError where error.status != nil {
            if let r = error.decodeBody(CampaignSendResult.self) { return handleText(r, snap: snap) }
            line(false, "Text: " + error.message)
        } catch is CancellationError {
            outcomeUnknown = true
            line(false, "Text: the connection dropped mid-send. Campaigns sent shows what went out.")
        } catch {
            // The answer was lost, not necessarily the send: the texts may
            // already be going. The same text again resumes that campaign
            // and texts nobody twice.
            outcomeUnknown = true
            line(false, "Text: the connection dropped mid-send. Campaigns sent shows what went out.")
        }
        return false
    }

    private func handleText(_ r: CampaignSendResult, snap: StudioSnapshot) -> Bool {
        if r.ok {
            mark(.text, snap)
            line(true, r.acceptedLine)
            return true
        }
        if r.isGateFlag {
            gateFlags.append(SendGateFlag(channel: "text", message: r.error ?? "Cavnar AI held this text back.",
                                          reasons: r.reasons))
            line(false, "Text: Cavnar AI flagged it \u{2014} nothing was sent")
        } else {
            if r.isQuietHours { overview?.sendingNow = false }
            line(false, "Text: " + (r.error ?? "couldn\u{2019}t send"))
        }
        return false
    }

    private func sendEmail(_ body: NewsletterSendBody, snap: StudioSnapshot) async -> Bool {
        let r: NewsletterSendResult
        do {
            r = try await client.send("/mobile/api/guest-newsletter", method: .post, body: body, retryTransient: false)
        } catch let error as APIClient.APIError where error.status != nil {
            guard let decoded = error.decodeBody(NewsletterSendResult.self) else {
                line(false, "Email: " + error.message)
                return false
            }
            r = decoded
        } catch {
            outcomeUnknown = true
            line(false, "Email: the connection dropped. Campaigns sent shows what went out; sending the same email again today mails nobody twice.")
            return false
        }
        return handleEmail(r, snap: snap)
    }

    private func handleEmail(_ r: NewsletterSendResult, snap: StudioSnapshot) -> Bool {
        if r.ok {
            mark(.email, snap)
            overview?.mailingAddressSet = true
            line(r.isClean, r.summary, newsletterId: r.newsletterId, retry: r.retryCount)
            return true
        }
        if r.alreadySent {
            line(false, r.summary, newsletterId: r.newsletterId, retry: r.retryCount, new: r.newCount)
        } else if r.needsMailingAddress {
            // The law's address isn't on file: the field opens (the owner's
            // to fill), and the same send goes again with it.
            overview?.mailingAddressSet = false
            line(false, "Email: " + (r.error ?? "add your mailing address first"))
        } else if r.gateFlagged {
            gateFlags.append(SendGateFlag(channel: "email", message: r.error ?? "Cavnar AI held this email back.",
                                          reasons: r.reasons))
            line(false, "Email: Cavnar AI flagged it \u{2014} nothing was sent")
        } else {
            line(false, "Email: " + (r.error ?? "couldn\u{2019}t send"))
        }
        return false
    }

    private struct PostAnswer: Decodable {
        let ok: Bool?
        let error: String?
    }

    private func sendPost(_ post: StudioPostBody, snap: StudioSnapshot) async -> Bool {
        let label = platforms.first { $0.key == post.platform }?.label ?? post.platform.capitalized
        do {
            let r: PostAnswer = try await client.send(post.path, method: .post, body: post, retryTransient: false)
            if r.ok == true {
                mark(.social, snap)
                line(true, "Posted to \(label)")
                return true
            }
            line(false, "\(label): " + (r.error ?? "couldn\u{2019}t post"))
        } catch let error as APIClient.APIError where error.status != nil {
            line(false, "\(label): " + error.message)
        } catch {
            outcomeUnknown = true
            line(false, "\(label): the connection dropped. Check the account before posting it again.")
        }
        return false
    }

    /// "Retry N failed" / "Send to the N new subscribers" on a result line
    /// — confirmed by the caller first.
    func followUp(_ lineID: UUID, retry: Bool) async {
        guard let i = results.firstIndex(where: { $0.id == lineID }), let id = results[i].newsletterId else { return }
        results[i].busy = true
        let r = await CampaignMail.followUp(client: client, newsletterId: id, retry: retry)
        guard let j = results.firstIndex(where: { $0.id == lineID }) else { return }
        results[j].busy = false
        results[j].good = r.isClean
        results[j].text = r.ok ? r.summary : "Email: " + (r.error ?? "couldn\u{2019}t send")
        results[j].retryCount = r.ok ? r.retryCount : results[j].retryCount
        results[j].newCount = nil
        if r.ok { Haptic.success() }
    }

    private func trimmed(_ s: String) -> String { s.trimmingCharacters(in: .whitespacesAndNewlines) }
}

/// The two follow-ups an email offers, shared by the Studio's result lines
/// and the history rows: retry the failures a retry can reach, or send to
/// the subscribers it never reached (guest_email.retry_failed /
/// send_to_new_subscribers). Never throws: a refusal comes back as the
/// result with its sentence.
enum CampaignMail {
    static func followUp(client: APIClient, newsletterId: Int, retry: Bool) async -> NewsletterSendResult {
        let path = "/mobile/api/guest-newsletter/\(newsletterId)/" + (retry ? "retry" : "send-new")
        do {
            return try await client.send(path, method: .post, retryTransient: false)
        } catch let error as APIClient.APIError {
            if let r = error.decodeBody(NewsletterSendResult.self) { return r }
            var r = NewsletterSendResult()
            r.error = error.message
            return r
        } catch {
            var r = NewsletterSendResult()
            r.error = "The connection dropped. Campaigns sent shows what went out."
            return r
        }
    }
}
