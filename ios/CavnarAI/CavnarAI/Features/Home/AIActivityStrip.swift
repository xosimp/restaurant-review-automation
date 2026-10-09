import SwiftUI

/// What Cavnar AI has actually been doing — the activity feed, on the phone.
///
/// One thin line under the pulse strip: a breathing ember dot and a
/// rotating sentence of what is armed right now ("Watching 6 competitors",
/// "Next review sweep at 4pm"). Tap it for the feed: what ran and when,
/// and what the AI is still holding. Every line comes from
/// /mobile/api/activity, which reads rows jobs wrote; the strip shows
/// nothing at all for an account with nothing running, because a feed that
/// invented activity would be the one thing this product must never do.
///
/// Not a card and not a notification: the quiet evidence that the platform
/// works between the moments the owner looks.
struct AIActivity: Decodable {
    struct Line: Decodable, Identifiable {
        let module: String?
        let kind: String?
        let text: String
        let at: String?
        var id: String { (at ?? "") + "|" + text }
    }
    struct LastRun: Decodable { let job: String?; let at: String? }
    /// Something the product is about to do on its own — the undo window
    /// (delayed.py). Cancel is a status change; nothing has gone out.
    struct Queued: Decodable, Identifiable {
        let id: Int
        let kind: String?
        let text: String
        let executeAt: String?
        enum CodingKeys: String, CodingKey { case id, kind, text; case executeAt = "execute_at" }
    }
    let ok: Bool
    let working: [Line]?
    let entries: [Line]?
    let memory: [Line]?
    let queued: [Queued]?
    let lastRun: LastRun?
    let generatedAt: String?
    enum CodingKeys: String, CodingKey {
        case ok, working, entries, memory, queued
        case lastRun = "last_run"
        case generatedAt = "generated_at"
    }
    var isEmpty: Bool { (working ?? []).isEmpty && (entries ?? []).isEmpty && (memory ?? []).isEmpty && (queued ?? []).isEmpty }
}

@Observable
@MainActor
final class AIActivityViewModel {
    var activity: AIActivity?
    var index = 0
    private var lastLoaded: Date?

    func load(force: Bool = false) async {
        if !force, let last = lastLoaded, Date().timeIntervalSince(last) < 55 { return }
        let fresh: AIActivity? = try? await APIClient.shared.send("/mobile/api/activity", hapticOnError: false)
        guard let fresh, fresh.ok else { return }
        withAnimation(.easeOut(duration: 0.35)) { activity = fresh }
        lastLoaded = Date()
    }

    var currentLine: String? {
        // Something queued to happen outranks the ambient line: the owner
        // should see "Publishing the schedule at 11am" before anything else.
        if let q = activity?.queued?.first { return q.text }
        guard let w = activity?.working, !w.isEmpty else { return nil }
        return w[index % w.count].text
    }

    private typealias OK = APIClient.OKResponse

    /// The undo. Returns the server's sentence on failure.
    func cancel(_ q: AIActivity.Queued) async -> String? {
        do {
            let r = try await APIClient.shared.undoQueuedAction(q.id)
            if r.ok {
                // What the undo did, and — for an automatic send it counts
                // against — the one-tap "why?" (memory round, M1 "undo").
                undoneNote = r.message
                askWhy = r.askWhy
                await load(force: true)
                return nil
            }
            return r.error ?? "That already went out."
        } catch let e as APIClient.APIError { return e.message }
        catch { return "Couldn\u{2019}t reach Cavnar AI." }
    }

    /// The server's sentence after an undo, and its question.
    var undoneNote: String?
    var askWhy: UndoAskWhy?

    func advance() {
        guard let w = activity?.working, w.count > 1 else { return }
        withAnimation(.easeInOut(duration: 0.35)) { index = (index + 1) % w.count }
    }
}

struct AIActivityStrip: View {
    let viewModel: AIActivityViewModel
    var paused: Bool = false
    @State private var showingFeed = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        if let line = viewModel.currentLine {
            Button {
                Haptic.light()
                showingFeed = true
            } label: {
                HStack(spacing: 9) {
                    BreathingDot(color: .cavnarEmber, paused: paused || reduceMotion)
                    HomeMixedText.make(line, size: CavnarType.caption, weight: 600, color: .cavnarInk2)
                        .lineLimit(1)
                        .id(line)
                        .transition(.opacity.combined(with: .move(edge: .bottom)))
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(Color.cavnarInk3)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 9)
                .background(Color.cavnarPaper2.opacity(0.55), in: Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Cavnar AI is working: \(line). Opens the activity feed.")
            .task(id: paused) {
                // Rotate the line every seven seconds while on screen.
                guard !paused else { return }
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(7))
                    viewModel.advance()
                }
            }
            .sheet(isPresented: $showingFeed) {
                AIActivityFeedSheet(viewModel: viewModel)
            }
        }
    }
}

/// A dot that breathes — the same shape the web strip and the Home
/// "Monitoring N signals" pill use.
struct BreathingDot: View {
    var color: Color = .cavnarEmber
    var paused: Bool = false
    @State private var swell = false

    var body: some View {
        ZStack {
            Circle().fill(color.opacity(0.35))
                .frame(width: 8, height: 8)
                .scaleEffect(swell ? 2.0 : 1.0)
                .opacity(swell ? 0 : 0.7)
            Circle().fill(color).frame(width: 8, height: 8)
                .shadow(color: color.opacity(0.6), radius: 4)
        }
        .frame(width: 16, height: 16)
        .onAppear {
            guard !paused else { return }
            withAnimation(.easeInOut(duration: 2.4).repeatForever(autoreverses: false)) { swell = true }
        }
    }
}

struct AIActivityFeedSheet: View {
    let viewModel: AIActivityViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var undoError: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if let a = viewModel.activity, !a.isEmpty {
                        if let q = a.queued, !q.isEmpty {
                            section("About to happen") {
                                ForEach(q) { item in
                                    HStack(alignment: .top, spacing: 10) {
                                        BreathingDot(color: .cavnarEmber).padding(.top, 2)
                                        VStack(alignment: .leading, spacing: 2) {
                                            HomeMixedText.make(item.text, size: CavnarType.secondary, weight: 600, color: .cavnarInk)
                                            if let at = item.executeAt, let when = Self.clock(at) {
                                                Text("at \(when)").font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk3)
                                            }
                                        }
                                        Spacer(minLength: 0)
                                        Button {
                                            Haptic.light()
                                            Task { if let err = await viewModel.cancel(item) { undoError = err } else { Haptic.success() } }
                                        } label: {
                                            Text("Undo").cavnarText(.label, color: .cavnarEmber2).cavnarHitTarget()
                                        }
                                        .buttonStyle(.plain)
                                    }
                                    .padding(.vertical, 8)
                                }
                                if let undoError {
                                    Text(undoError).font(.cavnar(.caption)).foregroundStyle(Color.cavnarRed)
                                }
                                if let note = viewModel.undoneNote {
                                    Text(note).font(.cavnarBody(CavnarType.caption, weight: 600)).foregroundStyle(Color.cavnarGreen)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                        if let w = a.working, !w.isEmpty {
                            section("Right now") {
                                ForEach(w) { line in row(line.text, tone: .cavnarEmber, breathing: true, ago: nil) }
                            }
                        }
                        if let es = a.entries, !es.isEmpty {
                            section("Recently") {
                                ForEach(es) { line in row(line.text, tone: .cavnarGreen, breathing: false, ago: Self.ago(line.at)) }
                            }
                        }
                        if let m = a.memory, !m.isEmpty {
                            section("Still holding") {
                                ForEach(m) { line in row(line.text, tone: .cavnarInk3, breathing: false, ago: nil) }
                            }
                        }
                        Text("Every line is something that actually ran, with its time. Nothing here is generated to fill space.")
                            .font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        Text("Nothing to show yet. Once data is flowing, everything Cavnar AI does for this restaurant is listed here as it happens.")
                            .font(.cavnar(.body))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .navigationTitle("Working for you")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }.font(.cavnar(.label))
                }
            }
            .task { await viewModel.load(force: true) }
            .undoWhyDialog(Binding(get: { viewModel.askWhy }, set: { viewModel.askWhy = $0 }))
        }
    }

    private func section<Content: View>(_ kicker: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(kicker.uppercased())
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .tracking(1.4)
                .foregroundStyle(Color.cavnarInk3)
                .padding(.bottom, 4)
            VStack(alignment: .leading, spacing: 0) { content() }
                .cavnarCard()
        }
    }

    private func row(_ text: String, tone: Color, breathing: Bool, ago: String?) -> some View {
        HStack(alignment: .top, spacing: 10) {
            if breathing {
                BreathingDot(color: tone).padding(.top, 2)
            } else {
                Circle().fill(tone).frame(width: 7, height: 7).padding(.top, 6).padding(.horizontal, 4)
            }
            VStack(alignment: .leading, spacing: 2) {
                HomeMixedText.make(text, size: CavnarType.secondary, weight: 500, color: .cavnarInk2)
                if let ago {
                    Text(ago).font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 8)
    }

    /// "11:00am" in the viewer's clock from an ISO-8601 UTC stamp.
    static func clock(_ iso: String) -> String? {
        guard let t = ISO8601DateFormatter().date(from: iso) else { return nil }
        let f = DateFormatter(); f.dateFormat = "h:mma"; f.amSymbol = "am"; f.pmSymbol = "pm"
        return f.string(from: t)
    }

    /// "12 min ago" from an ISO-8601 UTC stamp.
    static func ago(_ iso: String?) -> String? {
        guard let iso, let t = ISO8601DateFormatter().date(from: iso) else { return nil }
        let m = Int(Date().timeIntervalSince(t) / 60)
        if m < 1 { return "just now" }
        if m < 60 { return "\(m) min ago" }
        let h = m / 60
        if h < 24 { return h == 1 ? "1 hour ago" : "\(h) hours ago" }
        let d = h / 24
        return d == 1 ? "1 day ago" : "\(d) days ago"
    }
}

/// "Text to 212 guests · at 4:30pm  [Undo]" — something Cavnar AI is about to
/// send on its own (the undo window, delayed.py), as an amber banner at the
/// top of Home (iOS readability round, 10/8/26, #40). Only while something
/// is queued; nothing has gone out, and Undo is a status change. The
/// ambient "Right now" ticker lives in Notifications now.
struct HomeQueuedBanner: View {
    let viewModel: AIActivityViewModel
    @State private var error: String?
    @State private var busy = false
    @State private var showingFeed = false

    /// The sentence, with when it goes out.
    static func line(_ q: AIActivity.Queued) -> String {
        guard let at = q.executeAt, let when = AIActivityFeedSheet.clock(at) else { return q.text }
        return q.text + " \u{00B7} goes out at " + when
    }

    var body: some View {
        if let queued = viewModel.activity?.queued, let q = queued.first {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                HStack(alignment: .center, spacing: CavnarSpace.s) {
                    Image(systemName: "paperplane.fill")
                        .font(.cavnar(.body))
                        .foregroundStyle(Color.cavnarAmber)
                        .accessibilityHidden(true)
                    CavnarMixedText(Self.line(q), role: .label)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button {
                        Haptic.medium()
                        busy = true
                        Task {
                            if let err = await viewModel.cancel(q) {
                                error = err
                                busy = false
                            } else {
                                error = nil
                                busy = false
                                Haptic.success()
                                // The server's sentence for what the undo did
                                // stays a few seconds, then the slot closes.
                                try? await Task.sleep(for: .seconds(6))
                                viewModel.undoneNote = nil
                            }
                        }
                    } label: {
                        Group {
                            if busy {
                                CavnarShimmerText(text: "Undo", color: Color.cavnarEmber2)
                            } else {
                                Text("Undo").cavnarText(.label, color: .cavnarEmber2)
                            }
                        }
                        .padding(.horizontal, CavnarSpace.s)
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .disabled(busy)
                    .accessibilityLabel("Undo: \(q.text)")
                }
                if queued.count > 1 {
                    Button {
                        Haptic.light()
                        showingFeed = true
                    } label: {
                        Text("+\(queued.count - 1) more going out")
                            .cavnarText(.secondary, color: .cavnarEmber2)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }
                if let error {
                    Text(error).cavnarText(.secondary, color: .cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.horizontal, CavnarSpace.m)
            .padding(.vertical, CavnarSpace.xs)
            .background(Color.cavnarAmberBg, in: RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                .strokeBorder(Color.cavnarAmber.opacity(0.6), lineWidth: 1))
            .sheet(isPresented: $showingFeed) {
                AIActivityFeedSheet(viewModel: viewModel)
            }
            .undoWhyDialog(Binding(get: { viewModel.askWhy }, set: { viewModel.askWhy = $0 }))
        } else if let note = viewModel.undoneNote {
            // What the undo did, once, after the banner leaves.
            Text(note).cavnarText(.secondary, color: .cavnarGreen)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
