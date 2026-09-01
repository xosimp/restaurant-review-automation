import SwiftUI

/// Opened from Account's "Profile & details" row. Split down the middle:
/// restaurant identity fields (name/location/neighborhood/vibe/known-for)
/// are set once by Will during onboarding and stay locked here, because
/// client_api.py matches several of them by exact string elsewhere (AI
/// query construction, competitor lookups) — a client edit could silently
/// break that matching with no visible error. Everything else here has no
/// such dependency (pure contact info, or freeform notes fed to the AI as
/// context rather than parsed), so it's a real editable form now instead
/// of "email will@cavnar.ai to change this."
struct AccountProfileDetailView: View {
    let viewModel: AccountViewModel
    let profile: AccountProfile
    @Environment(\.dismiss) private var dismiss
    @Environment(SessionStore.self) private var sessionStore
    @State private var showingUpdateEmail = false
    @State private var showingLocationSwitcher = false

    @State private var ownerName: String
    @State private var ownerPhone: String
    @State private var voiceNotes: String
    @State private var neverSay: String
    @State private var menuNotes: String

    private enum Field: Hashable { case ownerName, ownerPhone, voiceNotes, neverSay, menuNotes }
    @FocusState private var focusedField: Field?

    init(viewModel: AccountViewModel, profile: AccountProfile) {
        self.viewModel = viewModel
        self.profile = profile
        _ownerName  = State(initialValue: profile.ownerName ?? "")
        _ownerPhone = State(initialValue: profile.ownerPhone ?? "")
        _voiceNotes = State(initialValue: profile.voiceNotes ?? "")
        _neverSay   = State(initialValue: profile.neverSay ?? "")
        _menuNotes  = State(initialValue: profile.menuNotes ?? "")
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    lockedSection
                    contactSection
                    voiceSection

                    if let error = viewModel.saveProfileError {
                        Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                    }

                    Button {
                        Task {
                            await viewModel.updateProfile(
                                ownerName: ownerName, ownerPhone: ownerPhone,
                                voiceNotes: voiceNotes, neverSay: neverSay, menuNotes: menuNotes
                            )
                            if viewModel.saveProfileSucceeded { dismiss() }
                        }
                    } label: {
                        if viewModel.isSavingProfile {
                            ProgressView().tint(.white)
                        } else {
                            Text("Save changes")
                        }
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSavingProfile))
                    .disabled(viewModel.isSavingProfile)
                    .frame(maxWidth: .infinity)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Profile")
            .navigationBarTitleDisplayMode(.inline)
            .keyboardDoneToolbar { focusedField = nil }
            .sheet(isPresented: $showingUpdateEmail) {
                UpdateEmailSheet(viewModel: viewModel)
            }
            .sheet(isPresented: $showingLocationSwitcher) {
                LocationSwitcherView { Task { await viewModel.load() } }
            }
        }
    }

    // MARK: - Locked (admin-set) fields

    private var lockedSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            sectionHeader("Restaurant")
            VStack(alignment: .leading, spacing: 10) {
                lockedRow("Restaurant", profile.restaurantName)
                if let location = profile.locationName {
                    divider()
                    lockedRow("Location", location)
                }
                if let neighborhood = profile.neighborhood {
                    divider()
                    lockedRow("Neighborhood", neighborhood)
                }
                if let vibe = profile.vibe {
                    divider()
                    lockedRow("Atmosphere", vibe)
                }
                if let knownFor = profile.knownFor {
                    divider()
                    lockedRow("Known for", knownFor)
                }
                divider()
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "lock.fill")
                        .font(.system(size: 10))
                        .foregroundStyle(Color.cavnarInk3)
                        .padding(.top, 1)
                    Text("Set during setup — this is what the AI uses to describe your restaurant, so it stays admin-managed. Email will@cavnar.ai to change it.")
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarInk3)
                }
                .padding(.top, 2)
            }
            .cavnarCard()
        }
    }

    private func lockedRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
            Spacer()
            Text(value).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk2)
        }
    }

    // MARK: - Editable contact

    private var contactSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            sectionHeader("Contact")
            VStack(alignment: .leading, spacing: 16) {
                profileField("Owner name", text: $ownerName, field: .ownerName)
                profileField("Phone", text: $ownerPhone, field: .ownerPhone, keyboardType: .phonePad)

                divider()
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Email").font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
                        Text(profile.ownerEmail ?? "—").font(.cavnarBody(14, weight: 600)).foregroundStyle(Color.cavnarInk)
                    }
                    Spacer()
                    Button {
                        Haptic.light()
                        showingUpdateEmail = true
                    } label: {
                        Text("Update")
                            .font(.cavnarBody(11, weight: 700))
                            .foregroundStyle(Color.cavnarEmber)
                    }
                }

                if sessionStore.currentUser?.isOwner == true {
                    divider()
                    Button {
                        Haptic.light()
                        showingLocationSwitcher = true
                    } label: {
                        HStack {
                            Text("Locations").font(.cavnarBody(14, weight: 600)).foregroundStyle(Color.cavnarInk)
                            Spacer()
                            Image(systemName: "chevron.right").font(.system(size: 12)).foregroundStyle(Color.cavnarInk3)
                        }
                    }
                }
            }
            .cavnarCard()
        }
    }

    // MARK: - AI voice (editable, freeform)

    private var voiceSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            sectionHeader("How the AI writes for you")
            VStack(alignment: .leading, spacing: 16) {
                profileEditor("Brand voice", placeholder: "e.g. warm, a little playful, never corporate", text: $voiceNotes, field: .voiceNotes)
                divider()
                profileEditor("Never says", placeholder: "Phrases or claims the AI should avoid", text: $neverSay, field: .neverSay)
                divider()
                profileEditor("Menu highlights", placeholder: "Dishes, specials, or ingredients worth mentioning", text: $menuNotes, field: .menuNotes)
            }
            .cavnarCard()
        }
    }

    // MARK: - Shared field styles

    private func profileField(_ label: String, text: Binding<String>, field: Field, keyboardType: UIKeyboardType = .default) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
            TextField(label, text: text)
                .font(.cavnarBody(14, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .keyboardType(keyboardType)
                .focused($focusedField, equals: field)
        }
    }

    private func profileEditor(_ label: String, placeholder: String, text: Binding<String>, field: Field) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
            ZStack(alignment: .topLeading) {
                if text.wrappedValue.isEmpty {
                    Text(placeholder)
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk3.opacity(0.6))
                        .padding(.top, 8)
                        .padding(.leading, 4)
                }
                TextEditor(text: text)
                    .font(.cavnarBody(13, weight: 500))
                    .foregroundStyle(Color.cavnarInk)
                    .scrollContentBackground(.hidden)
                    .frame(minHeight: 64, maxHeight: 110)
                    .focused($focusedField, equals: field)
            }
        }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.cavnarBody(12.5, weight: 700))
            .foregroundStyle(Color.cavnarInk3)
    }

    private func divider() -> some View {
        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
    }
}
