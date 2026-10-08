import CryptoKit
import Foundation
import ImageIO
import Security
import UIKit
import UniformTypeIdentifiers

/// "Send to Cavnar AI" from Mail, Files or Photos (parity audit #95): each
/// shared PDF or photo is uploaded to POST /mobile/api/food-cost/invoices
/// — the route Food Cost's own Scan invoice uses — and read on the server
/// as a job (`async`), so it lands in Food Cost → Invoices "Waiting on you"
/// whether or not anyone is watching. Nothing is applied: every line still
/// waits for the owner to check it in the app.
///
/// The session comes from the keychain access group the app copies it into
/// (Keychain.mirrorSessionForExtensions); this target holds no other secret
/// and never signs in itself.
enum ShareUploader {
    struct SharedSession: Decodable {
        let token: String
        let baseURL: String
        /// The location the app's session is on, named for a login with
        /// more than one (Keychain.mirrorSessionLocation); nil otherwise.
        let restaurantId: Int?
        let locationName: String?
        enum CodingKeys: String, CodingKey {
            case token
            case baseURL = "base_url"
            case restaurantId = "restaurant_id"
            case locationName = "location_name"
        }
    }

    /// "Goes to North Ave." — where the upload lands, for a group's login;
    /// nil when there is only one place it could go.
    static func destinationLine(_ session: SharedSession?) -> String? {
        guard let name = session?.locationName?.trimmingCharacters(in: .whitespacesAndNewlines),
              !name.isEmpty else { return nil }
        return "Goes to \(name)"
    }

    /// The same item and group the app writes (Keychain.sharedSessionAccount).
    static let sharedSessionAccount = "cavnar.shared.session"
    static let route = "/mobile/api/food-cost/invoices"
    /// invoices.MAX_PDF_BYTES is 4.5 MB; a little under it.
    static let maxPDFBytes = Int(4.4 * 1024 * 1024)

    static var accessGroup: String? {
        guard let g = Bundle.main.object(forInfoDictionaryKey: "CavnarKeychainGroup") as? String,
              !g.isEmpty, !g.hasPrefix("$(") else { return nil }
        return g
    }

    static func session() -> SharedSession? {
        guard let group = accessGroup else { return nil }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: sharedSessionAccount,
            kSecAttrAccessGroup as String: group,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return try? JSONDecoder().decode(SharedSession.self, from: data)
    }

    /// Release talks to production only, whatever the copy says — the same
    /// rule AppEnvironment keeps for the app.
    static func baseURL(_ session: SharedSession) -> URL? {
        #if DEBUG
        return URL(string: session.baseURL)
        #else
        return URL(string: "https://dashboard.cavnar.ai")
        #endif
    }

    struct Item: Identifiable, Equatable {
        let id = UUID()
        let data: Data
        let filename: String
        let mimeType: String
        var isPDF: Bool { mimeType == "application/pdf" }
    }

    enum Failure: Error, Equatable {
        case notSignedIn
        case tooBig(String)
        case unreadable
        case server(String)
        case offline

        var sentence: String {
            switch self {
            case .notSignedIn: return "Open Cavnar AI and sign in on this iPhone first."
            case .tooBig(let name): return "\(name) is too big to read. Send a few pages at a time."
            case .unreadable: return "That file couldn\u{2019}t be read. Share a PDF or a photo of the invoice."
            case .server(let s): return s
            case .offline: return "Couldn\u{2019}t reach Cavnar AI. Check your connection and try again."
            }
        }
    }

    /// Reads the shared attachments: PDFs as they are, images as a JPEG at
    /// most 2,000 px on the long side (the app's own scan does the same).
    @MainActor
    static func load(_ providers: [NSItemProvider]) async -> [Item] {
        var out: [Item] = []
        for (i, p) in providers.enumerated() {
            if p.hasItemConformingToTypeIdentifier(UTType.pdf.identifier),
               let data = await loadData(p, type: .pdf) {
                out.append(Item(data: data, filename: p.suggestedName.map { $0 + ".pdf" } ?? "invoice-\(i + 1).pdf",
                                mimeType: "application/pdf"))
            } else if p.hasItemConformingToTypeIdentifier(UTType.image.identifier),
                      let raw = await loadData(p, type: .image), let jpeg = downscaledJPEG(raw) {
                out.append(Item(data: jpeg, filename: "invoice-\(i + 1).jpg", mimeType: "image/jpeg"))
            }
        }
        return out
    }

    @MainActor
    private static func loadData(_ provider: NSItemProvider, type: UTType) async -> Data? {
        await withCheckedContinuation { cont in
            _ = provider.loadDataRepresentation(for: type) { data, _ in cont.resume(returning: data) }
        }
    }

    static func downscaledJPEG(_ data: Data, maxSide: CGFloat = 2000) -> Data? {
        let noCache = [kCGImageSourceShouldCache: false] as CFDictionary
        guard let source = CGImageSourceCreateWithData(data as CFData, noCache) else { return nil }
        let options = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                       kCGImageSourceCreateThumbnailWithTransform: true,
                       kCGImageSourceThumbnailMaxPixelSize: maxSide] as CFDictionary
        guard let cg = CGImageSourceCreateThumbnailAtIndex(source, 0, options) else { return nil }
        return UIImage(cgImage: cg).jpegData(compressionQuality: 0.8)
    }

    /// The multipart request the route reads: the file in `file`, and
    /// `async=1` so the read runs as a job — the extension does not wait on
    /// a model. The Idempotency-Key is the app's own (path + SHA-256 of the
    /// file), so sharing the same invoice twice is read once.
    static func request(for item: Item, session: SharedSession) throws -> URLRequest {
        guard let base = baseURL(session),
              var comps = URLComponents(url: base.appendingPathComponent(String(route.dropFirst())),
                                        resolvingAgainstBaseURL: false) else { throw Failure.notSignedIn }
        comps.queryItems = [URLQueryItem(name: "async", value: "1")]
        guard let url = comps.url else { throw Failure.notSignedIn }
        var r = URLRequest(url: url)
        r.httpMethod = "POST"
        r.timeoutInterval = 60
        r.setValue("Bearer \(session.token)", forHTTPHeaderField: "Authorization")
        let boundary = "cavnar-\(UUID().uuidString)"
        r.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let digest = SHA256.hash(data: item.data).map { String(format: "%02x", $0) }.joined()
        r.setValue("\(route):\(digest)", forHTTPHeaderField: "Idempotency-Key")
        var body = Data()
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data("Content-Disposition: form-data; name=\"file\"; filename=\"\(item.filename)\"\r\n".utf8))
        body.append(Data("Content-Type: \(item.mimeType)\r\n\r\n".utf8))
        body.append(item.data)
        body.append(Data("\r\n--\(boundary)\r\n".utf8))
        body.append(Data("Content-Disposition: form-data; name=\"async\"\r\n\r\n1\r\n".utf8))
        body.append(Data("--\(boundary)--\r\n".utf8))
        r.httpBody = body
        return r
    }

    private struct Answer: Decodable {
        let ok: Bool?
        let error: String?
        let jobId: String?
        enum CodingKeys: String, CodingKey {
            case ok, error
            case jobId = "job_id"
        }
    }

    /// Sends one item. A 202 (the read started) or 200 is success.
    static func send(_ item: Item, session: SharedSession) async throws {
        if item.isPDF, item.data.count > maxPDFBytes { throw Failure.tooBig(item.filename) }
        let request = try request(for: item, session: session)
        let urlSession: URLSession
        if (request.url?.host ?? "").hasSuffix("cavnar.ai") {
            urlSession = URLSession(configuration: .ephemeral, delegate: PinnedSessionDelegate(), delegateQueue: nil)
        } else {
            urlSession = URLSession(configuration: .ephemeral)
        }
        defer { urlSession.finishTasksAndInvalidate() }
        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await urlSession.data(for: request)
        } catch {
            throw Failure.offline
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let answer = try? JSONDecoder().decode(Answer.self, from: data)
        if status == 401 { throw Failure.notSignedIn }
        guard (200...299).contains(status), answer?.ok != false else {
            throw Failure.server(answer?.error ?? "Cavnar AI couldn\u{2019}t take that invoice (\(status)).")
        }
    }
}
