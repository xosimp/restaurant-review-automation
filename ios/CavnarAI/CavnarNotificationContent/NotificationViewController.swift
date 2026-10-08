import SwiftUI
import UIKit
import UserNotifications
import UserNotificationsUI

/// The expanded view of a Cavnar AI notification (parity audit #36). A
/// review with a drafted reply had "Approve & post" on the lock screen and
/// nothing to read but the guest's review: the reply it published was never
/// shown. Long-pressing the notification now shows the reply itself (push.py
/// sends it as `draft`), and for the nightly report its headline in full.
///
/// Registered for CAVNAR_REVIEW_DRAFTED, CAVNAR_DSR and the lineup brief's
/// CAVNAR_LINEUP / CAVNAR_LINEUP_REVIEW (project.yml) — the brief Approve
/// sends to staff, read before it is (re-audit 10/8/26). Reads
/// the payload only — the extension never signs in and never calls the API;
/// the buttons under it are the app's own (PushManager). Dark only, like the
/// app.
final class NotificationViewController: UIViewController, UNNotificationContentExtension {
    private var host: UIHostingController<NotificationPreviewView>?

    override func viewDidLoad() {
        super.viewDidLoad()
        overrideUserInterfaceStyle = .dark
        view.backgroundColor = UIColor(Color.cavnarPaper)
    }

    func didReceive(_ notification: UNNotification) {
        let content = notification.request.content
        let preview = NotificationPreview(title: content.title, body: content.body,
                                          category: content.categoryIdentifier, userInfo: content.userInfo)
        let root = NotificationPreviewView(preview: preview)
        let controller: UIHostingController<NotificationPreviewView>
        if let host {
            host.rootView = root
            controller = host
        } else {
            controller = UIHostingController(rootView: root)
            controller.overrideUserInterfaceStyle = .dark
            controller.view.backgroundColor = .clear
            addChild(controller)
            controller.view.translatesAutoresizingMaskIntoConstraints = false
            view.addSubview(controller.view)
            NSLayoutConstraint.activate([
                controller.view.leadingAnchor.constraint(equalTo: view.leadingAnchor),
                controller.view.trailingAnchor.constraint(equalTo: view.trailingAnchor),
                controller.view.topAnchor.constraint(equalTo: view.topAnchor),
                controller.view.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            ])
            controller.didMove(toParent: self)
            host = controller
        }
        let width = view.bounds.width > 0 ? view.bounds.width : 360
        let fitted = controller.sizeThatFits(in: CGSize(width: width, height: .greatestFiniteMagnitude))
        preferredContentSize = CGSize(width: width, height: min(max(fitted.height, 120), 560))
    }
}

/// The notification's content in the app's language: an ember kicker, the
/// title in Clash, the guest's words or the night's headline, and — for a
/// review — the reply Approve & post publishes, on its own card.
struct NotificationPreviewView: View {
    let preview: NotificationPreview

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 6) {
                Text(preview.kicker.uppercased())
                    .font(.cavnarBody(CavnarType.kicker, weight: 700))
                    .tracking(1.1)
                    .foregroundStyle(Color.cavnarEmber)
                if let night = preview.night {
                    Text(night)
                        .font(.cavnarNumber(CavnarType.kicker, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            if !preview.title.isEmpty {
                Text(preview.title)
                    .font(.cavnarHeadline(CavnarType.emphasis + 1))
                    .foregroundStyle(Color.cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !preview.body.isEmpty {
                PreviewMixedText.make(preview.body,
                                      size: preview.kind == .report ? CavnarType.emphasis : CavnarType.secondary)
                    .foregroundStyle(preview.kind == .report ? Color.cavnarInk : Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let draftKicker = preview.draftKicker {
                VStack(alignment: .leading, spacing: 8) {
                    Text(draftKicker.uppercased())
                        .font(.cavnarBody(CavnarType.kicker, weight: 700))
                        .tracking(1.1)
                        .foregroundStyle(Color.cavnarEmber)
                    if let draft = preview.draft {
                        PreviewMixedText.make(draft, size: CavnarType.body)
                            .foregroundStyle(Color.cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let note = preview.draftNote {
                        Text(note)
                            .font(.cavnarBody(CavnarType.caption))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper)
        .environment(\.colorScheme, .dark)
    }
}

/// Words in Apfel Grotezk, every number in Space Grotesk — the app's rule
/// for figures inside a sentence ("$4,210 net, 12% over"), without the app's
/// HomeMixedText (the extension compiles only the design tokens).
enum PreviewMixedText {
    static func make(_ text: String, size: CGFloat) -> Text {
        var out = Text("")
        var run = ""
        var runIsNumber = false
        func flush() {
            guard !run.isEmpty else { return }
            let piece = Text(run).font(runIsNumber ? .cavnarNumber(size) : .cavnarBody(size))
            out = out + piece
            run = ""
        }
        let chars = Array(text)
        for (i, ch) in chars.enumerated() {
            let next = i + 1 < chars.count ? chars[i + 1] : " "
            let numeric = ch.isNumber
                || (("$,./%:-".contains(ch)) && runIsNumber && !run.isEmpty && (next.isNumber || ch == "%"))
                || (ch == "$" && next.isNumber)
            if numeric != runIsNumber { flush() }
            runIsNumber = numeric
            run.append(ch)
        }
        flush()
        return out
    }
}
