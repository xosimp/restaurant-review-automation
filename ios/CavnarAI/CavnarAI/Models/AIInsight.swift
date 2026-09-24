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
    /// False when a figure in the read could not be traced to the data the
    /// model was given; the screen then carries CavnarCaveat.unverifiedFigures
    /// and the server offers no answer controls (M-16). Optional: absent on
    /// modules that don't send it, and on older cached copies.
    var figuresVerified: Bool? = nil
    var unsupportedFigures: [String]? = nil
    /// H2: false when the read states a cause no stored diagnosis backs —
    /// the screen carries CavnarCaveat.unverifiedCauses. H8: the forecast
    /// computed in Python (`forecast`, an object — not `insight_forecast`,
    /// the model's text line). Optional: absent on the modules that don't
    /// send them and on older cached copies.
    var causesVerified: Bool? = nil
    var unsupportedCauses: [String]? = nil
    var computedForecast: ComputedForecast? = nil

    enum CodingKeys: String, CodingKey {
        case intro = "insight_intro"
        case recommendations = "insight_recommendations"
        case forecast = "insight_forecast"
        case recKeys = "insight_rec_keys"
        case figuresVerified = "figures_verified"
        case unsupportedFigures = "unsupported_figures"
        case causesVerified = "causes_verified"
        case unsupportedCauses = "unsupported_causes"
        case computedForecast = "forecast"
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
