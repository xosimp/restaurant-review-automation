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
                CavnarKicker("Your photos")
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
                            .cavnarText(.label)
                        if let w = media.width, let h = media.height {
                            HomeMixedText.make("\(w) × \(h)", role: .caption)
                        }
                    }
                    Spacer()
                    Button {
                        Haptic.light()
                        viewModel.clearMedia()
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.cavnar(.body))
                            .foregroundStyle(Color.cavnarInk2)
                            // 44pt to hit, a label to hear (re-audit 10/8/26 L10).
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Remove photo")
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
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
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
                    .font(.cavnar(.label))
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
                    } else if let error = viewModel.previewError {
                        // A preview that couldn't load says so, with the way
                        // to try again — it used to be an empty sheet (M9).
                        VStack(alignment: .leading, spacing: CavnarSpace.s) {
                            Text(error)
                                .cavnarText(.secondary, color: .cavnarRedText)
                                .fixedSize(horizontal: false, vertical: true)
                            Button {
                                Haptic.light()
                                Task { await viewModel.loadPreview(platform: platform, body: text, ctaType: ctaType) }
                            } label: {
                                Text("Try again").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                        }
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
                .cavnarText(.label, color: .cavnarGreen)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(preview.problems, id: \.self) { problem in
                    Label(problem, systemImage: "exclamationmark.triangle")
                        .cavnarText(.secondary, color: .cavnarRedText)
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
                    .cavnarText(.body, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                if preview.truncated {
                    Text("… more")
                        .cavnarText(.label, color: .cavnarInk2)
                    Text("Everything after this is hidden until someone taps.")
                        .cavnarText(.caption)
                }
            }
            .padding(14)
        }
        .background(Color.cavnarPaper)
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).stroke(Color.cavnarPaper3, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }

    /// One line, not three tiles (re-audit 10/8/26 W11): "212 of 2,200
    /// characters · 4 hashtags", red when over.
    private func counts(_ preview: MarketingPreview) -> some View {
        CavnarMixedText(Self.countLine(preview), role: .secondary,
                        color: preview.overLimit ? .cavnarRedText : .cavnarInk2)
    }

    static func countLine(_ preview: MarketingPreview) -> String {
        let chars = preview.limit.map { "\(preview.characters.formatted()) of \($0.formatted()) characters" }
            ?? "\(preview.characters.formatted()) characters"
        let tags = "\(preview.hashtagCount) \(preview.hashtagCount == 1 ? "hashtag" : "hashtags")"
        return "\(chars) \u{00B7} \(tags)"
    }
}

// MARK: - Schedule

/// Pick a slot. Everything this module did was generate-now/post-now, and an
/// owner does admin at 11pm for a post that belongs on Tuesday at lunch.
struct MarketingScheduleSheet: View {
    /// Every channel the post goes to (re-audit 10/8/26 H4): the selected
    /// targets, not "Instagram if connected" — one scheduled post each.
    let platforms: [String]
    let text: String
    let topic: String
    let contentType: String?
    let ctaType: String?
    let ctaURL: String?
    let viewModel: MarketingComposeViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var when = Date().addingTimeInterval(3600)
    @State private var clock = RestaurantClock.timeZone
    /// Channels already scheduled by this sheet, so a retry after one
    /// failed never schedules the others twice.
    @State private var done: Set<String> = []

    private var destinations: String {
        MarketingViewModel.channelList(platforms.map { $0 == "google" ? "Google" : $0.capitalized })
    }

    /// "Chicago", from the zone's identifier.
    private var clockName: String {
        clock.identifier.split(separator: "/").last.map { $0.replacingOccurrences(of: "_", with: " ") }
            ?? clock.identifier
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Text("Cavnar AI will publish this to \(destinations) for you. Times are your restaurant's own clock (\(clockName)).")
                        .cavnarText(.body)
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
                            var failures: [String] = []
                            for platform in platforms where !done.contains(platform) {
                                await viewModel.schedule(platform: platform, body: text, topic: topic,
                                                         contentType: contentType, ctaType: ctaType,
                                                         ctaURL: ctaURL, at: when)
                                if let error = viewModel.scheduleError {
                                    failures.append("\(platform.capitalized): \(error)")
                                } else {
                                    done.insert(platform)
                                }
                            }
                            if failures.isEmpty {
                                dismiss()
                            } else {
                                viewModel.scheduleError = failures.joined(separator: "\n")
                            }
                        }
                    } label: {
                        if viewModel.isScheduling {
                            CavnarShimmerText(text: "Scheduling…")
                        } else {
                            Text("Schedule it").frame(maxWidth: .infinity)
                        }
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isScheduling || platforms.isEmpty))
                    .disabled(viewModel.isScheduling || platforms.isEmpty)

                    if let error = viewModel.scheduleError {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
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

/// "Scheduled & sent" (readability round 10/8/26 #58): what is still
/// coming (Upcoming, soonest first) and what went out — posted, didn't go
/// out, cancelled — split by each post's own status. The queue was mostly a
/// record of what had already gone.
struct MarketingQueueView: View {
    let viewModel: MarketingComposeViewModel
    /// The post whose Cancel was tapped, awaiting the confirm.
    @State private var postToCancel: ScheduledPost?

    /// Still waiting to go, soonest first.
    static func upcoming(_ posts: [ScheduledPost]) -> [ScheduledPost] {
        posts.filter(\.isPending).sorted { $0.scheduledFor < $1.scheduledFor }
    }

    /// Everything else, newest first.
    static func wentOut(_ posts: [ScheduledPost]) -> [ScheduledPost] {
        posts.filter { !$0.isPending }.sorted { $0.scheduledFor > $1.scheduledFor }
    }

    var body: some View {
        let upcoming = Self.upcoming(viewModel.scheduled)
        let wentOut = Self.wentOut(viewModel.scheduled)
        ScrollView {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                // A cancel that didn't go through, said (re-audit M18).
                if let error = viewModel.scheduleError {
                    Text(error)
                        .cavnarText(.secondary, color: .cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }
                CavnarKicker("Upcoming")
                if upcoming.isEmpty {
                    Text("Nothing scheduled. Write a post, then choose Schedule instead of posting it now.")
                        .cavnarText(.body)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(upcoming) { post in
                    row(post)
                }
                if !wentOut.isEmpty {
                    CavnarKicker("Went out")
                        .padding(.top, CavnarSpace.s)
                    ForEach(wentOut) { post in
                        row(post)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(CavnarSpace.gutter)
        }
        .cavnarModuleBackground()
        .navigationTitle("Scheduled & sent")
        .toolbar { cavnarTitleToolbar("Scheduled & sent") }
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
        VStack(alignment: .leading, spacing: CavnarSpace.xxs + 2) {
            HStack(alignment: .firstTextBaseline) {
                HomeMixedText.make(post.whenLabel, role: .label)
                Spacer()
                Text(post.statusLabel)
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(statusColor(post.status))
            }
            Text(post.platform.capitalized)
                .cavnarText(.caption)
            Text(post.body)
                .cavnarText(.body)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            if let error = post.error, !error.isEmpty {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
            }
            if post.isPending {
                Button(role: .destructive) {
                    Haptic.selection()
                    postToCancel = post
                } label: {
                    Text("Cancel this post")
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(CavnarSpace.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "posted": return .cavnarGreen
        case "failed": return .cavnarRedText
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
                        .cavnarText(.body)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 40)
                }
                if let error = viewModel.draftError {
                    Text(error).cavnarText(.secondary, color: .cavnarRedText)
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
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(alignment: .firstTextBaseline) {
                Text(draft.topic?.isEmpty == false ? draft.topic! : "Untitled")
                    .cavnarText(.label)
                Spacer()
                Text(draft.statusLabel)
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(draft.isApproved ? Color.cavnarGreen
                                     : (draft.isExpired ? Color.cavnarInk3 : Color.cavnarEmber2))
            }
            Text(draft.body)
                .cavnarText(.body)
                .lineLimit(4)
                .fixedSize(horizontal: false, vertical: true)
            // Retired server-side: a quiet-night piece whose night passed
            // before anyone approved it. Said once, muted, and nothing below
            // offers to release it.
            if let note = draft.expiredNote {
                Text(note)
                    .cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let who = draft.createdByName {
                Text(draft.isApproved && draft.approvedByName != nil
                     ? "Written by \(who) · approved by \(draft.approvedByName!)"
                     : "Written by \(who)")
                    .cavnarText(.caption)
            }

            // Real 44pt buttons (readability round #19): Approve first, as
            // the decision this screen exists for; Open; the trash last.
            HStack(spacing: CavnarSpace.xs) {
                if draft.canApprove {
                    Button {
                        Haptic.light()
                        Task { await viewModel.approve(draft) }
                    } label: {
                        Text("Approve").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSoftButtonStyle())
                }
                // "Open" puts it in the composer — the road to Post, Schedule
                // and Send — so an expired draft doesn't get it. Delete stays.
                if let onUse, draft.canOpenInComposer {
                    Button {
                        Haptic.selection()
                        onUse(draft)
                    } label: {
                        // A text or an email opens in the Campaign Studio,
                        // where it is sent (M3).
                        Text(draft.isGuestMessage ? "Open in Campaigns" : "Open").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
                Spacer(minLength: 0)
                Button {
                    Haptic.selection()
                    draftToDelete = draft
                } label: {
                    Image(systemName: "trash")
                        .font(.cavnar(.body))
                        .foregroundStyle(Color.cavnarInk2)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Delete draft")
            }
            .padding(.top, 2)
        }
        .padding(CavnarSpace.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }
}
