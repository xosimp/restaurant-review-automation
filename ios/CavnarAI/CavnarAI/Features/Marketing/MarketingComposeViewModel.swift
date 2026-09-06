import Foundation
import Observation
import UIKit

/// The compose surface: a photo, a preview, a queue and a drafts shelf.
///
/// Kept apart from MarketingViewModel, which owns generating copy. Composing
/// is what happens to that copy afterwards — attaching a picture, checking it
/// against the platform, deciding whether it goes out now or on Tuesday — and
/// folding it into the generator would have made one object that does both
/// badly.
@Observable
@MainActor
final class MarketingComposeViewModel {
    // Photo
    var media: MarketingMedia?
    var recentMedia: [MarketingMedia] = []
    var isUploading = false
    var mediaError: String?

    // Preview
    var preview: MarketingPreview?
    var isPreviewing = false

    // Queue
    var scheduled: [ScheduledPost] = []
    var isScheduling = false
    var scheduleError: String?
    var didSchedule = false

    // Drafts
    var drafts: [MarketingDraft] = []
    var isSavingDraft = false
    var draftError: String?
    var savedDraftID: Int?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    var pendingCount: Int { scheduled.filter(\.isPending).count }

    // MARK: - Photo

    private struct UploadBody: Encodable {
        let imageBase64: String
        enum CodingKeys: String, CodingKey { case imageBase64 = "image_base64" }
    }

    /// Re-encoded to JPEG here as well as on the server — a 12-megapixel HEIC
    /// base64'd is a several-megabyte request body over a restaurant's wifi,
    /// and the server is going to downscale it anyway.
    func upload(_ image: UIImage) async {
        isUploading = true
        mediaError = nil
        defer { isUploading = false }
        guard let data = image.jpegData(compressionQuality: 0.85) else {
            mediaError = "That photo couldn't be read."
            return
        }
        do {
            let uploaded: MarketingMedia = try await client.send(
                "/mobile/api/marketing/media", method: .post,
                body: UploadBody(imageBase64: data.base64EncodedString())
            )
            media = uploaded
            Haptic.success()
            await loadMedia()
        } catch let error as APIClient.APIError {
            mediaError = error.message
        } catch {
            mediaError = "Couldn't upload that photo."
        }
    }

    private struct MediaListResponse: Decodable {
        let ok: Bool
        let media: [MarketingMedia]
    }

    func loadMedia() async {
        let response: MediaListResponse? = try? await client.send("/mobile/api/marketing/media")
        recentMedia = response?.media ?? []
    }

    func clearMedia() {
        media = nil
        preview = nil
    }

    // MARK: - Preview

    private struct PreviewBody: Encodable {
        let platform: String
        let body: String
        let mediaId: Int?
        let ctaType: String?

        enum CodingKeys: String, CodingKey {
            case platform, body
            case mediaId = "media_id"
            case ctaType = "cta_type"
        }
    }

    func loadPreview(platform: String, body: String, ctaType: String?) async {
        guard !body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            preview = nil
            return
        }
        isPreviewing = true
        defer { isPreviewing = false }
        preview = try? await client.send(
            "/mobile/api/marketing/preview", method: .post,
            body: PreviewBody(platform: platform, body: body, mediaId: media?.id, ctaType: ctaType)
        )
    }

    // MARK: - Queue

    private struct ScheduleBody: Encodable {
        let platform: String
        let body: String
        let topic: String
        let contentType: String?
        let mediaId: Int?
        let ctaType: String?
        let ctaUrl: String?
        let scheduledFor: String

        enum CodingKeys: String, CodingKey {
            case platform, body, topic
            case contentType = "content_type"
            case mediaId = "media_id"
            case ctaType = "cta_type"
            case ctaUrl = "cta_url"
            case scheduledFor = "scheduled_for"
        }
    }

    private struct OKResponse: Decodable {
        let ok: Bool
        let id: Int?
        let error: String?
    }

    private struct ScheduleListResponse: Decodable {
        let ok: Bool
        let posts: [ScheduledPost]
    }

    /// The slot is sent as the restaurant's own wall clock, not UTC — the
    /// owner picking 11am means 11am in their dining room, and the backend
    /// stores and compares it that way.
    static func localStamp(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f.string(from: date)
    }

    func schedule(platform: String, body: String, topic: String, contentType: String?,
                  ctaType: String?, ctaURL: String?, at date: Date) async {
        isScheduling = true
        scheduleError = nil
        defer { isScheduling = false }
        do {
            let response: OKResponse = try await client.send(
                "/mobile/api/marketing/schedule", method: .post,
                body: ScheduleBody(platform: platform, body: body, topic: topic,
                                   contentType: contentType, mediaId: media?.id,
                                   ctaType: ctaType, ctaUrl: ctaURL,
                                   scheduledFor: Self.localStamp(date))
            )
            if response.ok {
                Haptic.success()
                didSchedule = true
                await loadScheduled()
            } else {
                scheduleError = response.error ?? "Couldn't schedule that."
            }
        } catch let error as APIClient.APIError {
            scheduleError = error.message
        } catch {
            scheduleError = "Couldn't schedule that."
        }
    }

    func loadScheduled() async {
        let response: ScheduleListResponse? = try? await client.send("/mobile/api/marketing/schedule")
        scheduled = response?.posts ?? []
    }

    func cancel(_ post: ScheduledPost) async {
        _ = try? await client.send("/mobile/api/marketing/schedule/\(post.id)",
                                   method: .delete) as OKResponse
        await loadScheduled()
    }

    // MARK: - Drafts

    private struct DraftBody: Encodable {
        let id: Int?
        let body: String
        let topic: String?
        let contentType: String?
        let mediaId: Int?

        enum CodingKeys: String, CodingKey {
            case id, body, topic
            case contentType = "content_type"
            case mediaId = "media_id"
        }
    }

    private struct DraftListResponse: Decodable {
        let ok: Bool
        let drafts: [MarketingDraft]
    }

    func saveDraft(body: String, topic: String, contentType: String?, id: Int? = nil) async {
        isSavingDraft = true
        draftError = nil
        defer { isSavingDraft = false }
        do {
            let response: OKResponse = try await client.send(
                "/mobile/api/marketing/drafts", method: .post,
                body: DraftBody(id: id, body: body, topic: topic.isEmpty ? nil : topic,
                                contentType: contentType, mediaId: media?.id)
            )
            if response.ok {
                Haptic.success()
                savedDraftID = response.id
                await loadDrafts()
            } else {
                draftError = response.error ?? "Couldn't save that draft."
            }
        } catch let error as APIClient.APIError {
            draftError = error.message
        } catch {
            draftError = "Couldn't save that draft."
        }
    }

    func loadDrafts() async {
        let response: DraftListResponse? = try? await client.send("/mobile/api/marketing/drafts")
        drafts = response?.drafts ?? []
    }

    /// Owner-only on the server, so the error here is the real rule rather
    /// than a hidden button.
    func approve(_ draft: MarketingDraft) async {
        do {
            let response: OKResponse = try await client.send(
                "/mobile/api/marketing/drafts/\(draft.id)/approve", method: .post)
            if response.ok {
                Haptic.success()
                await loadDrafts()
            } else {
                draftError = response.error
            }
        } catch let error as APIClient.APIError {
            draftError = error.message
        } catch {
            draftError = "Couldn't approve that draft."
        }
    }

    func deleteDraft(_ draft: MarketingDraft) async {
        _ = try? await client.send("/mobile/api/marketing/drafts/\(draft.id)",
                                   method: .delete) as OKResponse
        drafts.removeAll { $0.id == draft.id }
    }
}
