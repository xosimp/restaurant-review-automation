import SwiftUI

private enum IntelSubTab: String, CaseIterable, Identifiable {
    case competitors = "Competitors"
    case aiVisibility = "AI Visibility"
    var id: String { rawValue }
}

struct IntelView: View {
    @State private var viewModel = IntelViewModel()
    @State private var aiVisibilityViewModel = AIVisibilityViewModel()
    @State private var subTab: IntelSubTab = .competitors
    @State private var expandedCompetitors: Set<String> = []

    var body: some View {
        VStack(spacing: 0) {
            CavnarSegmentedControl(selection: $subTab, options: IntelSubTab.allCases) { $0.rawValue }
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 16)

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if subTab == .competitors {
                        if let summary = viewModel.summary {
                            if !summary.hasData {
                                emptyState
                            } else {
                                content(summary)
                            }
                        } else if viewModel.isLoading {
                            ProgressView().padding(.top, 60)
                        } else if let error = viewModel.errorMessage {
                            VStack(spacing: 8) {
                                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                                Button("Retry") { Task { await viewModel.load() } }
                            }
                            .padding(.top, 60)
                            .frame(maxWidth: .infinity)
                        }
                    } else {
                        AIVisibilitySection(viewModel: aiVisibilityViewModel)
                    }
                }
                .padding(20)
            }
        }
        .cavnarModuleBackground()
        .refreshable { await viewModel.load() }
        .navigationTitle("Intel")
        .navigationBarTitleDisplayMode(.inline)
        .cavnarEmberBackButton()
        .task { await viewModel.load() }
    }

    private var emptyState: some View {
        VStack(spacing: 14) {
            Image(systemName: "binoculars")
                .font(.system(size: 32))
                .foregroundStyle(Color.cavnarInk3)
            Text("No competitor data yet")
                .font(.cavnarBody(14, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Text("See how your ratings, review volume, and reputation stack up against nearby restaurants. Takes about 30 seconds.")
                .font(.cavnarBody(12))
                .foregroundStyle(Color.cavnarInk3)
                .multilineTextAlignment(.center)
            refreshButton(label: "Fetch competitor data")
            if let error = viewModel.refreshError {
                Text(error).font(.cavnarBody(11)).foregroundStyle(Color.cavnarRed)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
    }

    @ViewBuilder
    private func content(_ summary: IntelSummary) -> some View {
        statRow(summary)

        if let intro = summary.intro, !intro.isEmpty {
            Text(intro)
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk2)
        }

        ForEach(summary.sections) { section in
            VStack(alignment: .leading, spacing: 8) {
                Text(section.name)
                    .font(.cavnarBody(13, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                ForEach(section.bullets, id: \.self) { bullet in
                    Text("• \(bullet)")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk2)
                }
            }
            .cavnarCard()
        }

        if let ownRating = summary.ownRating, !summary.competitors.isEmpty {
            ratingComparisonChart(summary, ownRating: ownRating)
        }

        if !summary.recommendations.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text("Recommendations")
                    .font(.cavnarBody(13, weight: 700))
                    .foregroundStyle(Color.cavnarEmber)
                ForEach(summary.recommendations, id: \.self) { rec in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: "lightbulb.fill")
                            .font(.system(size: 11))
                            .foregroundStyle(Color.cavnarEmber)
                        Text(rec)
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk)
                    }
                }
            }
            .cavnarCard()
        }

        if !summary.competitors.isEmpty {
            competitorsSection(summary)
        }
    }

    // MARK: - Summary stat row

    private func statRow(_ summary: IntelSummary) -> some View {
        let count = summary.competitors.count
        let avgRating = count > 0 ? summary.competitors.reduce(0.0) { $0 + $1.rating } / Double(count) : 0
        let delta = summary.ownRating.map { (($0 - avgRating) * 10).rounded() / 10 }

        return HStack(spacing: 0) {
            statTile(value: "\(count)", label: "Competitors tracked", tone: Color.cavnarInk)
            Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(width: 1, height: 30)
            statTile(value: count > 0 ? String(format: "%.1f★", avgRating) : "—", label: "Avg competitor rating", tone: Color.cavnarInk)
            Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(width: 1, height: 30)
            if let own = summary.ownRating, let delta {
                VStack(spacing: 4) {
                    Text(String(format: "%.1f★", own))
                        .font(.cavnarNumber(18, weight: 700))
                        .foregroundStyle(own >= 4.0 ? Color.cavnarGreen : (own >= 3.0 ? Color.cavnarAmber : Color.cavnarRed))
                    Text(delta == 0 ? "TIED" : (delta > 0 ? "▲\(String(format: "%.1f", delta)) ABOVE AVG" : "▼\(String(format: "%.1f", abs(delta))) BELOW AVG"))
                        .font(.cavnarBody(8, weight: 700))
                        .tracking(0.6)
                        .foregroundStyle(Color.cavnarInk3)
                }
                .frame(maxWidth: .infinity)
            } else {
                statTile(value: "\(summary.recommendations.count)", label: "Action items", tone: Color.cavnarInk)
            }
        }
        .cavnarCard()
    }

    private func statTile(value: String, label: String, tone: Color) -> some View {
        VStack(spacing: 4) {
            Text(value)
                .font(.cavnarNumber(18, weight: 700))
                .foregroundStyle(tone)
            Text(label.uppercased())
                .font(.cavnarBody(8, weight: 700))
                .tracking(0.6)
                .foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: - Rating comparison

    private func ratingComparisonChart(_ summary: IntelSummary, ownRating: Double) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("RATING COMPARISON")
                .font(.cavnarBody(10, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(spacing: 8) {
                ratingBar(name: "Your restaurant", rating: ownRating, tone: Color.cavnarEmber, boldName: true)
                ForEach(summary.competitors) { c in
                    ratingBar(name: c.name, rating: c.rating, tone: Color.cavnarEmber.opacity(0.45), boldName: false)
                }
            }
        }
        .cavnarCard()
    }

    private func ratingBar(name: String, rating: Double, tone: Color, boldName: Bool) -> some View {
        HStack(spacing: 10) {
            Text(name)
                .font(.cavnarBody(12, weight: boldName ? 700 : 400))
                .foregroundStyle(boldName ? Color.cavnarInk : Color.cavnarInk2)
                .lineLimit(1)
                .frame(width: 100, alignment: .leading)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.cavnarPaper3.opacity(0.6))
                    Capsule().fill(tone)
                        .frame(width: geo.size.width * min(rating / 5, 1))
                }
            }
            .frame(height: 10)
            Text(String(format: "%.1f", rating))
                .font(.cavnarNumber(12, weight: 700))
                .foregroundStyle(boldName ? Color.cavnarEmber : Color.cavnarInk2)
                .frame(width: 30, alignment: .trailing)
        }
    }

    // MARK: - Competitor cards

    private func competitorsSection(_ summary: IntelSummary) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("NEARBY COMPETITORS")
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                Spacer()
                refreshButton(label: "Refresh")
            }
            if let error = viewModel.refreshError {
                Text(error).font(.cavnarBody(11)).foregroundStyle(Color.cavnarRed)
            }
            ForEach(summary.competitors) { c in
                competitorCard(c, ownRating: summary.ownRating)
            }
            if let updatedAt = summary.updatedAt {
                updatedLabel(updatedAt)
            }
        }
    }

    private func competitorCard(_ c: Competitor, ownRating: Double?) -> some View {
        let isExpanded = expandedCompetitors.contains(c.id)
        let visibleReviews = isExpanded ? c.reviews : Array(c.reviews.prefix(2))
        let remaining = c.reviews.count - visibleReviews.count

        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                Text(c.name)
                    .font(.cavnarBody(13, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                if let ownRating {
                    let diff = ((ownRating - c.rating) * 10).rounded() / 10
                    if diff != 0 {
                        Text(diff > 0 ? "▲\(String(format: "%.1f", diff)) ahead" : "▼\(String(format: "%.1f", abs(diff))) behind")
                            .font(.cavnarBody(9, weight: 700))
                            .foregroundStyle(diff > 0 ? Color.cavnarGreen : Color.cavnarAmber)
                    } else {
                        Text("tied")
                            .font(.cavnarBody(9, weight: 700))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
                Text("\(String(format: "%.1f", c.rating))★ \(c.reviewCount) reviews")
                    .font(.cavnarNumber(11))
                    .foregroundStyle(Color.cavnarAmber)
            }
            if !c.vicinity.isEmpty {
                Text(c.vicinity).font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
            }
            ForEach(visibleReviews) { r in
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "star.fill")
                        .font(.system(size: 9))
                        .foregroundStyle(r.rating >= 4 ? Color.cavnarGreen : Color.cavnarRed)
                    Text(r.text)
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarInk3)
                        .lineLimit(2)
                }
                .padding(.top, 4)
                .overlay(alignment: .top) {
                    Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                }
            }
            if remaining > 0 {
                Button {
                    expandedCompetitors.insert(c.id)
                } label: {
                    Text("Show \(remaining) more review\(remaining == 1 ? "" : "s")")
                        .font(.cavnarBody(11, weight: 600))
                        .foregroundStyle(Color.cavnarEmber)
                }
                .padding(.top, 2)
            }
        }
        .cavnarCard()
    }

    private func updatedLabel(_ updatedAt: String) -> some View {
        let datePart = String(updatedAt.prefix(10))
        let parts = datePart.split(separator: "-").compactMap { Int($0) }
        var daysOld: Int? = nil
        if parts.count == 3 {
            var comps = DateComponents()
            comps.year = parts[0]; comps.month = parts[1]; comps.day = parts[2]
            if let date = Calendar.current.date(from: comps) {
                daysOld = Calendar.current.dateComponents([.day], from: date, to: Date()).day
            }
        }
        return HStack(spacing: 8) {
            Text("Last updated: \(datePart)")
                .font(.cavnarBody(11))
                .foregroundStyle(Color.cavnarInk3)
            if let daysOld, daysOld >= 7 {
                Text("Consider refreshing")
                    .font(.cavnarBody(9, weight: 700))
                    .foregroundStyle(Color.cavnarAmber)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(Color.cavnarAmber.opacity(0.18))
                    .clipShape(Capsule())
            }
        }
    }

    private func refreshButton(label: String) -> some View {
        Button {
            Task { await viewModel.refreshCompetitors() }
        } label: {
            if viewModel.isRefreshing {
                HStack(spacing: 6) {
                    ProgressView().tint(.white)
                    Text("Refreshing…")
                }
            } else {
                Text(label)
            }
        }
        .buttonStyle(CavnarPrimaryButtonStyle())
        .disabled(viewModel.isRefreshing)
    }
}
