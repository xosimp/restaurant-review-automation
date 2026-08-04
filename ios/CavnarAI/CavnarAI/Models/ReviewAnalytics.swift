import Foundation

struct ResponsePerformance: Decodable {
    let total: Int
    let days: Int
    let approvedAsIs: Int
    let edited: Int
    let regenerated: Int

    enum CodingKeys: String, CodingKey {
        case total, days
        case approvedAsIs = "approved_as_is"
        case edited, regenerated
    }
}

struct TopicHeatmapEntry: Decodable, Identifiable {
    let category: String
    let label: String
    let count: Int
    let positive: Int
    let negative: Int
    let neutral: Int
    let pctPositive: Int
    let pctNegative: Int
    let trend: String

    var id: String { category }

    enum CodingKeys: String, CodingKey {
        case category, label, count, positive, negative, neutral, trend
        case pctPositive = "pct_positive"
        case pctNegative = "pct_negative"
    }
}

struct SentimentWeek: Decodable, Identifiable {
    let label: String
    let weekKey: String
    let positive: Int
    let negative: Int
    let neutral: Int
    let total: Int
    let avgRating: Double

    var id: String { weekKey }

    enum CodingKeys: String, CodingKey {
        case label
        case weekKey = "week_key"
        case positive, negative, neutral, total
        case avgRating = "avg_rating"
    }
}

struct ResponseTemplate: Codable, Identifiable {
    let id: Int
    let title: String
    let body: String
    let category: String
    let useCount: Int?

    enum CodingKeys: String, CodingKey {
        case id, title, body, category
        case useCount = "use_count"
    }
}
