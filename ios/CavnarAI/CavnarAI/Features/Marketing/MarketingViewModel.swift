import Foundation
import Observation

struct MarketingStats: Codable {
    let generated: Int
    let published: Int
    let thisMonth: Int

    enum CodingKeys: String, CodingKey {
        case generated, published
        case thisMonth = "this_month"
    }
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

@Observable
@MainActor
final class MarketingViewModel {
    var stats: MarketingStats?
    var calendar: [ContentCalendarIdea] = []
    var isLoading = false
    var errorMessage: String?

    // Generator
    var selectedType = "instagram_post"
    var topic = ""
    var isGenerating = false
    var generatedContent: String?
    var generateError: String?

    let contentTypes = [
        ("instagram_post", "Instagram Post"),
        ("weekly_email", "Weekly Email"),
        ("google_promo", "Google Promo"),
        ("happy_hour", "Happy Hour"),
        ("loyalty_nudge", "Loyalty Nudge"),
    ]

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct MarketingResponse: Decodable {
        let ok: Bool
        let stats: MarketingStats
        let calendar: [ContentCalendarIdea]
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let response: MarketingResponse = try await client.send("/mobile/api/marketing")
            stats = response.stats
            calendar = response.calendar
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load marketing data."
        }
    }

    private struct GenerateBody: Encodable {
        let type: String
        let topic: String
    }

    private struct GenerateResponse: Decodable {
        let ok: Bool
        let content: String?
        let error: String?
    }

    func generate() async {
        isGenerating = true
        generateError = nil
        generatedContent = nil
        defer { isGenerating = false }
        do {
            let response: GenerateResponse = try await client.send(
                "/mobile/api/marketing/generate-content", method: .post,
                body: GenerateBody(type: selectedType, topic: topic)
            )
            if response.ok {
                generatedContent = response.content
            } else {
                generateError = response.error ?? "Couldn't generate content."
            }
        } catch let error as APIClient.APIError {
            generateError = error.message
        } catch {
            generateError = "Couldn't generate content."
        }
    }
}
