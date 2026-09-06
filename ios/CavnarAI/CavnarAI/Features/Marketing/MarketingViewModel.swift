import Foundation
import Observation
import UIKit

struct MarketingStats: Codable {
    let generated: Int
    let published: Int
    let thisMonth: Int

    enum CodingKeys: String, CodingKey {
        case generated, published
        case thisMonth = "this_month"
    }
}

/// Served by the backend rather than hardcoded here. The app carried its own
/// list of five, missing `event_announcement` entirely and renaming three of
/// the others ("Loyalty Nudge" for what the product calls a re-engagement
/// text), so the two platforms disagreed about what Cavnar AI can even write.
struct MarketingContentType: Decodable, Identifiable, Hashable {
    let id: String
    let label: String
    let description: String

    /// Platform ceilings, used for the live counter under the editor. The web
    /// tab has always shown these; the app let an owner send a 3,000-character
    /// caption to Instagram and find out from Meta.
    var characterLimit: Int? {
        switch id {
        case "instagram_post", "happy_hour", "event_announcement": return 2_200
        case "google_promo": return 1_500
        case "loyalty_nudge": return 160
        default: return nil
        }
    }

    var limitLabel: String {
        switch id {
        case "google_promo": return "Google post limit"
        case "loyalty_nudge": return "SMS limit"
        default: return "Instagram limit"
        }
    }
}

/// Where this restaurant can actually publish right now.
struct MarketingChannels: Decodable {
    var instagram = false
    var facebook = false
    var google = false

    var none: Bool { !instagram && !facebook && !google }

    init(instagram: Bool = false, facebook: Bool = false, google: Bool = false) {
        self.instagram = instagram
        self.facebook = facebook
        self.google = google
    }

    /// Decoded key by key so an older build of the backend, which sends no
    /// channels at all, degrades to "nothing connected" rather than failing
    /// the whole Marketing payload.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        instagram = try c.decodeIfPresent(Bool.self, forKey: .instagram) ?? false
        facebook = try c.decodeIfPresent(Bool.self, forKey: .facebook) ?? false
        google = try c.decodeIfPresent(Bool.self, forKey: .google) ?? false
    }

    enum CodingKeys: String, CodingKey { case instagram, facebook, google }
}

struct ContentCalendarIdea: Codable, Identifiable {
    let day: String
    let date: String?
    let platform: String
    let angle: String
    let type: String

    enum CodingKeys: String, CodingKey {
        case day, date, platform, angle, type
    }

    var id: String { "\(day)-\(type)" }
}

/// Google Business posts carry an optional action button, and it is the half
/// of the post that converts. `create_local_post` has always accepted one —
/// the app just never offered it.
enum GoogleCallToAction: String, CaseIterable, Identifiable {
    case none = ""
    case learnMore = "LEARN_MORE"
    case order = "ORDER"
    case book = "BOOK"
    case shop = "SHOP"
    case signUp = "SIGN_UP"
    case call = "CALL"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .none: return "No button"
        case .learnMore: return "Learn more"
        case .order: return "Order online"
        case .book: return "Book"
        case .shop: return "Shop"
        case .signUp: return "Sign up"
        case .call: return "Call now"
        }
    }

    /// CALL uses the listing's own number and Google rejects a url alongside it.
    var needsLink: Bool { self != .none && self != .call }
}

@Observable
@MainActor
final class MarketingViewModel {
    var stats: MarketingStats?
    var calendar: [ContentCalendarIdea] = []
    var channels = MarketingChannels()
    var isLoading = false
    var errorMessage: String?

    // Generator
    var selectedType = "instagram_post"
    var topic = ""
    var isGenerating = false
    /// The draft itself, editable. It used to be rendered as read-only `Text`
    /// while a one-tap Post button sat under it — and the prompts for three of
    /// the six types ask Claude for TWO versions, so tapping Post published
    /// "Option 1 (Short & Punchy): …" and both drafts, verbatim, with no way
    /// to trim it anywhere in the app.
    var draft = ""
    var hasDraft = false
    var generateError: String?
    private var lastGeneratedTopic = ""

    // Social posting. The image is no longer a URL the owner types — it's a
    // photo they picked, uploaded by MarketingComposeViewModel, whose public
    // URL is handed in at post time.
    var googleCTA: GoogleCallToAction = .none
    var googleCTALink = ""
    var isPosting = false
    var postError: String?
    var postedPlatform: String?

    // Calendar
    var isGeneratingCalendar = false
    var calendarError: String?

    /// Fallback only — the live list arrives with /marketing. Kept so the
    /// generator still works if that call fails.
    static let fallbackContentTypes = [
        MarketingContentType(id: "instagram_post", label: "Instagram/FB post",
                             description: "Caption + hashtags for a food or ambiance photo"),
        MarketingContentType(id: "weekly_email", label: "Weekly email",
                             description: "Short newsletter to regulars — specials, events, updates"),
        MarketingContentType(id: "google_promo", label: "Google post",
                             description: "Short promotional post for Google Business Profile"),
        MarketingContentType(id: "loyalty_nudge", label: "Re-engagement text",
                             description: "SMS to guests who haven't visited in 3+ weeks"),
        MarketingContentType(id: "happy_hour", label: "Happy hour promo",
                             description: "Social post driving traffic to Mon-Thu 4-6pm deals"),
        MarketingContentType(id: "event_announcement", label: "Event announcement",
                             description: "Post announcing a special dinner, wine night, or seasonal menu"),
    ]
    var contentTypes: [MarketingContentType] = MarketingViewModel.fallbackContentTypes

    /// How long to wait on a model. Generating a post or a week of calendar
    /// ideas takes several seconds and occasionally much longer; the
    /// session-wide 20s is tuned for ordinary reads and was cutting these off
    /// mid-flight, which surfaced as "the connection dropped" for work that
    /// was in fact completing.
    static let generationTimeout: TimeInterval = 90

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    // MARK: - Derived

    var selectedContentType: MarketingContentType? {
        contentTypes.first { $0.id == selectedType }
    }

    var selectedTypeLabel: String { selectedContentType?.label ?? "Content" }

    var characterLimit: Int? { selectedContentType?.characterLimit }

    var isOverLimit: Bool {
        guard let limit = characterLimit else { return false }
        return draft.count > limit
    }

    /// A Google Promo belongs on the Google listing, not on Instagram — so
    /// that one type swaps the destinations rather than adding a third button.
    var isGooglePost: Bool { selectedType == "google_promo" }

    var canPostSomewhere: Bool {
        isGooglePost ? channels.google : (channels.instagram || channels.facebook)
    }

    // MARK: - Load

    private struct MarketingResponse: Decodable {
        let ok: Bool
        let stats: MarketingStats
        let calendar: [ContentCalendarIdea]
        let contentTypes: [MarketingContentType]?
        let channels: MarketingChannels?

        enum CodingKeys: String, CodingKey {
            case ok, stats, calendar, channels
            case contentTypes = "content_types"
        }
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let response: MarketingResponse = try await client.send("/mobile/api/marketing")
            stats = response.stats
            calendar = response.calendar
            channels = response.channels ?? MarketingChannels()
            if let types = response.contentTypes, !types.isEmpty {
                contentTypes = types
                if !types.contains(where: { $0.id == selectedType }) {
                    selectedType = types[0].id
                }
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load marketing data."
        }
    }

    // MARK: - Generate

    private struct GenerateBody: Encodable {
        let type: String
        let topic: String
        let fromCalendar: Bool

        enum CodingKeys: String, CodingKey {
            case type, topic
            case fromCalendar = "from_calendar"
        }
    }

    private struct GenerateResponse: Decodable {
        let ok: Bool
        let content: String?
        let error: String?
    }

    func generate(fromCalendar: Bool = false) async {
        isGenerating = true
        generateError = nil
        postedPlatform = nil
        postError = nil
        draft = ""
        hasDraft = false
        defer { isGenerating = false }
        let requestedTopic = topic
        do {
            let response: GenerateResponse = try await client.send(
                "/mobile/api/marketing/generate-content", method: .post,
                body: GenerateBody(type: selectedType, topic: requestedTopic, fromCalendar: fromCalendar),
                // Writing a post is a Sonnet call — comfortably past the 20s
                // the session uses for ordinary reads, which was cancelling
                // work the server went on to finish anyway.
                timeout: MarketingViewModel.generationTimeout
            )
            if response.ok, let content = response.content {
                draft = content
                hasDraft = true
                lastGeneratedTopic = requestedTopic
            } else {
                generateError = response.error ?? "Couldn't generate content."
            }
        } catch let error as APIClient.APIError {
            generateError = error.message
        } catch {
            generateError = "Couldn't generate content."
        }
    }

    /// Load a calendar idea into the generator and write it, which also tells
    /// the backend the idea was used so next week's calendar doesn't repeat it.
    func generate(from idea: ContentCalendarIdea) async {
        selectedType = idea.type
        topic = idea.angle
        await generate(fromCalendar: true)
    }

    func copyDraft() {
        UIPasteboard.general.string = draft
        Haptic.success()
    }

    // MARK: - Calendar

    private struct CalendarResponse: Decodable {
        let ok: Bool
        let calendar: [ContentCalendarIdea]?
        let error: String?
    }

    /// Generating a week is an explicit act now. It used to happen on every
    /// single load of this tab, which paid for a Sonnet call each time and
    /// handed back a different "this week" on every open.
    func generateCalendar() async {
        isGeneratingCalendar = true
        calendarError = nil
        defer { isGeneratingCalendar = false }
        do {
            let response: CalendarResponse = try await client.send(
                "/mobile/api/marketing/calendar", method: .post,
                // A week of ideas is the longest call this app makes.
                timeout: MarketingViewModel.generationTimeout
            )
            if response.ok, let ideas = response.calendar, !ideas.isEmpty {
                calendar = ideas
            } else {
                calendarError = response.error ?? "Couldn't build a calendar right now."
            }
        } catch let error as APIClient.APIError {
            calendarError = error.message
        } catch {
            calendarError = "Couldn't build a calendar right now."
        }
    }

    /// The web tab's "Download CSV", as something a phone can actually do with
    /// a file — the same four columns, handed to the share sheet.
    var calendarCSV: String {
        var rows = ["Day,Date,Platform,Content Idea,Type"]
        for idea in calendar {
            let cells = [idea.day, idea.date ?? "", idea.platform, idea.angle, idea.type]
            rows.append(cells.map { "\"\($0.replacingOccurrences(of: "\"", with: "\"\""))\"" }
                .joined(separator: ","))
        }
        return rows.joined(separator: "\n")
    }

    // MARK: - Publish

    private struct PostBody: Encodable {
        let caption: String
        let imageUrl: String?
        let topic: String

        enum CodingKeys: String, CodingKey {
            case caption
            case imageUrl = "image_url"
            case topic
        }
    }

    private struct PostResponse: Decodable {
        let ok: Bool
        let postId: String?
        let error: String?

        enum CodingKeys: String, CodingKey {
            case ok, error
            case postId = "post_id"
        }
    }

    private func publish(_ path: String, body: any Encodable, platform: String) async {
        isPosting = true
        postError = nil
        defer { isPosting = false }
        do {
            let response: PostResponse = try await client.send(path, method: .post, body: body)
            if response.ok {
                postedPlatform = platform
                Haptic.success()
            } else {
                postError = response.error ?? "Couldn't post to \(platform)."
            }
        } catch let error as APIClient.APIError {
            postError = error.message
        } catch {
            postError = "Couldn't post to \(platform)."
        }
    }

    func postToInstagram(imageURL: String?) async {
        guard hasDraft else { return }
        let url = (imageURL ?? "").trimmingCharacters(in: .whitespaces)
        guard !url.isEmpty else {
            postError = "Instagram needs a photo — add one above first."
            return
        }
        await publish("/mobile/api/marketing/post-to-instagram",
                      body: PostBody(caption: draft, imageUrl: url, topic: lastGeneratedTopic),
                      platform: "Instagram")
    }

    func postToFacebook() async {
        guard hasDraft else { return }
        await publish("/mobile/api/marketing/post-to-facebook",
                      body: PostBody(caption: draft, imageUrl: nil, topic: lastGeneratedTopic),
                      platform: "Facebook")
    }

    private struct GooglePostBody: Encodable {
        let summary: String
        let ctaType: String
        let ctaUrl: String

        enum CodingKeys: String, CodingKey {
            case summary
            case ctaType = "cta_type"
            case ctaUrl = "cta_url"
        }
    }

    /// Publishes the generated Google Promo copy to the connected Google
    /// Business Profile listing — the step this content type was always
    /// written for but never had.
    func postToGoogle() async {
        guard hasDraft else { return }
        let link = googleCTALink.trimmingCharacters(in: .whitespaces)
        if googleCTA.needsLink && link.isEmpty {
            postError = "That button needs a link."
            return
        }
        if googleCTA == .call && !link.isEmpty {
            postError = "A Call button uses your listing's own number — clear the link."
            return
        }
        await publish("/mobile/api/marketing/google-post",
                      body: GooglePostBody(summary: draft, ctaType: googleCTA.rawValue,
                                           ctaUrl: googleCTA.needsLink ? link : ""),
                      platform: "Google")
    }
}
