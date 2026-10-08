import PhotosUI
import SwiftUI

// MARK: - Photo

/// Pick a photo from the camera roll and attach it. This is the piece that
/// makes Instagram publishing usable at all from a phone: the app used to ask
/// for a public image URL, typed by hand, for a picture sitting in Photos.
struct MarketingPhotoPicker: View {
    let viewModel: MarketingComposeViewModel
    @State private var selection: PhotosPickerItem?
    /// The camera — the phone's own advantage over the web's file input.
    @State private var showingCamera = false
    /// A library photo long-pressed for deletion, awaiting the confirm.
    @State private var photoToDelete: MarketingMedia?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            content
            libraryStrip
        }
        .task { if viewModel.recentMedia.isEmpty { await viewModel.loadMedia() } }
        .fullScreenCover(isPresented: $showingCamera) {
            StaffCameraPicker { image in
                showingCamera = false
                guard let image else { return }
                Task { await viewModel.upload(image) }
            }
            .ignoresSafeArea()
        }
        .confirmationDialog("Delete this photo from your library?",
                            isPresented: Binding(get: { photoToDelete != nil }, set: { if !$0 { photoToDelete = nil } }),
                            titleVisibility: .visible, presenting: photoToDelete) { item in
            Button("Delete photo", role: .destructive) {
                Task { await viewModel.deleteMedia(item) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { _ in
            Text("A scheduled post, a draft or an email still sending keeps the photo it uses.")
        }
    }

    /// Recent uploads, newest first: tap to use one, long-press to delete it.
    @ViewBuilder
    private var libraryStrip: some View {
        if !viewModel.recentMedia.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("YOUR PHOTOS")
                    .font(.cavnarBody(CavnarType.kicker, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                ScrollView(.horizontal) {
                    HStack(spacing: 8) {
                        ForEach(viewModel.recentMedia.prefix(12)) { item in
                            thumb(item)
                        }
                    }
                    .padding(.vertical, 2)
                }
                .scrollIndicators(.hidden)
            }
        }
    }

    private func thumb(_ item: MarketingMedia) -> some View {
        let on = viewModel.media?.id == item.id
        return Button {
            viewModel.select(item)
        } label: {
            AsyncImage(url: URL(string: item.url)) { image in
                image.resizable().aspectRatio(contentMode: .fill)
            } placeholder: {
                Color.cavnarPaper2
            }
            .frame(width: 64, height: 64)
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(on ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: on ? 2 : 1))
            .overlay(alignment: .topTrailing) {
                if on {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 15, weight: .bold))
                        .foregroundStyle(Color.cavnarEmber)
                        .padding(4)
                }
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(on ? "Photo in use" : "Use this photo")
        .accessibilityHint("Touch and hold to delete it from your library")
        .contextMenu {
            Button { viewModel.select(item) } label: { Label("Use this photo", systemImage: "checkmark") }
            Button(role: .destructive) {
                photoToDelete = item
            } label: { Label("Delete photo", systemImage: "trash") }
        }
    }

    @ViewBuilder
    private var content: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let media = viewModel.media {
                HStack(spacing: 12) {
                    AsyncImage(url: URL(string: media.url)) { image in
                        image.resizable().aspectRatio(contentMode: .fill)
                    } placeholder: {
                        Color.cavnarPaper2
                    }
                    .frame(width: 72, height: 72)
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))

                    VStack(alignment: .leading, spacing: 4) {
                        Text("Photo attached")
                            .font(.cavnarBody(16, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        if let w = media.width, let h = media.height {
                            (Text("\(w)").font(.cavnarNumber(15)) + Text(" × ")
                                + Text("\(h)").font(.cavnarNumber(15)))
                                .font(.cavnarBody(15))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    Spacer()
                    Button {
                        viewModel.clearMedia()
                    } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(Color.cavnarInk3)
                    }
                }
            }

            // Dashed drop zones rather than more outlined buttons — they
            // read as "a photo goes here", which is what they are, and stop
            // competing with the four editing actions under them. The camera
            // first where there is one: the dish is in front of the owner.
            HStack(spacing: 8) {
                if StaffCameraPicker.isAvailable {
                    Button {
                        Haptic.light()
                        showingCamera = true
                    } label: {
                        MarketingDropZone(text: viewModel.isUploading ? nil : "Take photo", systemImage: "camera")
                    }
                    .buttonStyle(.plain)
                    .disabled(viewModel.isUploading)
                }
                let pickText: String? = viewModel.isUploading ? nil
                    : (viewModel.media == nil ? "Add a photo" : "Change photo")
                PhotosPicker(selection: $selection, matching: .images, photoLibrary: .shared()) {
                    MarketingDropZone(text: pickText, systemImage: "photo.on.rectangle")
                }
                .buttonStyle(.plain)
                .disabled(viewModel.isUploading)
            }

            if let error = viewModel.mediaError {
                Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
            }
        }
        .onChange(of: selection) { _, item in
            guard let item else { return }
            Task {
                if let data = try? await item.loadTransferable(type: Data.self),
                   let image = UIImage(data: data) {
                    await viewModel.upload(image)
                } else {
                    viewModel.mediaError = "That photo couldn't be read."
                }
                selection = nil
            }
        }
    }

}

/// One dashed photo zone; nil text is the upload in flight.
struct MarketingDropZone: View {
    let text: String?
    let systemImage: String

    var body: some View {
        Group {
            if let text {
                Label(text, systemImage: systemImage)
                    .font(.cavnarBody(15, weight: 700))
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
            } else {
                CavnarShimmerText(text: "Uploading\u{2026}")
            }
        }
        .foregroundStyle(Color.cavnarEmber2)
        .frame(maxWidth: .infinity)
        .frame(minHeight: 44)
        .padding(.horizontal, 10)
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
            .strokeBorder(Color.cavnarEmber2.opacity(0.5), style: StrokeStyle(lineWidth: 1, dash: [5, 4])))
        .contentShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }
}

// MARK: - Preview

/// What the post will look like where it lands. You used to find out a
/// caption was over the limit from Meta, after pressing Post.
struct MarketingPreviewSheet: View {
    let platform: String
    /// Named `text`, not `body` — `body` is SwiftUI's own.
    let text: String
    let ctaType: String?
    let viewModel: MarketingComposeViewModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let preview = viewModel.preview {
                        problems(preview)
                        card(preview)
                        counts(preview)
                    } else if viewModel.isPreviewing {
                        CavnarWorkingLine().padding(.vertical, 30)
                    }
                }
                .padding(20)
            }
            // accountSheetChrome, not a bare Button("Done"): every other
            // sheet in the app closes with the same ember chevron in the
            // leading corner, and this was the one left using an unstyled
            // system text button in the trailing one.
            .accountSheetChrome("Preview")
            .task { await viewModel.loadPreview(platform: platform, body: text, ctaType: ctaType) }
        }
    }

    @ViewBuilder
    private func problems(_ preview: MarketingPreview) -> some View {
        if preview.ready {
            Label("Ready to post", systemImage: "checkmark.seal")
                .font(.cavnarBody(16, weight: 600))
                .foregroundStyle(Color.cavnarGreen)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(preview.problems, id: \.self) { problem in
                    Label(problem, systemImage: "exclamationmark.triangle")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .background(Color.cavnarPaper2)
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
        }
    }

    /// A rough stand-in for the real post, not a pixel-perfect mock — the
    /// useful part is the fold, since a caption that loses people is one
    /// whose first line said nothing.
    private func card(_ preview: MarketingPreview) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            if let url = preview.imageURL, let parsed = URL(string: url) {
                AsyncImage(url: parsed) { image in
                    image.resizable().aspectRatio(contentMode: .fill)
                } placeholder: {
                    Color.cavnarPaper2
                }
                .frame(maxWidth: .infinity)
                .frame(height: 220)
                .clipped()
            }
            VStack(alignment: .leading, spacing: 6) {
                Text(preview.visibleBeforeMore)
                    .font(.cavnarBody(16))
                    .foregroundStyle(Color.cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                if preview.truncated {
                    Text("… more")
                        .font(.cavnarBody(15, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                    Text("Everything after this is hidden until someone taps.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            .padding(14)
        }
        .background(Color.cavnarPaper)
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).stroke(Color.cavnarPaper3, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }

    private func counts(_ preview: MarketingPreview) -> some View {
        HStack(spacing: 0) {
            tile(value: "\(preview.characters)", label: "Characters",
                 tint: preview.overLimit ? Color.cavnarRed : Color.cavnarInk)
            Divider()
            tile(value: preview.limit.map { "\($0)" } ?? "—", label: "Limit", tint: Color.cavnarInk)
            Divider()
            tile(value: "\(preview.hashtagCount)", label: "Hashtags", tint: Color.cavnarInk)
        }
        .cavnarCard()
    }

    private func tile(value: String, label: String, tint: Color) -> some View {
        VStack(spacing: 4) {
            Text(value).font(.cavnarNumber(22, weight: 500)).foregroundStyle(tint)
            Text(label).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Schedule

/// Pick a slot. Everything this module did was generate-now/post-now, and an
/// owner does admin at 11pm for a post that belongs on Tuesday at lunch.
struct MarketingScheduleSheet: View {
    let platform: String
    let text: String
    let topic: String
    let contentType: String?
    let ctaType: String?
    let ctaURL: String?
    let viewModel: MarketingComposeViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var when = Date().addingTimeInterval(3600)
    @State private var clock = RestaurantClock.timeZone

    /// "Chicago", from the zone's identifier.
    private var clockName: String {
        clock.identifier.split(separator: "/").last.map { $0.replacingOccurrences(of: "_", with: " ") }
            ?? clock.identifier
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Text("Cavnar AI will publish this to \(platform.capitalized) for you. Times are your restaurant's own clock (\(clockName)).")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    // Shown and picked on the restaurant's clock, the same
                    // one localStamp writes the slot in — so 11:00 here is
                    // 11:00 in the dining room wherever the phone is.
                    DatePicker("When", selection: $when, in: Date()...,
                               displayedComponents: [.date, .hourAndMinute])
                        .datePickerStyle(.graphical)
                        .tint(Color.cavnarEmber)
                        .environment(\.timeZone, clock)

                    Button {
                        Task {
                            await viewModel.schedule(platform: platform, body: text, topic: topic,
                                                     contentType: contentType, ctaType: ctaType,
                                                     ctaURL: ctaURL, at: when)
                            if viewModel.scheduleError == nil { dismiss() }
                        }
                    } label: {
                        if viewModel.isScheduling {
                            CavnarShimmerText(text: "Scheduling…")
                        } else {
                            Text("Schedule it").frame(maxWidth: .infinity)
                        }
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .disabled(viewModel.isScheduling)

                    if let error = viewModel.scheduleError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Schedule")
            .task {
                await viewModel.learnRestaurantClock()
                clock = RestaurantClock.timeZone
            }
        }
    }
}

/// The queue itself.
struct MarketingQueueView: View {
    let viewModel: MarketingComposeViewModel
    /// The post whose Cancel was tapped, awaiting the confirm.
    @State private var postToCancel: ScheduledPost?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if viewModel.scheduled.isEmpty {
                    Text("Nothing queued. Write a post, then choose Schedule instead of posting it now.")
                        .font(.cavnarBody(16))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 40)
                }
                ForEach(viewModel.scheduled) { post in
                    row(post)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
        }
        .cavnarModuleBackground()
        .navigationTitle("Scheduled")
        .toolbar { cavnarTitleToolbar("Scheduled") }
        .cavnarEmberRefreshable { await viewModel.loadScheduled() }
        .task { await viewModel.loadScheduled() }
        .confirmationDialog("Cancel this post?",
                            isPresented: Binding(get: { postToCancel != nil }, set: { if !$0 { postToCancel = nil } }),
                            titleVisibility: .visible, presenting: postToCancel) { post in
            Button("Cancel the post", role: .destructive) {
                Task { await viewModel.cancel(post) }
            }
            Button("Keep it", role: .cancel) {}
        } message: { post in
            Text("It won\u{2019}t go to \(post.platform.capitalized) at \(post.whenLabel).")
        }
    }

    private func row(_ post: ScheduledPost) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(post.whenLabel)
                    .font(.cavnarBody(15, weight: 700))
                    .foregroundStyle(Color.cavnarEmber)
                Spacer()
                Text(post.statusLabel)
                    .font(.cavnarBody(15, weight: 700))
                    .foregroundStyle(statusColor(post.status))
            }
            Text(post.platform.capitalized)
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk3)
            Text(post.body)
                .font(.cavnarBody(16))
                .foregroundStyle(Color.cavnarInk)
                .lineLimit(4)
                .fixedSize(horizontal: false, vertical: true)
            if let error = post.error, !error.isEmpty {
                Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
            }
            if post.isPending {
                Button(role: .destructive) {
                    Haptic.selection()
                    postToCancel = post
                } label: {
                    Text("Cancel this post")
                        .font(.cavnarBody(15, weight: 600))
                        .foregroundStyle(Color.cavnarEmber)
                }
                .padding(.top, 2)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "posted": return .cavnarGreen
        case "failed": return .cavnarRed
        case "cancelled": return .cavnarInk3
        default: return .cavnarInk
        }
    }
}

// MARK: - Drafts

/// Saved copy, and the approval step in front of publishing.
struct MarketingDraftsView: View {
    let viewModel: MarketingComposeViewModel
    var onUse: ((MarketingDraft) -> Void)?
    /// The draft whose trash was tapped, awaiting the confirm.
    @State private var draftToDelete: MarketingDraft?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if viewModel.drafts.isEmpty {
                    Text("Nothing saved. Generate a post and choose Save draft — it'll be here whenever you come back.")
                        .font(.cavnarBody(16))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 40)
                }
                if let error = viewModel.draftError {
                    Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                }
                ForEach(viewModel.drafts) { draft in
                    row(draft)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
        }
        .cavnarModuleBackground()
        .navigationTitle("Drafts")
        .toolbar { cavnarTitleToolbar("Drafts") }
        .cavnarEmberRefreshable { await viewModel.loadDrafts() }
        .task { await viewModel.loadDrafts() }
        .confirmationDialog("Delete this draft?",
                            isPresented: Binding(get: { draftToDelete != nil }, set: { if !$0 { draftToDelete = nil } }),
                            titleVisibility: .visible, presenting: draftToDelete) { draft in
            Button("Delete draft", role: .destructive) {
                Task { await viewModel.deleteDraft(draft) }
            }
            Button("Cancel", role: .cancel) {}
        }
    }

    private func row(_ draft: MarketingDraft) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(draft.topic?.isEmpty == false ? draft.topic! : "Untitled")
                    .font(.cavnarBody(16, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                Text(draft.statusLabel)
                    .font(.cavnarBody(15, weight: 700))
                    .foregroundStyle(draft.isApproved ? Color.cavnarGreen
                                     : (draft.isExpired ? Color.cavnarInk3 : Color.cavnarEmber2))
            }
            Text(draft.body)
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk3)
                .lineLimit(4)
                .fixedSize(horizontal: false, vertical: true)
            // Retired server-side: a quiet-night piece whose night passed
            // before anyone approved it. Said once, muted, and nothing below
            // offers to release it.
            if let note = draft.expiredNote {
                Text(note)
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let who = draft.createdByName {
                Text(draft.isApproved && draft.approvedByName != nil
                     ? "Written by \(who) · approved by \(draft.approvedByName!)"
                     : "Written by \(who)")
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
            }

            HStack(spacing: 14) {
                // "Open" puts it in the composer — the road to Post, Schedule
                // and Send — so an expired draft doesn't get it. Delete stays.
                if let onUse, draft.canOpenInComposer {
                    Button {
                        Haptic.selection()
                        onUse(draft)
                    } label: {
                        Text("Open").font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarEmber)
                    }
                }
                if draft.canApprove {
                    Button {
                        Task { await viewModel.approve(draft) }
                    } label: {
                        Text("Approve").font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarEmber)
                    }
                }
                Spacer()
                Button {
                    Haptic.selection()
                    draftToDelete = draft
                } label: {
                    Image(systemName: "trash").foregroundStyle(Color.cavnarEmber)
                }
            }
            .padding(.top, 2)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }
}
