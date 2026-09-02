import SwiftUI

/// Opened from Account's "Profile & details" row. Option A ("identity
/// card") from the account-sheet design review: the sheet opens on who
/// this restaurant is — monogram tile, name, location — with the admin-set
/// facts as chips, then the editable contact and AI-voice fields in warm
/// cards. Restaurant identity fields (name/location/neighborhood/vibe/
/// known-for) stay admin-managed because client_api.py matches several of
/// them by exact string (AI query construction, competitor lookups); the
/// "Set during onboarding" chip is the whole lock notice now.
struct AccountProfileDetailView: View {
    let viewModel: AccountViewModel
    let profile: AccountProfile
    @Environment(\.dismiss) private var dismiss
    @Environment(SessionStore.self) private var sessionStore
    @State private var showingUpdateEmail = false
    @State private var showingLocationSwitcher = false
    @State private var locations = LocationSwitcherViewModel()

    @State private var ownerName: String
    @State private var ownerPhone: String
    @State private var voiceNotes: String
    @State private var neverSay: String
    @State private var menuNotes: String

    private enum Field: Hashable { case ownerName, ownerPhone, voiceNotes, neverSay, menuNotes }
    @FocusState private var focusedField: Field?
    @State private var postedLabel: String?

    init(viewModel: AccountViewModel, profile: AccountProfile) {
        self.viewModel = viewModel
        self.profile = profile
        _ownerName  = State(initialValue: profile.ownerName ?? "")
        _ownerPhone = State(initialValue: profile.ownerPhone ?? "")
        _voiceNotes = State(initialValue: profile.voiceNotes ?? "")
        _neverSay   = State(initialValue: profile.neverSay ?? "")
        _menuNotes  = State(initialValue: profile.menuNotes ?? "")
    }

    private var isOwner: Bool { sessionStore.currentUser?.isOwner == true }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    hero
                    chips
                    contactSection
                    voiceSection

                    if let error = viewModel.saveProfileError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    Button {
                        Task {
                            await viewModel.updateProfile(
                                ownerName: ownerName, ownerPhone: ownerPhone,
                                voiceNotes: voiceNotes, neverSay: neverSay, menuNotes: menuNotes
                            )
                            if viewModel.saveProfileSucceeded {
                                Haptic.success()
                                postedLabel = "Profile saved"
                            }
                        }
                    } label: {
                        Group {
                            if viewModel.isSavingProfile {
                                CavnarShimmerText(text: "Saving…")
                            } else {
                                Text("Save changes")
                            }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSavingProfile))
                    .disabled(viewModel.isSavingProfile)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Restaurant")
            .keyboardDoneToolbar { focusedField = nil }
            .cavnarPostedOverlay(postedLabel) { dismiss() }
            .sheet(isPresented: $showingUpdateEmail) {
                UpdateEmailSheet(viewModel: viewModel)
            }
            .sheet(isPresented: $showingLocationSwitcher) {
                LocationSwitcherView { Task { await viewModel.load() } }
            }
            .task {
                if isOwner { await locations.load() }
            }
        }
    }

    // MARK: - Identity

    private var initials: String {
        let words = profile.restaurantName.split(separator: " ")
        return String(words.prefix(2).compactMap { $0.first }).uppercased()
    }

    private var hero: some View {
        AccountHero(title: profile.restaurantName) {
            GlowBadge(systemImage: "building.2", size: 64, monogram: initials)
        } subtitle: {
            Text(subtitleLine)
        }
    }

    /// Onboarding data for Gia Mia had `neighborhood` re-stating the exact
    /// city/state `locationName` already shows ("St. Charles, IL" +
    /// "St. Charles, Illinois — downtown First Street Plaza"), so the hero
    /// read "St.Charles, IL - St. Charles, Illinois - downtown First
    /// St...". Fixed the source data, but this strips a repeated leading
    /// city/state clause from `neighborhood` before ever joining the two,
    /// so a future restaurant entered the same way can't reproduce it.
    private var subtitleLine: String {
        var parts: [String] = []
        if let loc = profile.locationName, !loc.isEmpty { parts.append(loc) }
        if let nb = profile.neighborhood, !nb.isEmpty {
            let detail = Self.stripCityOverlap(from: nb, cityLine: profile.locationName)
            if !detail.isEmpty { parts.append(detail) }
        }
        return parts.joined(separator: " · ")
    }

    private static func stripCityOverlap(from neighborhood: String, cityLine: String?) -> String {
        guard let cityLine, !cityLine.isEmpty else { return neighborhood }
        let city = cityLine.split(separator: ",").first.map(String.init) ?? cityLine
        guard !city.isEmpty else { return neighborhood }
        for separator in [" — ", " – ", " - "] {
            guard let range = neighborhood.range(of: separator) else { continue }
            let head = String(neighborhood[..<range.lowerBound])
            guard head.localizedCaseInsensitiveContains(city) else { return neighborhood }
            let tail = String(neighborhood[range.upperBound...]).trimmingCharacters(in: .whitespaces)
            return tail.isEmpty ? neighborhood : tail.prefix(1).uppercased() + tail.dropFirst()
        }
        return neighborhood
    }

    /// Known-for reads as one chip per thing ("Wood-fired pizza & house
    /// pasta" → two chips), not one long one.
    private var factChips: [String] {
        var out: [String] = []
        if let vibe = profile.vibe, !vibe.isEmpty { out.append(vibe) }
        if let knownFor = profile.knownFor {
            let parts = knownFor
                .replacingOccurrences(of: " and ", with: " & ")
                .split(whereSeparator: { $0 == "&" || $0 == "," })
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            out.append(contentsOf: parts.map { $0.prefix(1).uppercased() + $0.dropFirst() })
        }
        return out
    }

    // Chips are collapsed to just the first one at rest — a wall of orange
    // pills under the restaurant name was too much before you've even
    // reached the editable fields. Tapping the "+N" chip expands the rest;
    // tapping the trailing chip again (now "Less") collapses back.
    @State private var chipsExpanded = false

    private var allChips: [(text: String, muted: Bool)] {
        factChips.map { ($0, false) } + [("Set during onboarding", true)]
    }

    private var chips: some View {
        AccountFlowLayout(spacing: 6) {
            if let first = allChips.first {
                AccountChip(text: first.text, muted: first.muted)
            }
            if allChips.count > 1 {
                if chipsExpanded {
                    ForEach(allChips.dropFirst().indices, id: \.self) { i in
                        AccountChip(text: allChips[i].text, muted: allChips[i].muted)
                    }
                    chipToggle(label: "Less", systemImage: "chevron.up", expand: false)
                } else {
                    chipToggle(label: "+\(allChips.count - 1)", systemImage: "chevron.down", expand: true)
                }
            }
        }
    }

    private func chipToggle(label: String, systemImage: String, expand: Bool) -> some View {
        Button {
            Haptic.light()
            withAnimation(.easeOut(duration: 0.2)) { chipsExpanded = expand }
        } label: {
            HStack(spacing: 3) {
                Text(label)
                Image(systemName: systemImage).font(.system(size: 9, weight: .bold))
            }
            .font(.cavnarBody(13.5, weight: 700))
            .foregroundStyle(Color.cavnarInk2)
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
            .background(Color.white.opacity(0.05))
            .overlay(Capsule().strokeBorder(Color.white.opacity(0.1), lineWidth: 1))
            .clipShape(Capsule())
        }
        .buttonStyle(.plain)
    }

    // MARK: - Contact

    private var contactSection: some View {
        AccountSection(kicker: "Contact") {
            AccountField(label: "Owner", text: $ownerName, focus: $focusedField, field: .ownerName)
            AccountField(label: "Phone", text: $ownerPhone, focus: $focusedField, field: .ownerPhone, keyboardType: .phonePad, isNumber: true)
            // Shares AccountFieldRow's exact label/value/reserved-underline
            // footprint (see AccountDisplayRow's own doc comment) — Email
            // isn't edited inline (it opens its own sheet), but it sits in
            // this same card next to Owner/Phone and needs to measure the
            // same height as they do.
            AccountDisplayRow(label: "Email", value: profile.ownerEmail ?? "—", showsDivider: isOwner) {
                AccountLink(title: "Update") { showingUpdateEmail = true }
            }
            if isOwner {
                // AccountKVRow, not a hand-rolled HStack — Locations is a
                // single-line "tap to go elsewhere" row, the same family
                // as Sign-in's Password/2FA rows, so it gets their exact
                // shared row height instead of improvising its own.
                Button {
                    Haptic.light()
                    showingLocationSwitcher = true
                } label: {
                    AccountKVRow(label: "Locations", showsDivider: false) {
                        if locations.locations.isEmpty {
                            Image(systemName: "chevron.right").font(.system(size: 13, weight: .semibold)).foregroundStyle(Color.cavnarInk3)
                        } else {
                            AccountChip(text: "\(locations.locations.count)", muted: true)
                        }
                    }
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: - AI voice

    private var voiceSection: some View {
        AccountSection(kicker: "How the AI writes for you") {
            AccountEditor(label: "Brand voice", placeholder: "e.g. warm, a little playful, never corporate", text: $voiceNotes, focus: $focusedField, field: .voiceNotes)
            AccountEditor(label: "Never says", placeholder: "Phrases or claims the AI should avoid", text: $neverSay, focus: $focusedField, field: .neverSay)
            AccountEditor(label: "Menu highlights", placeholder: "Dishes, specials, or ingredients worth mentioning", text: $menuNotes, focus: $focusedField, field: .menuNotes, showsDivider: false)
        }
    }
}
