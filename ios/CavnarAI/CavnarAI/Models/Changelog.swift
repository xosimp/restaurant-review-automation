import Foundation

struct ChangelogEntry: Decodable, Identifiable {
    let id: Int
    let title: String
    let body: String?
    let tag: String?
    let publishedAt: String?

    enum CodingKeys: String, CodingKey {
        case id, title, body, tag
        case publishedAt = "published_at"
    }
}
