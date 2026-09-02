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
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
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
            Text([profile.locationName, profile.neighborhood].compactMap { $0 }.joined(separator: " · "))
        }
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

    private var chips: some View {
        AccountFlowLayout(spacing: 6) {
            ForEach(factChips, id: \.self) { AccountChip(text: $0) }
            AccountChip(text: "Set during onboarding", muted: true)
        }
    }

    // MARK: - Contact

    private var contactSection: some View {
        AccountSection(kicker: "Contact") {
            AccountField(label: "Owner", text: $ownerName, focus: $focusedField, field: .ownerName)
            AccountField(label: "Phone", text: $ownerPhone, focus: $focusedField, field: .ownerPhone, keyboardType: .phonePad, isNumber: true)
            AccountKVRow(label: "", showsDivider: isOwner) {
                HStack(alignment: .center) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("EMAIL").font(.cavnarBody(12.5, weight: 700)).tracking(0.8).foregroundStyle(Color.cavnarInk3)
                        Text(profile.ownerEmail ?? "—").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarInk)
                    }
                    Spacer()
                    AccountLink(title: "Update") { showingUpdateEmail = true }
                }
            }
            if isOwner {
                Button {
                    Haptic.light()
                    showingLocationSwitcher = true
                } label: {
                    HStack {
                        Text("Locations").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarInk)
                        Spacer()
                        if locations.locations.isEmpty {
                            Image(systemName: "chevron.right").font(.system(size: 12, weight: .semibold)).foregroundStyle(Color.cavnarInk3)
                        } else {
                            AccountChip(text: "\(locations.locations.count)", muted: true)
                        }
                    }
                    .padding(.vertical, 10)
                    .contentShape(Rectangle())
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
