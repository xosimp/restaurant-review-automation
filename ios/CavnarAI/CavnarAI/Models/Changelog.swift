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

    /// `published_at` as the owner reads a date — "9/21/26" — or nil when
    /// the server sent none or nothing date-shaped.
    var displayDate: String? {
        guard let raw = publishedAt?.trimmingCharacters(in: .whitespaces), raw.count >= 10 else { return nil }
        let out = CavnarDate.mdy(raw)
        return out == raw ? nil : out
    }
}
