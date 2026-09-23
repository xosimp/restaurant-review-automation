import Foundation

/// Structured AI consultant insight — mirrors the web dashboard's
/// intro/Recommendations/Forecast layout (see client_api.py's
/// parse_insight_sections) instead of a raw text blob, so the app can
/// render the same numbered-circle recommendation cards natively.
struct AIInsight: Codable, Equatable {
    let intro: String
    let recommendations: [String]
    let forecast: String?
    /// Each recommendation's rec_ledger key, index for index with
    /// `recommendations`; nil at an index means that line gets no answer
    /// controls. Lines the owner already answered are left out of both
    /// arrays server-side. Optional so an older server, and Labor's cached
    /// copy written before this existed, still decode.
    var recKeys: [String?]? = nil

    enum CodingKeys: String, CodingKey {
        case intro = "insight_intro"
        case recommendations = "insight_recommendations"
        case forecast = "insight_forecast"
        case recKeys = "insight_rec_keys"
    }

    /// The key for the recommendation at `index`, or nil when it has none
    /// (or the arrays disagree in length — never a key for the wrong line).
    func recKey(at index: Int) -> String? {
        guard let recKeys, recKeys.count == recommendations.count,
              recKeys.indices.contains(index) else { return nil }
        guard let key = recKeys[index], !key.isEmpty else { return nil }
        return key
    }
}
