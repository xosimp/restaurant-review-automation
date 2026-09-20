import SwiftUI

/// What Cavnar AI can say on day one, before the account has any data.
///
/// The delight audit found the first session was a working dashboard with
/// nothing in it: reviews wait on a fetch, labor waits on an upload, food
/// cost waits on a count, and the honest empty states that say so are
/// correct but tell an owner nothing about their own restaurant. Honest
/// emptiness buys trust, not a first impression — those are different
/// purchases.
///
/// These lines come from Google Places via the Place ID already on the
/// account (`first_look.py`), so they are available the instant it exists.
/// The server returns an empty list the moment the restaurant has data of
/// its own, which is what keeps this a first-session card rather than a
/// permanent fixture telling a three-month client their Google rating.
struct HomeFirstLook: View {
    let lines: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HomeSectionHeader(kicker: "Day one", title: "What I can already see")
            VStack(alignment: .leading, spacing: 8) {
                ForEach(lines, id: \.self) { line in
                    HomeMixedText.make(line, size: 14.5, weight: 500, color: .cavnarInk2)
                }
                // Says where it came from. A number with no source is the
                // one thing this product never ships, and it matters most
                // on the very first figure an owner ever sees from it.
                Text("From your Google listing — your own numbers replace this as they arrive.")
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 2)
            }
            .cavnarCard()
        }
    }
}
