import SwiftUI

/// Select mode's Approve confirm (re-audit 10/8/26): every reply that would
/// post, in its own words — not a count (NS5 M10, the proposal card's rule).
/// `reviews` is the snapshot shown; Approve posts exactly those, each bound
/// to the words listed here (`review_hashes`), so a reply rewritten while
/// the sheet was open is not posted.
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
                VStack(alignment: .leading, spacing: 16) {
                    HomeMixedText.make(Self.title(reviews.count), size: 19, weight: 700, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(ReviewsListView.bulkApproveMessage(held: held))
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(reviews) { r in
                            replyRow(r)
                        }
                    }
                    Button {
                        Haptic.light()
                        onApprove()
                        dismiss()
                    } label: {
                        HomeMixedText.make("Approve \(reviews.count)", size: 16, weight: 600,
                                           color: .white, numberColor: .white)
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: reviews.isEmpty || isWorking))
                    .disabled(reviews.isEmpty || isWorking)
                }
                .padding(20)
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
        VStack(alignment: .leading, spacing: 6) {
            HomeMixedText.make("\(r.rating.map { "\($0)\u{2605}" } ?? "\u{2014}") \(r.author ?? "A guest")",
                               size: 12, weight: 700, color: .cavnarEmber2, numberColor: .cavnarEmber2)
            Text((r.draftResponse ?? "").trimmingCharacters(in: .whitespacesAndNewlines))
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
        .accessibilityElement(children: .combine)
    }
}
