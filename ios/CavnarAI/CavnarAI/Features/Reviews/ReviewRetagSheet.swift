import SwiftUI

/// "Fix tags" — correct how Cavnar AI tagged one review, in the analyser's
/// own vocabulary: what it is about (up to three topics), how it reads, how
/// serious it is, and the dishes it names (memory round, 9/29/26: POST
/// /mobile/api/reviews/<id>/retag {categories?, sentiment?, severity?,
/// dishes?} → {changed, review}). Only what changed is sent; the review
/// keeps the correction through a re-analysis, and the analyser reads the
/// restaurant's recent corrections as examples.
struct ReviewRetagSheet: View {
    let review: Review
    /// The tags as they stand now — the server's answer after a save, else
    /// the review's own.
    let current: ReviewTags
    var onSaved: (ReviewTags) -> Void = { _ in }

    @Environment(\.dismiss) private var dismiss
    @State private var categories: Set<String> = []
    @State private var sentiment = ""
    @State private var severity = ""
    @State private var dishes = ""
    @State private var busy = false
    @State private var error: String?
    @State private var seeded = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("Cavnar AI tags every review so its trends group like with like. Fix one here and the next reads follow how you tag.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    AccountSection(kicker: "What it\u{2019}s about \u{00B7} up to three") {
                        AccountFlowLayout(spacing: 8, lineSpacing: 8) {
                            ForEach(ReviewTagVocabulary.categories, id: \.key) { item in
                                choice(item.label, on: categories.contains(item.key)) { toggle(item.key) }
                            }
                        }
                        .padding(.vertical, 12)
                    }

                    AccountSection(kicker: "How it reads") {
                        AccountFlowLayout(spacing: 8, lineSpacing: 8) {
                            ForEach(ReviewTagVocabulary.sentiments, id: \.self) { s in
                                choice(s.capitalized, on: sentiment == s) { sentiment = s }
                            }
                        }
                        .padding(.vertical, 12)
                    }

                    AccountSection(kicker: "How serious") {
                        AccountFlowLayout(spacing: 8, lineSpacing: 8) {
                            ForEach(ReviewTagVocabulary.severities, id: \.key) { item in
                                choice(item.label, on: severity == item.key) { severity = item.key }
                            }
                        }
                        .padding(.vertical, 12)
                    }

                    AccountSection(kicker: "Dishes it names") {
                        VStack(alignment: .leading, spacing: 6) {
                            TextField("e.g. margherita, tiramisu", text: $dishes)
                                .cavnarTextFieldStyle()
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                            Text("Up to three, separated by commas \u{2014} the guest\u{2019}s own words are kept beside them.")
                                .cavnarText(.caption)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.vertical, 12)
                    }

                    if let error {
                        Text(error)
                            .cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    Button {
                        Haptic.light()
                        Task { await save() }
                    } label: {
                        Group {
                            if busy { CavnarShimmerText(text: "Saving\u{2026}") } else { Text(changes.isEmpty ? "Nothing changed" : "Save tags") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: busy || changes.isEmpty))
                    .disabled(busy || changes.isEmpty)
                }
                .padding(20)
            }
            .accountSheetChrome("Fix tags")
        }
        .onAppear {
            guard !seeded else { return }
            seeded = true
            categories = Set(current.categories)
            sentiment = current.sentiment ?? ""
            severity = current.severity ?? ""
            dishes = current.dishes.joined(separator: ", ")
        }
    }

    private func toggle(_ key: String) {
        Haptic.selection()
        if categories.contains(key) {
            categories.remove(key)
        } else if categories.count < 3 {
            categories.insert(key)
        } else {
            error = "Three topics at most \u{2014} take one off first."
            return
        }
        error = nil
    }

    private func choice(_ label: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.selection()
            action()
        } label: {
            Text(label)
                .font(.cavnarBody(CavnarType.secondary, weight: on ? 700 : 400))
                .foregroundStyle(on ? Color.cavnarEmber2 : Color.cavnarInk2)
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
                .background(Capsule().fill(on ? Color.cavnarEmber.opacity(0.16) : Color.white.opacity(0.04)))
                .overlay(Capsule().strokeBorder(on ? Color.cavnarEmber.opacity(0.45) : Color.white.opacity(0.08),
                                                lineWidth: 1))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    private var dishList: [String] {
        dishes.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces).lowercased() }
            .filter { !$0.isEmpty }
    }

    /// The body the route takes; a nil field is left out (not changed).
    struct RetagBody: Encodable, Equatable {
        var categories: [String]? = nil
        var sentiment: String? = nil
        var severity: String? = nil
        var dishes: [String]? = nil
        var isEmpty: Bool { categories == nil && sentiment == nil && severity == nil && dishes == nil }
    }

    /// Only what differs from the tags now.
    var changes: RetagBody {
        var out = RetagBody()
        if !categories.isEmpty, categories != Set(current.categories) {
            out.categories = ReviewTagVocabulary.categories.map(\.key).filter { categories.contains($0) }
        }
        if !sentiment.isEmpty, sentiment != (current.sentiment ?? "") { out.sentiment = sentiment }
        if !severity.isEmpty, severity != (current.severity ?? "") { out.severity = severity }
        if dishList != current.dishes.map({ $0.lowercased() }) { out.dishes = Array(dishList.prefix(3)) }
        return out
    }

    private struct Response: Decodable {
        let ok: Bool
        let error: String?
        let changed: [String]?
        let review: ReviewTags?
    }

    private func save() async {
        let body = changes
        guard !body.isEmpty else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let r: Response = try await APIClient.shared.send("/mobile/api/reviews/\(review.id)/retag", method: .post,
                                                              body: body, retryTransient: false)
            guard r.ok else { error = r.error ?? "Couldn\u{2019}t save those tags."; return }
            Haptic.success()
            onSaved(r.review ?? current)
            dismiss()
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t save those tags."
        }
    }
}

/// A review's tags as the re-tag route answers them: `{id, categories,
/// sentiment, severity, dishes}`.
struct ReviewTags: Decodable, Equatable {
    var categories: [String]
    var sentiment: String?
    var severity: String?
    var dishes: [String]

    init(categories: [String], sentiment: String?, severity: String?, dishes: [String]) {
        self.categories = categories; self.sentiment = sentiment; self.severity = severity; self.dishes = dishes
    }

    init(_ review: Review) {
        self.init(categories: review.categories, sentiment: review.sentiment, severity: review.severity,
                  dishes: review.entities?.dishes ?? [])
    }

    enum CodingKeys: String, CodingKey { case categories, sentiment, severity, dishes }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        categories = ((try? c.decodeIfPresent([String].self, forKey: .categories)) ?? nil) ?? []
        sentiment = (try? c.decodeIfPresent(String.self, forKey: .sentiment)) ?? nil
        severity = (try? c.decodeIfPresent(String.self, forKey: .severity)) ?? nil
        dishes = ((try? c.decodeIfPresent([String].self, forKey: .dishes)) ?? nil) ?? []
    }

    /// "Tagged: food quality, service · negative · Service quality" — the
    /// tags in words, for the line under the review once corrected.
    var line: String {
        var bits: [String] = []
        if !categories.isEmpty { bits.append(categories.map(ReviewTagVocabulary.categoryLabel).joined(separator: ", ")) }
        if let sentiment { bits.append(sentiment) }
        if let severity { bits.append(ReviewTagVocabulary.severities.first { $0.key == severity }?.label ?? severity) }
        if !dishes.isEmpty { bits.append(dishes.joined(separator: ", ")) }
        return "Tagged: " + bits.joined(separator: " \u{00B7} ")
    }
}
