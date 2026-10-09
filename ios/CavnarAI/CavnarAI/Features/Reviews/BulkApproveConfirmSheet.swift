import SwiftUI

/// Select mode's Approve confirm (re-audit 10/8/26): every reply that would
/// post, in its own words — not a count (NS5 M10, the proposal card's rule).
/// `reviews` is the snapshot shown; Approve posts exactly those, each bound
/// to the words listed here (`review_hashes`), so a reply rewritten while
/// the sheet was open is not posted. Approve is pinned in thumb reach under
/// the list (readability round 10/8/26) — it used to sit after every reply.
struct BulkApproveConfirmSheet: View {
    let reviews: [Review]
    /// Selected but not in the bulk — read one at a time.
    let held: Int
    let isWorking: Bool
    var onApprove: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.m) {
                    CavnarMixedText(Self.title(reviews.count), role: .headline)
                    Text(ReviewsListView.bulkApproveMessage(held: held))
                        .cavnarText(.body)
                        .fixedSize(horizontal: false, vertical: true)
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
                    HomeMixedText.make("Approve \(reviews.count)", role: .label,
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
        }
    }

    static func title(_ n: Int) -> String {
        "Approve and post \(n) \(n == 1 ? "reply" : "replies")?"
    }

    /// "5★ Ann", then the reply as it would post.
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
