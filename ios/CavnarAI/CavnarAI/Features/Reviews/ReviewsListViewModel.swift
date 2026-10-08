import Foundation
import CryptoKit
import Observation

/// The web inbox's filter chips, on the phone.
enum ReviewInboxFilter: String, CaseIterable, Identifiable {
    case all = "All"
    case urgent = "Urgent"
    case toApprove = "To approve"
    case negative = "Negative"
    case positive = "Positive"
    var id: String { rawValue }

    /// The same filter, as models.get_reviews_data names it. The inbox is
    /// paged, so a chip has to be answered by the server over every review
    /// — filtering the first page client-side said "No urgent reviews"
    /// while review 51 was urgent (CLIENT-30).
    var serverKey: String {
        switch self {
        case .all: return "all"
        case .urgent: return "urgent"
        case .toApprove: return "pending"
        case .negative: return "negative"
        case .positive: return "positive"
        }
    }

    /// A nav path's `?filter=` (nav.py): the server's key, or the words a
    /// card or the web may use for the same chip. Nil for anything else.
    init?(key: String?) {
        switch key?.lowercased().replacingOccurrences(of: "-", with: "_") {
        case "all": self = .all
        case "urgent": self = .urgent
        case "pending", "to_approve", "toapprove", "awaiting", "awaiting_approval", "drafted": self = .toApprove
        case "negative": self = .negative
        case "positive": self = .positive
        default: return nil
        }
    }
}

@Observable
@MainActor
final class ReviewsListViewModel {
    var reviews: [Review] = []
    var isLoading = false
    var isLoadingMore = false
    var errorMessage: String?
    var filter: ReviewInboxFilter = .all
    /// What the owner typed. The server searches the whole inbox for it
    /// (models.get_reviews_data: author or text), the same as the web's
    /// search box — it used to filter only the page already on the phone, so
    /// a review past the first 50 was never found. Debounced: a pause in
    /// typing reloads page one with it.
    var searchText = "" {
        didSet { if searchText != oldValue { scheduleSearch() } }
    }
    private var searchTask: Task<Void, Never>?
    /// The search the rows on screen (and the next page) were loaded with.
    private var loadedSearch = ""
    private var trimmedSearch: String { searchText.trimmingCharacters(in: .whitespacesAndNewlines) }

    private func scheduleSearch() {
        searchTask?.cancel()
        guard inboxOpened, trimmedSearch != loadedSearch else { return }
        searchTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(350))
            guard !Task.isCancelled, let self else { return }
            await self.reload()
        }
    }
    /// The header figures — rating, response rate, urgent, awaiting. The
    /// app modelled all of this in ReviewStats and then never called
    /// /mobile/api/review-stats from anywhere, so the phone's Reviews tab
    /// showed no reputation summary at all while the web showed four pills.
    var stats: ReviewStats?
    /// The real review-fetch state from the first page (DH4-6), in the
    /// restaurant's clock: "Checked 11:02am · next check 4pm", "Last check
    /// 9/21/26 — 6 checks missed". Nil from an older server.
    private(set) var fetchLine: ServerStatusLine?
    /// When a page last loaded — the foreground-refresh policy's clock.
    private(set) var lastLoadedAt: Date?
    /// Paging state. load() used to ask for every review the restaurant had
    /// ever received (filter=all, no limit) and filter client-side.
    private(set) var total = 0
    private(set) var hasMore = false
    private var nextOffset = 0

    /// The chip is applied by the server (load() sends filter=serverKey), and
    /// again here so a row whose status changed on the detail screen leaves
    /// "To approve" at once. Search is the server's too (load() sends it);
    /// the same match here narrows the list while a debounce is pending.
    var filteredReviews: [Review] {
        var out = reviews
        // These must mean the same thing here, in models.get_reviews_data
        // and in the web inbox. They didn't: "To approve" was drafted-only
        // on the phone, "not approved and not posted and not urgent" in the
        // web's JS, and response_status='drafted' on the server — three
        // different sets behind one label. Server definition wins.
        switch filter {
        case .all: break
        case .urgent: out = out.filter(\.isUrgent)
        case .toApprove: out = out.filter(\.isInQueue)
        case .negative: out = out.filter { $0.sentiment == "negative" }
        case .positive: out = out.filter { $0.sentiment == "positive" }
        }
        let q = trimmedSearch.lowercased()
        if !q.isEmpty {
            // The server's fields (author OR text), so the list never shows
            // a match here the next page's server search would not.
            out = out.filter {
                ($0.text ?? "").lowercased().contains(q)
                    || ($0.author ?? "").lowercased().contains(q)
            }
        }
        return out
    }

    /// A chip's count, or nil when it isn't known. Rows on the phone can be
    /// counted only once every row of the loaded set is here; until then the
    /// active chip shows the server's own total and the others show nothing
    /// — counting a first page read as "0 urgent" (CLIENT-30).
    func count(for filter: ReviewInboxFilter) -> Int? {
        if filter == self.filter { return hasMore ? total : localCount(filter) }
        guard self.filter == .all, !hasMore else { return nil }
        return localCount(filter)
    }

    private func localCount(_ filter: ReviewInboxFilter) -> Int {
        switch filter {
        case .all: return reviews.count
        case .urgent: return reviews.filter(\.isUrgent).count
        case .toApprove: return reviews.filter(\.isInQueue).count
        case .negative: return reviews.filter { $0.sentiment == "negative" }.count
        case .positive: return reviews.filter { $0.sentiment == "positive" }.count
        }
    }

    func remove(reviewID: Int) {
        reviews.removeAll { $0.id == reviewID }
    }

    // MARK: - Queue mode (friction audit #21)

    /// The first open: the link's filter when one was given, else "To
    /// approve" whenever replies are waiting (the stats say how many), else
    /// All. Set before the first page loads, so it loads once.
    func openInbox(preferred: ReviewInboxFilter?) async {
        defer { inboxOpened = true }
        // Only the first open chooses: coming back from a review keeps the
        // chip the owner picked.
        if !inboxOpened {
            if let preferred {
                filter = preferred
            } else {
                await loadStats()
                if (stats?.awaitingApproval ?? 0) > 0 { filter = .toApprove }
            }
        }
        await load()
    }

    /// Set once the first open has chosen its filter and loaded; a filter
    /// change before that is the open's own, not a chip tap to reload for.
    private(set) var inboxOpened = false

    /// The next reply waiting after `id`, in the order the list shows them —
    /// what "Approve & next" moves on to. Nil at the end of the queue.
    func nextInQueue(after id: Int) -> Review? {
        let queue = filteredReviews.filter { $0.responseStatus == "drafted" && !($0.draftResponse ?? "").isEmpty }
        guard let i = queue.firstIndex(where: { $0.id == id }) else {
            return queue.first { $0.id != id }
        }
        return queue.dropFirst(i + 1).first
    }

    /// A reply that may be approved without opening it: the bar Home's
    /// "Publish N replies" holds (models.BULK_PUBLISHABLE_SQL) — drafted,
    /// not flagged for a read, not urgent.
    static func canQuickApprove(_ review: Review) -> Bool {
        canQuickApprove(review, now: Date())
    }

    /// The bar's recency half too (F3-14): a reply more than
    /// `quickApproveMaxAgeDays` old is not one the bulk publish may post,
    /// so it is not one a swipe may post either. A review with no date of its
    /// own is judged by when it was fetched on the server; here it passes.
    static func canQuickApprove(_ review: Review, now: Date) -> Bool {
        review.responseStatus == "drafted"
            && !(review.draftResponse ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !review.draftIsFlagged
            && !review.isUrgent
            && isRecentEnoughForBulk(review.reviewDate, now: now)
    }

    /// thresholds.REPLY_OWED_MAX_AGE_DAYS — the window BULK_PUBLISHABLE_SQL
    /// holds a publish-without-reading to.
    static let quickApproveMaxAgeDays = 30

    static func isRecentEnoughForBulk(_ reviewDate: String?, now: Date) -> Bool {
        guard let raw = reviewDate else { return true }
        let p = raw.prefix(10).split(separator: "-")
        guard p.count == 3, let y = Int(p[0]), let m = Int(p[1]), let d = Int(p[2]) else { return true }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        guard let day = cal.date(from: DateComponents(year: y, month: m, day: d)),
              let cutoff = cal.date(byAdding: .day, value: -quickApproveMaxAgeDays, to: cal.startOfDay(for: now))
        else { return true }
        return day >= cutoff
    }

    private let client: APIClient
    /// Bumped by every load(). A page requested under an older generation
    /// (loadMore in flight when a pull-to-refresh or a chip replaced the
    /// list) belongs to the old paging and is dropped, not appended
    /// (CLIENT-30).
    private var generation = 0
    private var draftObserver: NotificationToken?

    init(client: APIClient = .shared) {
        self.client = client
        // A draft written or edited on the detail screen updates this list's
        // copy of the review. The detail screen is built from that copy, so
        // without this, reopening the same review offered "Write a reply"
        // again — a second paid draft for one the owner already had
        // (CLIENT-56).
        draftObserver = NotificationToken(NotificationCenter.default.addObserver(
            forName: ReviewDetailViewModel.draftDidChange, object: nil, queue: nil
        ) { [weak self] note in
            guard let id = note.userInfo?["id"] as? Int, let draft = note.userInfo?["draft"] as? String else { return }
            // Posted only from ReviewDetailViewModel, on the main actor.
            MainActor.assumeIsolated { self?.applyDraft(draft, toReview: id) }
        })
    }

    private func applyDraft(_ draft: String, toReview id: Int) {
        guard let index = reviews.firstIndex(where: { $0.id == id }) else { return }
        reviews[index] = reviews[index].withDraft(draft)
    }

    private struct ReviewsResponse: Decodable {
        let ok: Bool
        let reviews: [Review]
        let total: Int?
        let offset: Int?
        let hasMore: Bool?
        var fetchLine: LenientStatusLine? = nil

        enum CodingKeys: String, CodingKey {
            case ok, reviews, total, offset
            case hasMore = "has_more"
            case fetchLine = "fetch_line"
        }
    }

    /// One page. Matches models.REVIEWS_PAGE_SIZE.
    private static let pageSize = 50

    /// When the cached first page on screen was stored; nil once a live
    /// load lands.
    private(set) var cachedAt: Date?
    var stalenessNotice: String? { CacheFreshness.notice(savedAt: cachedAt) }

    /// A cached first page, shown as it was stored — paging state included,
    /// so the chips' counts read exactly as they did live (CLIENT-30).
    private func apply(_ cached: ReviewsResponse) {
        reviews = cached.reviews
        total = cached.total ?? cached.reviews.count
        nextOffset = cached.offset ?? cached.reviews.count
        hasMore = cached.hasMore ?? false
        fetchLine = cached.fetchLine?.value
    }

    /// category filters to reviews tagged with that topic-heatmap category
    /// (see TopicHeatmapEntry.category); platform filters to one review
    /// platform (e.g. "google"/"yelp"). Both nil/omitted keeps the normal
    /// unfiltered inbox behavior.
    func load(category: String? = nil, platform: String? = nil) async {
        generation += 1
        let mine = generation
        let session = SessionScope.generation
        // The inbox's first page per chip is kept on the device
        // (ResponseCache) and painted before the fetch; a category or
        // platform drill-down is not — it is a one-off look, not the inbox.
        // A search is a one-off look too: only the plain inbox is cached.
        let cache = (category == nil && platform == nil && trimmedSearch.isEmpty) ? ResponseCache<ReviewsResponse>(
            "reviews.inbox.\(filter.serverKey)") : nil
        if reviews.isEmpty, let hit = await cache?.load(), mine == generation {
            apply(hit.value)
            cachedAt = hit.savedAt
        }
        isLoading = true
        errorMessage = nil
        defer { if mine == generation { isLoading = false } }
        do {
            var query = ["filter": filter.serverKey, "limit": "\(Self.pageSize)", "offset": "0"]
            if let category { query["category"] = category }
            if let platform { query["platform"] = platform }
            let search = trimmedSearch
            if !search.isEmpty { query["search"] = search }
            let fetched: (value: ReviewsResponse, body: Data) = try await client.sendKeepingBody(
                "/mobile/api/reviews", query: query)
            // A newer load (another chip, a refresh) owns the list now.
            guard mine == generation else { return }
            loadedSearch = search
            cache?.save(fetched.body, generation: session)
            cachedAt = nil
            let response = fetched.value
            reviews = response.reviews
            total = response.total ?? response.reviews.count
            nextOffset = response.offset ?? response.reviews.count
            hasMore = response.hasMore ?? false
            fetchLine = response.fetchLine?.value
            lastLoadedAt = Date()
            loadCategory = category
            loadPlatform = platform
            await loadStats()
        } catch is CancellationError {
            // The screen went away mid-load (CLIENT-49) — not a failure.
        } catch let error as APIClient.APIError {
            if mine == generation { errorMessage = error.message }
        } catch is APIClient.SessionExpiredError {
            // Handled globally by SessionStore.
        } catch {
            if mine == generation { errorMessage = "Couldn't load reviews." }
        }
    }

    /// load() again with whatever category/platform the list was opened
    /// with — a chip change or a Retry.
    func reload() async {
        await load(category: loadCategory, platform: loadPlatform)
    }

    private var loadCategory: String?
    private var loadPlatform: String?

    /// The next page, appended. Called when the last row appears.
    func loadMore() async {
        guard hasMore, !isLoadingMore, !isLoading else { return }
        let mine = generation
        isLoadingMore = true
        defer { isLoadingMore = false }
        var query = ["filter": filter.serverKey, "limit": "\(Self.pageSize)", "offset": "\(nextOffset)"]
        if let loadCategory { query["category"] = loadCategory }
        if let loadPlatform { query["platform"] = loadPlatform }
        if !loadedSearch.isEmpty { query["search"] = loadedSearch }
        do {
            let response: ReviewsResponse = try await client.send(
                "/mobile/api/reviews", query: query, hapticOnError: false
            )
            guard mine == generation else { return }
            let known = Set(reviews.map(\.id))
            reviews.append(contentsOf: response.reviews.filter { !known.contains($0.id) })
            total = response.total ?? total
            nextOffset = response.offset ?? (nextOffset + response.reviews.count)
            hasMore = response.hasMore ?? false
        } catch {
            // A failed page is not a failed screen — the rows already on
            // screen stay, and the next scroll retries.
            if mine == generation { hasMore = true }
        }
    }

    /// The header figures. Separate from the list so a paging request
    /// doesn't re-fetch them.
    func loadStats() async {
        async let s: ReviewStats? = try? client.send("/mobile/api/review-stats", hapticOnError: false)
        // The why line's server half (density #32): the rating's move and
        // the stored top complaint. Nil on an older server — the line then
        // reads from the reviews on the phone alone.
        async let w: ReviewsWhyPayload? = try? client.send("/mobile/api/reviews/why-line", hapticOnError: false)
        let (stats, why) = await (s, w)
        self.stats = stats
        self.why = why
    }

    /// GET /mobile/api/reviews/why-line, when the server has it.
    var why: ReviewsWhyPayload?

    /// Called after a detail screen completes an approve/skip so the list
    /// reflects the new status in place — load() fetches every review
    /// (filter=all), not just an actionable queue, so a completed review
    /// should stay visible with its updated status pill, not disappear.
    func markCompleted(reviewID: Int, status: String) {
        if status == "deleted" { remove(reviewID: reviewID); selection.remove(reviewID); return }
        guard let index = reviews.firstIndex(where: { $0.id == reviewID }) else { return }
        reviews[index] = reviews[index].withStatus(status)
    }

    // MARK: - Multi-select (parity audit 10/7/26 #34, the web desk's #2)

    /// The reviews ticked in Select mode.
    var selection: Set<Int> = []
    /// True while a bulk approve or skip is running.
    private(set) var isBulkWorking = false
    /// What the last bulk action did — "3 approved (2 posted to Google) ·
    /// 1 held for you to read" — and whether any of it didn't go through.
    var bulkNote: (text: String, failed: Bool)?

    /// A card the web offers a box on: one still waiting on a decision.
    static func isSelectable(_ review: Review) -> Bool {
        !["posted", "approved", "pending-sync"].contains(review.responseStatus)
    }

    /// Skip applies to what is still in the queue (not answered, not
    /// already skipped) — the web's rvSelSkippable.
    static func isSkippable(_ review: Review) -> Bool {
        !["posted", "approved", "skipped", "pending-sync"].contains(review.responseStatus)
    }

    private var selectedReviews: [Review] { reviews.filter { selection.contains($0.id) } }
    /// The selected replies the bulk route may post: the swipe's own bar
    /// (canQuickApprove — drafted, not flagged, not urgent, recent).
    var bulkApprovable: [Int] { bulkApprovableReviews.map(\.id) }
    /// The same replies themselves — the confirm lists their words (NS5
    /// M10, re-audit 10/8/26: "words, not a count").
    var bulkApprovableReviews: [Review] { selectedReviews.filter { Self.canQuickApprove($0) } }
    var bulkSkippable: [Int] { selectedReviews.filter { Self.isSkippable($0) }.map(\.id) }
    /// Selected but not ready to post in bulk — read one at a time.
    var bulkHeld: Int { selection.count - bulkApprovable.count }

    func toggleSelection(_ review: Review) {
        guard Self.isSelectable(review) else { return }
        if selection.contains(review.id) { selection.remove(review.id) } else { selection.insert(review.id) }
    }

    struct BulkApproveBody: Encodable {
        let reviewIds: [Int]
        let limit: Int
        /// {id: models.draft_hash} of the words the confirm listed: a reply
        /// rewritten since is not posted (re-audit 10/8/26).
        var reviewHashes: [String: String]? = nil
        enum CodingKeys: String, CodingKey {
            case reviewIds = "review_ids"
            case limit
            case reviewHashes = "review_hashes"
        }
    }

    /// models.draft_hash: sha256 hex of the reply stripped.
    nonisolated static func draftHash(_ text: String) -> String {
        let data = Data(text.trimmingCharacters(in: .whitespacesAndNewlines).utf8)
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    /// The approve-all route pinned to these replies, 25 at a time (its
    /// cap), each bound to the words the confirm showed — the server
    /// re-checks each and never posts a flagged, urgent or rewritten draft.
    static func bulkApproveBodies(_ reviews: [Review]) -> [BulkApproveBody] {
        stride(from: 0, to: reviews.count, by: 25).map { start in
            let chunk = Array(reviews[start..<min(start + 25, reviews.count)])
            var hashes: [String: String] = [:]
            for r in chunk { hashes[String(r.id)] = draftHash(r.draftResponse ?? "") }
            return BulkApproveBody(reviewIds: chunk.map(\.id), limit: chunk.count, reviewHashes: hashes)
        }
    }

    /// The bar's Approve, after its confirm: exactly the replies the
    /// confirm listed (`shown`), in the words it showed.
    func bulkApprove(shown: [Review]) async {
        guard !shown.isEmpty, !isBulkWorking else { return }
        isBulkWorking = true
        defer { isBulkWorking = false }
        // Selected but never in the bulk (flagged, urgent, old, already
        // decided) — held for a read just as much as one the server held.
        let notInBulk = max(0, selection.count - shown.count)
        var approved = 0, posted = 0, failed = 0, changed = 0
        var lost = false
        for body in Self.bulkApproveBodies(shown) {
            do {
                let r: BulkPublishResult = try await client.send(
                    "/mobile/api/reviews/approve-all", method: .post, body: body, retryTransient: false)
                approved += r.approved
                posted += r.posted
                failed += r.failed
                changed += r.changed ?? 0
            } catch let error as APIClient.APIError {
                failed += body.reviewIds.count
                if error.mayHaveReachedServer { lost = true }
            } catch {
                failed += body.reviewIds.count
                lost = true
            }
        }
        let held = notInBulk + max(0, shown.count - approved - failed - changed)
        var bits = ["\(approved) approved" + (posted > 0 ? " (\(posted) posted to Google)" : "")]
        if held > 0 { bits.append("\(held) held for you to read") }
        if changed > 0 { bits.append("\(changed) changed since you read \(changed == 1 ? "it" : "them") \u{2014} not posted") }
        if failed > 0 { bits.append("\(failed) didn\u{2019}t go through" + (lost ? " \u{2014} check before trying again" : "")) }
        bulkNote = (bits.joined(separator: " \u{00B7} "), failed > 0)
        if failed == 0 { Haptic.success() }
        selection.removeAll()
        await reload()
    }

    /// The bar's Skip, after its confirm: the single skip route in turn.
    func bulkSkip() async {
        let ids = bulkSkippable
        guard !ids.isEmpty, !isBulkWorking else { return }
        isBulkWorking = true
        defer { isBulkWorking = false }
        var skipped = 0, bad = 0
        for id in ids {
            do {
                let _: APIClient.EmptyResponse = try await client.send(
                    "/mobile/api/reviews/\(id)/skip", method: .post, hapticOnError: false)
                skipped += 1
                markCompleted(reviewID: id, status: "skipped")
            } catch {
                bad += 1
            }
        }
        bulkNote = ("\(skipped) skipped" + (bad > 0 ? " \u{00B7} \(bad) didn\u{2019}t go through" : ""), bad > 0)
        if bad == 0 { Haptic.light() }
        selection.removeAll()
        await loadStats()
    }
}

/// Removes a block-based NotificationCenter observer when its owner goes away.
final class NotificationToken {
    private let token: NSObjectProtocol
    init(_ token: NSObjectProtocol) { self.token = token }
    deinit { NotificationCenter.default.removeObserver(token) }
}
