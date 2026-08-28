import SwiftUI

/// Pushed from Account's own "Restaurant profile" row (Option A's own
/// design language: rows push to a detail screen, not inline cards) —
/// restaurant/owner contact info plus the AI-voice fields the backend
/// already returned (voice_notes/never_say/menu_notes/known_for) that the
/// old flat Account screen never actually rendered.
struct AccountProfileDetailView: View {
    let viewModel: AccountViewModel
    let profile: AccountProfile
    @Environment(SessionStore.self) private var sessionStore
    @State private var showingUpdateEmail = false
    @State private var showingLocationSwitcher = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 4) {
                    sectionHeader("Restaurant")
                    infoCard {
                        infoRow("Restaurant", profile.restaurantName)
                        if let location = profile.locationName {
                            divider()
                            infoRow("Location", location)
                        }
                        if let neighborhood = profile.neighborhood {
                            divider()
                            infoRow("Neighborhood", neighborhood)
                        }
                        if let vibe = profile.vibe {
                            divider()
                            infoRow("Atmosphere", vibe)
                        }
                        if let knownFor = profile.knownFor {
                            divider()
                            infoRow("Known for", knownFor)
                        }
                    }
                }

                VStack(alignment: .leading, spacing: 4) {
                    sectionHeader("Contact")
                    infoCard {
                        infoRow("Owner", profile.ownerName ?? "—")
                        divider()
                        HStack {
                            Text("Email").font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                            Spacer()
                            Text(profile.ownerEmail ?? "—")
                                .font(.cavnarBody(13, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            Button {
                                Haptic.light()
                                showingUpdateEmail = true
                            } label: {
                                Text("Update")
                                    .font(.cavnarBody(11, weight: 700))
                                    .foregroundStyle(Color.cavnarEmber)
                            }
                        }
                        divider()
                        infoRow("Phone", profile.ownerPhone ?? "—")
                    }
                }

                // Only ever shown when this login is an owner on a
                // multi-location group — LocationSwitcherView itself
                // already returns an empty list otherwise, but gating the
                // row too avoids presenting a sheet that just shows
                // "No other locations" for the common single-location case.
                if sessionStore.currentUser?.isOwner == true {
                    Button {
                        Haptic.light()
                        showingLocationSwitcher = true
                    } label: {
                        HStack {
                            Text("Locations").font(.cavnarBody(13, weight: 600))
                            Spacer()
                            Image(systemName: "chevron.right").font(.system(size: 12)).foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    .foregroundStyle(Color.cavnarInk)
                    .cavnarCard()
                }

                if profile.voiceNotes != nil || profile.neverSay != nil || profile.menuNotes != nil {
                    VStack(alignment: .leading, spacing: 4) {
                        sectionHeader("How the AI writes for you")
                        infoCard {
                            if let voice = profile.voiceNotes {
                                multilineRow("Brand voice", voice)
                            }
                            if let neverSay = profile.neverSay {
                                if profile.voiceNotes != nil { divider() }
                                multilineRow("Never says", neverSay)
                            }
                            if let menuNotes = profile.menuNotes {
                                if profile.voiceNotes != nil || profile.neverSay != nil { divider() }
                                multilineRow("Menu highlights", menuNotes)
                            }
                        }
                    }
                }

                Text("To update your profile details, email will@cavnar.ai")
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .padding(20)
        }
        .cavnarModuleBackground()
        .navigationTitle("Profile")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showingUpdateEmail) {
            UpdateEmailSheet(viewModel: viewModel)
        }
        .sheet(isPresented: $showingLocationSwitcher) {
            LocationSwitcherView { Task { await viewModel.load() } }
        }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.cavnarBody(11, weight: 700))
            .foregroundStyle(Color.cavnarInk3)
    }

    private func infoCard<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) { content() }
            .cavnarCard()
    }

    private func infoRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
            Spacer()
            Text(value).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
        }
    }

    private func multilineRow(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label).font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
            Text(value).font(.cavnarBody(13, weight: 500)).foregroundStyle(Color.cavnarInk).lineSpacing(3)
        }
    }

    private func divider() -> some View {
        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
    }
}
