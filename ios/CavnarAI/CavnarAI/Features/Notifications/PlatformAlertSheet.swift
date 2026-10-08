import SwiftUI

/// An operator page (ops.alert_will → `platform_alert`), as its push
/// carried it: the subject, the alert's lines and when it went out. Only
/// admin logins are pushed it (push.ADMIN_ONLY_TYPES), and the app opens it
/// only for one (DeepLinkRouter `admin/platform`). It used to land on the
/// owner Home, which says nothing about the platform.
struct PlatformAlert: Identifiable, Equatable, Sendable {
    let subject: String?
    let lines: [String]
    /// When the page went out (`alert_at`, UTC ISO).
    let alertAt: Date?

    var id: String { "\(subject ?? "")|\(alertAt?.timeIntervalSince1970 ?? 0)|\(lines.count)" }

    /// The console's Operations area, where every one of these is worked.
    static let consoleURL = URL(string: "https://dashboard.cavnar.ai/admin#operations")!

    init(subject: String?, lines: [String], alertAt: Date?) {
        self.subject = subject
        self.lines = lines
        self.alertAt = alertAt
    }

    /// From the push's `cavnar` payload (ops.platform_alert_payload):
    /// `subject`, `lines` (at most six, each clipped), `alert_at`.
    init(cavnar: [String: Any]) {
        let subject = (cavnar["subject"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.subject = (subject?.isEmpty == false) ? String(subject!.prefix(160)) : nil
        let raw = (cavnar["lines"] as? [Any]) ?? []
        self.lines = raw.compactMap { ($0 as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .prefix(6)
            .map { String($0.prefix(200)) }
        self.alertAt = (cavnar["alert_at"] as? String).flatMap { CavnarDate.timestamp($0) }
    }

    /// "Sent 10/7/26 · 9:05am" on the phone's clock, or nil.
    func sentLine(in zone: TimeZone = .current) -> String? {
        alertAt.map { "Sent " + CavnarDate.mdyTime($0, in: zone) }
    }
}

/// "Platform needs you": the page's own words, when it went out, and the
/// console to work it in. Opened from the push (admins only).
struct PlatformAlertSheet: View {
    let alert: PlatformAlert
    @Environment(\.openURL) private var openURL
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text("PLATFORM NEEDS YOU")
                        .font(.cavnarBody(12, weight: 700))
                        .tracking(1.4)
                        .foregroundStyle(Color.cavnarEmber2)
                    Text(alert.subject ?? "A platform alert went out")
                        .font(.cavnarHeadline(20))
                        .foregroundStyle(Color.cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    if let sent = alert.sentLine() {
                        HomeMixedText.make(sent, size: 14, weight: 600, color: .cavnarInk3)
                    }
                    if alert.lines.isEmpty {
                        Text("The alert's details are in the console.")
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarInk2)
                    } else {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(Array(alert.lines.enumerated()), id: \.offset) { _, line in
                                HStack(alignment: .firstTextBaseline, spacing: 8) {
                                    Circle().fill(Color.cavnarEmber).frame(width: 5, height: 5)
                                    HomeMixedText.make(line, size: 14.5, weight: 500, color: .cavnarInk2)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                    }
                    Button {
                        Haptic.light()
                        openURL(PlatformAlert.consoleURL)
                        dismiss()
                    } label: {
                        Text("Open console").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .padding(.top, 4)
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .topLeading)
            }
            .accountSheetChrome("Platform")
        }
    }
}
