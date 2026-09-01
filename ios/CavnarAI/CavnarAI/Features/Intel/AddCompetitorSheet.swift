import SwiftUI

private enum AddCompetitorField: Hashable {
    case search
}

/// Search-driven "add a competitor" flow — reachable from competitorsSection's
/// own header pill (IntelView.swift), next to the existing Refresh pill.
/// Deliberately a name search against Google Places (biased to this
/// restaurant's own location), not a raw Place ID field the way the
/// admin-only custom_competitors setting works — an owner knows "The
/// Graceful Ordinary," not its Place ID.
struct AddCompetitorSheet: View {
    let viewModel: IntelViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var results: [PlaceSearchResult] = []
    @State private var isSearching = false
    @State private var hasSearchedOnce = false
    @State private var searchTask: Task<Void, Never>?
    @State private var addingPlaceId: String?
    @FocusState private var focusedField: AddCompetitorField?

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 0) {
                CavnarFloatingField(
                    icon: "magnifyingglass", placeholder: "Restaurant name", text: $query,
                    autocapitalization: .words,
                    focus: $focusedField, field: .search
                )
                .padding(20)
                // Debounced, not fired on every keystroke — this is a real
                // Google Places call with real cost, so waiting for a brief
                // pause in typing (matching the debounce-and-cancel pattern
                // already established for async work elsewhere in this app)
                // keeps a normal typing burst to one request, not one per
                // character.
                .onChange(of: query) { _, newValue in
                    searchTask?.cancel()
                    let trimmed = newValue.trimmingCharacters(in: .whitespaces)
                    guard trimmed.count >= 2 else {
                        results = []
                        hasSearchedOnce = false
                        isSearching = false
                        return
                    }
                    searchTask = Task {
                        try? await Task.sleep(for: .milliseconds(450))
                        guard !Task.isCancelled else { return }
                        isSearching = true
                        let found = await viewModel.searchPlaces(query: trimmed)
                        guard !Task.isCancelled else { return }
                        results = found
                        hasSearchedOnce = true
                        isSearching = false
                    }
                }

                if let addingPlaceId, let addingResult = results.first(where: { $0.placeId == addingPlaceId }) {
                    addingStatus(addingResult)
                } else if isSearching {
                    CavnarLoadingSeal(size: 32).frame(maxWidth: .infinity).padding(.top, 40)
                } else if hasSearchedOnce && results.isEmpty {
                    Text("No matches found")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(maxWidth: .infinity)
                        .padding(.top, 40)
                } else if !results.isEmpty {
                    ScrollView {
                        VStack(spacing: 0) {
                            ForEach(Array(results.enumerated()), id: \.element.id) { index, result in
                                resultRow(result)
                                if index < results.count - 1 {
                                    Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                                }
                            }
                        }
                        .padding(.horizontal, 20)
                    }
                } else {
                    Text("Search for a nearby restaurant to track it in your competitor comparison — even ones our automatic search doesn't catch.")
                        .font(.cavnarBody(12))
                        .foregroundStyle(Color.cavnarInk3)
                        .lineSpacing(3)
                        .padding(.horizontal, 20)
                        .padding(.top, 12)
                }
                Spacer(minLength: 0)
            }
            .cavnarModuleBackground()
            .navigationTitle("Add a Competitor")
            .navigationBarTitleDisplayMode(.inline)
            .keyboardDoneToolbar { focusedField = nil }
            .toolbar {
                cavnarToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }

    private func resultRow(_ result: PlaceSearchResult) -> some View {
        Button {
            Haptic.light()
            Task {
                addingPlaceId = result.placeId
                let success = await viewModel.addCompetitor(placeId: result.placeId)
                addingPlaceId = nil
                if success { dismiss() }
            }
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(result.name)
                        .font(.cavnarBody(13.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .lineLimit(1)
                    if !result.address.isEmpty {
                        Text(result.address)
                            .font(.cavnarBody(11))
                            .foregroundStyle(Color.cavnarInk3)
                            .lineLimit(1)
                    }
                    if result.reviewCount > 0 {
                        HStack(spacing: 4) {
                            Image(systemName: "star.fill")
                                .font(.system(size: 8))
                                .foregroundStyle(Color.cavnarAmber)
                            Text(String(format: "%.1f", result.rating) + " · \(result.reviewCount) reviews")
                                .font(.cavnarBody(10.5))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                }
                Spacer(minLength: 8)
                Image(systemName: "plus.circle.fill")
                    .font(.system(size: 20))
                    .foregroundStyle(Color.cavnarEmber)
            }
            .padding(.vertical, 12)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(addingPlaceId != nil)
    }

    /// Replaces the whole results list (not just the tapped row) once a
    /// selection is made — addCompetitor's own save is quick, but it then
    /// kicks off the same 20-40s Google Places + Claude job the "Refresh"
    /// button uses, genuinely needed here (unlike removal) since a brand
    /// new competitor has no reviews cached yet for the AI insight to
    /// consider. A tiny trailing spinner on one row gave zero indication
    /// of that, which is what read as a bare, unexplained "generic
    /// spinner" — this uses the house shimmer loading convention (same
    /// treatment as the AI Visibility check button) plus an actual
    /// explanation of what's happening and how long it reasonably takes,
    /// so a 20-40s wait doesn't feel like the app hung.
    private func addingStatus(_ result: PlaceSearchResult) -> some View {
        VStack(spacing: 14) {
            Spacer(minLength: 48)
            CavnarShimmerText(text: "Adding \(result.name)…", color: Color.cavnarInk)
                .font(.cavnarBody(14, weight: 600))
            CavnarShimmerLine(color: .cavnarEmber2)
                .frame(width: 140)
            Text("Fetching reviews and updating your competitive analysis — this usually takes 20–40 seconds.")
                .font(.cavnarBody(12))
                .foregroundStyle(Color.cavnarInk3)
                .multilineTextAlignment(.center)
                .lineSpacing(3)
                .padding(.horizontal, 36)
            Spacer(minLength: 48)
        }
        .frame(maxWidth: .infinity)
    }
}
