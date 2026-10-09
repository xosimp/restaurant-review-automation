import SwiftUI

/// The approve confirm (re-audit 10/8/26): every reply that would post, in
/// its own words — not a count (NS5 M10, the proposal card's rule) — and
/// where each one goes (M1): live on Google once Google is connected, or
/// approved for the owner to post on Yelp or another site. A swipe's Approve
/// opens it with its one reply (H1). `reviews` is the snapshot shown; Approve
/// posts exactly those, each bound to the words listed here
/// (`review_hashes`), so a reply rewritten while the sheet was open is not
/// posted. Approve is pinned in thumb reach under the list.
struct BulkApproveConfirmSheet: View {
    let reviews: [Review]
    /// Selected but not in the bulk — read one at a time.
    let held: Int
    let isWorking: Bool
    var onApprove: () -> Void
    @Environment(\.dismiss) private var dismiss
    /// Whether Google Business is connected — nil until read. Unknown never
    /// claims a reply goes live.
    @State private var googleConnected: Bool?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.m) {
                    CavnarMixedText(Self.title(reviews, googleConnected: googleConnected), role: .headline)
                    CavnarMixedText(Self.destinations(reviews, googleConnected: googleConnected, held: held),
                                    role: .body)
                    VStack(alignment: .leading, spacing: CavnarSpace.s) {
                        ForEach(reviews) { r in
                            replyRow(r)
                        }
                    }
                }
                .padding(CavnarSpace.gutter)
            }
            .cavnarPinnedBar {
                Button {
                    Haptic.light()
                    onApprove()
                    dismiss()
                } label: {
                    HomeMixedText.make(Self.buttonLabel(reviews, googleConnected: googleConnected), role: .label,
                                       color: .white, numberColor: .white)
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: reviews.isEmpty || isWorking))
                .disabled(reviews.isEmpty || isWorking)
            }
            .cavnarModuleBackground()
            .navigationTitle("Approve replies")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Approve replies") }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
            .task {
                guard reviews.contains(where: { $0.platform == "google" }) else { return }
                googleConnected = await ReviewDetailViewModel.googleConnection()
            }
        }
    }

    /// How many of these go live on Google now.
    static func postingCount(_ reviews: [Review], googleConnected: Bool?) -> Int {
        googleConnected == true ? reviews.filter { $0.platform == "google" }.count : 0
    }

    /// "Approve and post 2 replies?" only when every one goes live on
    /// Google; otherwise "Approve 3 replies?" — never a claim it posts.
    static func title(_ reviews: [Review], googleConnected: Bool?) -> String {
        let n = reviews.count
        let noun = n == 1 ? "reply" : "replies"
        if n == 1 {
            return postingCount(reviews, googleConnected: googleConnected) == 1
                ? "Approve and post this reply to Google?" : "Approve this reply?"
        }
        return postingCount(reviews, googleConnected: googleConnected) == n
            ? "Approve and post \(n) \(noun)?" : "Approve \(n) \(noun)?"
    }

    static func buttonLabel(_ reviews: [Review], googleConnected: Bool?) -> String {
        let n = reviews.count
        let posting = postingCount(reviews, googleConnected: googleConnected)
        if n == 1 { return posting == 1 ? "Approve & post" : "Approve" }
        return posting == n ? "Approve & post \(n)" : "Approve \(n)"
    }

    /// Where each goes, split by destination like PublishReadySheet's:
    /// "2 post to Google · 1 approved for you to post on Yelp".
    static func destinations(_ reviews: [Review], googleConnected: Bool?, held: Int = 0) -> String {
        let google = reviews.filter { $0.platform == "google" }.count
        var parts: [String] = []
        if google > 0 {
            switch googleConnected {
            case true?:
                parts.append(reviews.count == 1 ? "It goes live on Google under your name, as written below."
                                                : "\(google) post to Google under your name")
            case false?:
                parts.append(reviews.count == 1 ? "It\u{2019}s approved and posts to Google once Google Business is connected."
                                                : "\(google) approved, posting to Google once it\u{2019}s connected")
            case nil:
                parts.append(reviews.count == 1 ? "It goes to Google once Google Business is connected."
                                                : "\(google) for Google, once Google Business is connected")
            }
        }
        // Other sites, by name, in the order they first appear.
        var sites: [String] = []
        var counts: [String: Int] = [:]
        for r in reviews where r.platform != "google" {
            let site = r.platformDisplayName
            if counts[site] == nil { sites.append(site) }
            counts[site, default: 0] += 1
        }
        for site in sites {
            let k = counts[site] ?? 0
            parts.append(reviews.count == 1 ? "It\u{2019}s approved for you to post on \(site) \u{2014} Cavnar AI can\u{2019}t post there for you."
                                            : "\(k) approved for you to post on \(site)")
        }
        var line = parts.joined(separator: " \u{00B7} ")
        if held > 0 {
            line += " The other \(held) stay for you to read one at a time \u{2014} a flagged or urgent reply, or one already decided, is approved on its own."
        }
        return line
    }

    /// "5★ Ann · Google", then the reply as it would post.
    private func replyRow(_ r: Review) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs + 2) {
            HomeMixedText.make("\(r.rating.map { "\($0)\u{2605}" } ?? "\u{2014}") \(r.author ?? "A guest") \u{00B7} \(r.platformDisplayName)",
                               role: .caption, color: .cavnarEmber2, numberColor: .cavnarEmber2)
            Text((r.draftResponse ?? "").trimmingCharacters(in: .whitespacesAndNewlines))
                .cavnarText(.body)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(CavnarSpace.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
        .accessibilityElement(children: .combine)
    }
}
