import SwiftUI
import UIKit

/// The Share extension's sheet (parity audit #95): what was shared, where it
/// goes, and one Send. Sending is the confirm — nothing leaves the phone
/// until the owner taps it, and what it does is read the invoice into
/// Food Cost for the owner to check; no price changes until they apply it.
@objc(ShareViewController)
final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        overrideUserInterfaceStyle = .dark
        let providers = (extensionContext?.inputItems as? [NSExtensionItem] ?? [])
            .flatMap { $0.attachments ?? [] }
        let model = ShareModel(providers: providers)
        model.onDone = { [weak self] in
            self?.extensionContext?.completeRequest(returningItems: nil)
        }
        model.onCancel = { [weak self] in
            self?.extensionContext?.cancelRequest(withError: NSError(domain: "ai.cavnar.share", code: 0))
        }
        let host = UIHostingController(rootView: ShareSheetView(model: model).environment(\.colorScheme, .dark))
        addChild(host)
        host.view.translatesAutoresizingMaskIntoConstraints = false
        host.view.backgroundColor = .clear
        view.addSubview(host.view)
        NSLayoutConstraint.activate([
            host.view.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            host.view.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            host.view.topAnchor.constraint(equalTo: view.topAnchor),
            host.view.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        host.didMove(toParent: self)
        Task { await model.load() }
    }
}

@Observable
@MainActor
final class ShareModel {
    enum Phase: Equatable {
        case loading
        case ready
        case sending(done: Int)
        case sent
        case failed(String)
    }

    let providers: [NSItemProvider]
    var items: [ShareUploader.Item] = []
    var phase: Phase = .loading
    var onDone: (() -> Void)?
    var onCancel: (() -> Void)?

    init(providers: [NSItemProvider]) { self.providers = providers }

    var signedIn: Bool { ShareUploader.session() != nil }
    /// The location it goes to, for a group's login (re-audit 10/8/26 #12).
    var destination: String? { ShareUploader.destinationLine(ShareUploader.session()) }

    func load() async {
        items = await ShareUploader.load(providers)
        phase = items.isEmpty ? .failed(ShareUploader.Failure.unreadable.sentence) : .ready
    }

    func send() async {
        guard let session = ShareUploader.session() else {
            phase = .failed(ShareUploader.Failure.notSignedIn.sentence)
            return
        }
        for (i, item) in items.enumerated() {
            phase = .sending(done: i)
            do {
                try await ShareUploader.send(item, session: session)
            } catch let failure as ShareUploader.Failure {
                phase = .failed(failure.sentence)
                return
            } catch {
                phase = .failed(ShareUploader.Failure.offline.sentence)
                return
            }
        }
        phase = .sent
        Haptic.success()
        try? await Task.sleep(for: .seconds(1.6))
        onDone?()
    }
}

struct ShareSheetView: View {
    @Bindable var model: ShareModel

    private var count: Int { model.items.count }
    private var what: String {
        count == 1 ? (model.items.first?.isPDF == true ? "1 PDF" : "1 photo") : "\(count) files"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("SEND TO CAVNAR AI")
                .font(.cavnarBody(12, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber)
            Text("Supplier invoice")
                .font(.cavnarHeadline(24))
                .foregroundStyle(Color.cavnarInk)
            if model.signedIn, let destination = model.destination {
                // A group owner's invoice lands at the location the app is
                // on: say which, before Send.
                Label(destination, systemImage: "mappin.and.ellipse")
                    .font(.cavnarBody(14, weight: 600))
                    .foregroundStyle(Color.cavnarInk2)
                    .lineLimit(1)
            }
            switch model.phase {
            case .loading:
                CavnarSkeletonBar(height: 3)
                    .accessibilityLabel("Reading what you shared")
            case .ready:
                Text(model.signedIn
                     ? "\(what) will be read into Food Cost \u{2192} Invoices, waiting for you to check. No price changes until you apply it in the app."
                     : ShareUploader.Failure.notSignedIn.sentence)
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            case .sending(let done):
                HStack(spacing: 6) {
                    Text("Sending").font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk2)
                    Text("\(done + 1) of \(count)").font(.cavnarNumber(15, weight: 600)).foregroundStyle(Color.cavnarInk)
                }
                CavnarSkeletonBar(height: 3)
            case .sent:
                Text("Sent. It will be waiting in Food Cost \u{2192} Invoices.")
                    .font(.cavnarBody(15, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
            case .failed(let why):
                Text(why)
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            HStack(spacing: 12) {
                Button("Cancel") { model.onCancel?() }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                Spacer(minLength: 0)
                if model.phase == .ready && model.signedIn {
                    Button("Send to Cavnar AI") { Task { await model.send() } }
                        .buttonStyle(CavnarPrimaryButtonStyle())
                }
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color.cavnarPaper.ignoresSafeArea())
    }
}
