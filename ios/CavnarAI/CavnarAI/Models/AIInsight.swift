import Foundation

/// Structured AI consultant insight — mirrors the web dashboard's
/// intro/Recommendations/Forecast layout (see client_api.py's
/// parse_insight_sections) instead of a raw text blob, so the app can
/// render the same numbered-circle recommendation cards natively.
struct AIInsight: Decodable, Equatable {
    let intro: String
    let recommendations: [String]
    let forecast: String?

    enum CodingKeys: String, CodingKey {
        case intro = "insight_intro"
        case recommendations = "insight_recommendations"
        case forecast = "insight_forecast"
    }
}
