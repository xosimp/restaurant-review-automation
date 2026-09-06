import PhotosUI
import SwiftUI

// MARK: - Photo

/// Pick a photo from the camera roll and attach it. This is the piece that
/// makes Instagram publishing usable at all from a phone: the app used to ask
/// for a public image URL, typed by hand, for a picture sitting in Photos.
struct MarketingPhotoPicker: View {
    let viewModel: MarketingComposeViewModel
    @State private var selection: PhotosPickerItem?

    var body: some View {
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
                            .font(.cavnarBody(16.5, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        if let w = media.width, let h = media.height {
                            (Text("\(w)").font(.cavnarNumber(16)) + Text(" × ")
                                + Text("\(h)").font(.cavnarNumber(16)))
                                .font(.cavnarBody(16))
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

            PhotosPicker(selection: $selection, matching: .images, photoLibrary: .shared()) {
                if viewModel.isUploading {
                    CavnarShimmerText(text: "Uploading…")
                } else {
                    Label(viewModel.media == nil ? "Add a photo" : "Change photo",
                          systemImage: "photo.on.rectangle.angled")
                        .frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(viewModel.isUploading)

            if let error = viewModel.mediaError {
                Text(error).font(.cavnarBody(16)).foregroundStyle(Color.cavnarRed)
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
            .cavnarModuleBackground()
            .navigationTitle("Preview")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Preview")
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }.tint(Color.cavnarEmber)
                }
            }
            .task { await viewModel.loadPreview(platform: platform, body: text, ctaType: ctaType) }
        }
    }

    @ViewBuilder
    private func problems(_ preview: MarketingPreview) -> some View {
        if preview.ready {
            Label("Ready to post", systemImage: "checkmark.seal")
                .font(.cavnarBody(16.5, weight: 600))
                .foregroundStyle(Color.cavnarGreen)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(preview.problems, id: \.self) { problem in
                    Label(problem, systemImage: "exclamationmark.triangle")
                        .font(.cavnarBody(16))
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
                    .font(.cavnarBody(16.5))
                    .foregroundStyle(Color.cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                if preview.truncated {
                    Text("… more")
                        .font(.cavnarBody(16, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                    Text("Everything after this is hidden until someone taps.")
                        .font(.cavnarBody(16))
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
            Text(label).font(.cavnarBody(16)).foregroundStyle(Color.cavnarInk3)
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

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Text("Cavnar AI will publish this to \(platform.capitalized) for you. Times are your restaurant's own clock.")
                        .font(.cavnarBody(16))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    DatePicker("When", selection: $when, in: Date()...,
                               displayedComponents: [.date, .hourAndMinute])
                        .datePickerStyle(.graphical)
                        .tint(Color.cavnarEmber)

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
                        Text(error).font(.cavnarBody(16)).foregroundStyle(Color.cavnarRed)
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Schedule")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Schedule")
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }.tint(Color.cavnarInk3)
                }
            }
        }
    }
}

/// The queue itself.
struct MarketingQueueView: View {
    let viewModel: MarketingComposeViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if viewModel.scheduled.isEmpty {
                    Text("Nothing queued. Write a post, then choose Schedule instead of posting it now.")
                        .font(.cavnarBody(16.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 40)
                }
                ForEach(viewModel.scheduled) { post in
                    row(post)
                }
            }
            .padding(20)
        }
        .cavnarModuleBackground()
        .navigationTitle("Scheduled")
        .toolbar { cavnarTitleToolbar("Scheduled") }
        .cavnarEmberRefreshable { await viewModel.loadScheduled() }
        .task { await viewModel.loadScheduled() }
    }

    private func row(_ post: ScheduledPost) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(post.whenLabel)
                    .font(.cavnarBody(16, weight: 700))
                    .foregroundStyle(Color.cavnarEmber)
                Spacer()
                Text(post.statusLabel)
                    .font(.cavnarBody(16, weight: 700))
                    .foregroundStyle(statusColor(post.status))
            }
            Text(post.platform.capitalized)
                .font(.cavnarBody(16))
                .foregroundStyle(Color.cavnarInk3)
            Text(post.body)
                .font(.cavnarBody(16.5))
                .foregroundStyle(Color.cavnarInk)
                .lineLimit(4)
                .fixedSize(horizontal: false, vertical: true)
            if let error = post.error, !error.isEmpty {
                Text(error).font(.cavnarBody(16)).foregroundStyle(Color.cavnarRed)
            }
            if post.isPending {
                Button(role: .destructive) {
                    Haptic.selection()
                    Task { await viewModel.cancel(post) }
                } label: {
                    Text("Cancel this post")
                        .font(.cavnarBody(16, weight: 600))
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

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if viewModel.drafts.isEmpty {
                    Text("Nothing saved. Generate a post and choose Save draft — it'll be here whenever you come back.")
                        .font(.cavnarBody(16.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 40)
                }
                if let error = viewModel.draftError {
                    Text(error).font(.cavnarBody(16)).foregroundStyle(Color.cavnarRed)
                }
                ForEach(viewModel.drafts) { draft in
                    row(draft)
                }
            }
            .padding(20)
        }
        .cavnarModuleBackground()
        .navigationTitle("Drafts")
        .toolbar { cavnarTitleToolbar("Drafts") }
        .cavnarEmberRefreshable { await viewModel.loadDrafts() }
        .task { await viewModel.loadDrafts() }
    }

    private func row(_ draft: MarketingDraft) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(draft.topic?.isEmpty == false ? draft.topic! : "Untitled")
                    .font(.cavnarBody(16.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                Text(draft.isApproved ? "Approved" : "Draft")
                    .font(.cavnarBody(16, weight: 700))
                    .foregroundStyle(draft.isApproved ? Color.cavnarGreen : Color.cavnarEmber2)
            }
            Text(draft.body)
                .font(.cavnarBody(16))
                .foregroundStyle(Color.cavnarInk3)
                .lineLimit(4)
                .fixedSize(horizontal: false, vertical: true)

            if let who = draft.createdByName {
                Text(draft.isApproved && draft.approvedByName != nil
                     ? "Written by \(who) · approved by \(draft.approvedByName!)"
                     : "Written by \(who)")
                    .font(.cavnarBody(16))
                    .foregroundStyle(Color.cavnarInk3)
            }

            HStack(spacing: 14) {
                if let onUse {
                    Button {
                        Haptic.selection()
                        onUse(draft)
                    } label: {
                        Text("Open").font(.cavnarBody(16, weight: 600)).foregroundStyle(Color.cavnarEmber)
                    }
                }
                if !draft.isApproved {
                    Button {
                        Task { await viewModel.approve(draft) }
                    } label: {
                        Text("Approve").font(.cavnarBody(16, weight: 600)).foregroundStyle(Color.cavnarEmber)
                    }
                }
                Spacer()
                Button {
                    Haptic.selection()
                    Task { await viewModel.deleteDraft(draft) }
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
