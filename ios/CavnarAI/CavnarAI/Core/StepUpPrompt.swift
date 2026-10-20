import UIKit

/// The account holder's step-up (re-audit 10/20/26 R7-03): a sign-in
/// security change — turning two-factor off, new backup codes, adding a
/// co-owner, the owner's phone, signing other devices out, closing the
/// account, opening a view-as that can make changes — is refused 403
/// `{reauth_required}` unless this session typed its password in the last
/// 15 minutes. APIClient asks here for the password, posts it to the
/// server's `reauth_url`, and sends the change again; one prompt at a time.
///
/// A system alert with one secure field (DESIGN_SYSTEM.md → iOS → Step-up
/// prompt): it has to appear over whatever sheet started the change, from
/// the networking actor, without every screen growing its own password UI.
@MainActor
enum StepUpPrompt {
    private static var waiting: [CheckedContinuation<String?, Never>] = []
    private static var showing = false

    /// The password the person typed, or nil when they cancelled (or no
    /// window could show the prompt).
    static func ask(message: String, error: String? = nil) async -> String? {
        await withCheckedContinuation { continuation in
            waiting.append(continuation)
            guard !showing else { return }
            present(message: message, error: error)
        }
    }

    private static func finish(_ password: String?) {
        showing = false
        let all = waiting
        waiting = []
        for continuation in all { continuation.resume(returning: password) }
    }

    private static func present(message: String, error: String?) {
        guard let top = topController() else { finish(nil); return }
        showing = true
        let body = [error, message].compactMap { $0 }.joined(separator: "\n\n")
        let alert = UIAlertController(title: "Confirm it\u{2019}s you", message: body, preferredStyle: .alert)
        alert.addTextField { field in
            field.isSecureTextEntry = true
            field.textContentType = .password
            field.placeholder = "Your password"
        }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in finish(nil) })
        alert.addAction(UIAlertAction(title: "Confirm", style: .default) { [weak alert] _ in
            let typed = alert?.textFields?.first?.text ?? ""
            finish(typed.isEmpty ? nil : typed)
        })
        top.present(alert, animated: true)
    }

    private static func topController() -> UIViewController? {
        let window = UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .flatMap { $0.windows }
            .first { $0.isKeyWindow }
        var top = window?.rootViewController
        while let next = top?.presentedViewController { top = next }
        return top
    }
}
