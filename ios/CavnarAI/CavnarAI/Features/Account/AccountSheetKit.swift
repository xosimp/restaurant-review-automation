import SwiftUI

/// The "identity card" vocabulary every Account detail sheet is built
/// from (design review, option A): the sheet opens on WHO/WHAT this is —
/// an ember-tile badge, a Clash Display title, a one-line subtitle — then
/// a strip of chips or status tiles that answers the sheet's one question
/// at a glance, then the settings themselves in warm cards whose fields
/// light an ember underline on focus. Built once here so Restaurant,
/// Security, Alerts, Connections and Billing all read as one family.

// MARK: - Hero

struct AccountHero<Badge: View, Subtitle: View>: View {
    let title: String
    @ViewBuilder var badge: () -> Badge
    @ViewBuilder var subtitle: () -> Subtitle

    var body: some View {
        HStack(spacing: 14) {
            badge()
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.cavnarHeadline(24))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                subtitle()
                    .font(.cavnarBody(15.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
    }
}

// MARK: - Chips

struct AccountChip: View {
    let text: String
    var muted: Bool = false

    var body: some View {
        Text(text)
            .font(.cavnarBody(13.5, weight: 700))
            .foregroundStyle(muted ? Color.cavnarInk2 : Color.cavnarEmber2)
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
            .background(muted ? Color.white.opacity(0.04) : Color.cavnarEmber.opacity(0.12))
            .overlay(
                Capsule().strokeBorder(muted ? Color.white.opacity(0.08) : Color.cavnarEmber.opacity(0.3), lineWidth: 1)
            )
            .clipShape(Capsule())
            .lineLimit(1)
    }
}

/// Wraps chips onto as many lines as they need — no built-in SwiftUI
/// stack does this.
struct AccountFlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: width == .infinity ? x : width, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}

// MARK: - Section + kicker

struct AccountKicker: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.cavnarBody(13, weight: 700))
            .tracking(1.4)
            .foregroundStyle(Color.cavnarInk3)
    }
}

/// Kicker above a warm card. Rows inside the card draw their own
/// dividers (see `showsDivider` on each row type), so content is a plain
/// zero-spacing stack.
struct AccountSection<Content: View>: View {
    let kicker: String
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: kicker)
            VStack(alignment: .leading, spacing: 0) { content() }
                .cavnarCard()
        }
    }
}

struct AccountRowDivider: View {
    var body: some View {
        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
    }
}

// MARK: - Status tiles

struct AccountStatTile: View {
    let label: String
    let value: String
    var tone: Color = .cavnarInk
    var detail: String? = nil
    var detailIsNumber: Bool = false
    var valueIsNumber: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label.uppercased())
                .font(.cavnarBody(12, weight: 700))
                .tracking(1.1)
                .foregroundStyle(Color.cavnarInk3)
            Text(value)
                .font(valueIsNumber ? .cavnarNumber(18, weight: 600) : .cavnarHeadline(18))
                .foregroundStyle(tone)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            if let detail {
                Text(detail)
                    .font(detailIsNumber ? .cavnarNumber(13.5) : .cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
        .padding(.top, 13)
        .padding(.bottom, 11)
        .background(
            LinearGradient(
                colors: [Color.cavnarEmber.opacity(0.2), Color.cavnarEmber.opacity(0.05)],
                startPoint: .topLeading, endPoint: .bottomTrailing
            )
        )
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.cavnarEmber.opacity(0.28), lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 14))
    }
}

// MARK: - Rows

/// Label on the left, whatever you like on the right — a value or a
/// link. Every row in a card built from these shares one fixed content
/// height (`Self.rowHeight`, sized to a single line of the bumped 16pt
/// row font) regardless of what sits in the trailing slot — device
/// feedback caught a Toggle-vs-Text-link row visibly taller than its
/// siblings in the same card, which is what made Sign-in's three rows
/// read as inconsistently positioned even though each one was
/// individually centered. A fixed height removes the possibility of that
/// drift instead of tuning it away per call site.
struct AccountKVRow<Trailing: View>: View {
    static var rowHeight: CGFloat { 24 }

    let label: String
    var showsDivider: Bool = true
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                Text(label).font(.cavnarBody(16)).foregroundStyle(Color.cavnarInk3)
                Spacer(minLength: 8)
                trailing()
            }
            .frame(minHeight: Self.rowHeight)
            .padding(.vertical, 13)
            if showsDivider { AccountRowDivider() }
        }
    }
}

struct AccountValue: View {
    let text: String
    var isNumber: Bool = false
    var tone: Color = .cavnarInk

    var body: some View {
        Text(text)
            .font(isNumber ? .cavnarNumber(16, weight: 600) : .cavnarBody(16, weight: 700))
            .foregroundStyle(tone)
            .multilineTextAlignment(.trailing)
    }
}

struct AccountLink: View {
    let title: String
    var tone: Color = .cavnarEmber
    let action: () -> Void

    var body: some View {
        Button {
            Haptic.light()
            action()
        } label: {
            Text(title).font(.cavnarBody(16, weight: 700)).foregroundStyle(tone)
        }
        .buttonStyle(.plain)
    }
}

struct AccountPill: View {
    let text: String
    var on: Bool = true

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(on ? Color.cavnarGreen : Color.cavnarInk3).frame(width: 6, height: 6)
            Text(text).font(.cavnarBody(13, weight: 700)).foregroundStyle(on ? Color.cavnarGreen : Color.cavnarInk3)
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 4)
        .background(on ? Color.cavnarGreen.opacity(0.14) : Color.white.opacity(0.05))
        .clipShape(Capsule())
    }
}

// MARK: - Editable fields

/// A caption plus a small pencil glyph — nothing else in the app marks a
/// field as "yours to change" until you've already tapped into it and the
/// underline lights, which read as "not obviously editable" (device
/// feedback). The pencil is the at-rest cue every editable field in
/// Account now carries; read-only facts (Restaurant's admin-set chips,
/// Security's status tiles) never get one.
private struct AccountFieldLabel: View {
    let text: String
    var body: some View {
        HStack(spacing: 4) {
            Text(text.uppercased())
                .font(.cavnarBody(13, weight: 700))
                .tracking(0.8)
                .foregroundStyle(Color.cavnarInk3)
            Image(systemName: "pencil")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber.opacity(0.7))
        }
    }
}

/// An ember underline that lights only while its field has focus — the
/// one cue that says "this is yours to change" without a lock-icon
/// paragraph.
private struct AccountFocusUnderline: View {
    let lit: Bool
    var body: some View {
        Rectangle()
            .fill(
                LinearGradient(
                    colors: [Color.cavnarEmber, Color.cavnarEmber.opacity(0)],
                    startPoint: .leading, endPoint: .trailing
                )
            )
            .frame(height: 1.5)
            .opacity(lit ? 1 : 0)
            .animation(.easeOut(duration: 0.2), value: lit)
    }
}

struct AccountField<Field: Hashable>: View {
    let label: String
    @Binding var text: String
    var focus: FocusState<Field?>.Binding
    let field: Field
    var keyboardType: UIKeyboardType = .default
    var isNumber: Bool = false
    var showsDivider: Bool = true

    private var isFocused: Bool { focus.wrappedValue == field }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 5) {
                AccountFieldLabel(text: label)
                TextField("", text: $text)
                    .font(isNumber ? .cavnarNumber(17, weight: 600) : .cavnarBody(17, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .keyboardType(keyboardType)
                    .focused(focus, equals: field)
                AccountFocusUnderline(lit: isFocused)
                    .padding(.top, 2)
            }
            .padding(.vertical, 13)
            .contentShape(Rectangle())
            .onTapGesture { focus.wrappedValue = field }
            if showsDivider { AccountRowDivider() }
        }
    }
}

struct AccountEditor<Field: Hashable>: View {
    let label: String
    let placeholder: String
    @Binding var text: String
    var focus: FocusState<Field?>.Binding
    let field: Field
    var showsDivider: Bool = true

    private var isFocused: Bool { focus.wrappedValue == field }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 5) {
                AccountFieldLabel(text: label)
                ZStack(alignment: .topLeading) {
                    if text.isEmpty {
                        Text(placeholder)
                            .font(.cavnarBody(17))
                            .foregroundStyle(Color.cavnarInk3)
                            .padding(.top, 8)
                            .padding(.leading, 5)
                    }
                    TextEditor(text: $text)
                        .font(.cavnarBody(17))
                        .foregroundStyle(Color.cavnarInk2)
                        .scrollContentBackground(.hidden)
                        .scrollDisabled(true)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(minHeight: 36)
                        .focused(focus, equals: field)
                }
                AccountFocusUnderline(lit: isFocused)
                    .padding(.top, 2)
            }
            .padding(.vertical, 13)
            .contentShape(Rectangle())
            .onTapGesture { focus.wrappedValue = field }
            if showsDivider { AccountRowDivider() }
        }
    }
}

// MARK: - Devices

struct AccountDeviceRow: View {
    let session: AccountSession
    var showsDivider: Bool = true

    private var symbol: String {
        if session.deviceType == "ios" || session.label.hasPrefix("iPhone") || session.label == "Android" { return "iphone" }
        if session.label.hasPrefix("iPad") { return "ipad" }
        if session.label == "Mac" { return "laptopcomputer" }
        if session.label == "Windows" { return "desktopcomputer" }
        return "globe"
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: symbol)
                    .font(.system(size: 16, weight: .medium))
                    .foregroundStyle(session.isCurrent ? Color.cavnarEmber2 : Color.cavnarInk2)
                    .frame(width: 34, height: 34)
                    .background(session.isCurrent ? Color.cavnarEmber.opacity(0.14) : Color.white.opacity(0.05))
                    .overlay(
                        RoundedRectangle(cornerRadius: 10)
                            .strokeBorder(Color.cavnarEmber.opacity(session.isCurrent ? 0.6 : 0), lineWidth: 1.5)
                    )
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Text(session.label).font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                        if session.isCurrent { AccountPill(text: "This device") }
                    }
                    Text(AccountRelativeTime.describe(session.lastActive, activePrefix: true))
                        .font(.cavnarNumber(14))
                        .foregroundStyle(Color.cavnarInk3)
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 13)
            if showsDivider { AccountRowDivider() }
        }
    }
}

/// "Active now" / "12m ago" / "2 days ago" from the backend's
/// "YYYY-MM-DD HH:MM:SS" UTC stamps.
enum AccountRelativeTime {
    private static let parser: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        f.timeZone = TimeZone(identifier: "UTC")
        f.locale = Locale(identifier: "en_US_POSIX")
        return f
    }()
    private static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "MMM d"
        return f
    }()

    static func describe(_ stamp: String?, activePrefix: Bool = false) -> String {
        guard let stamp, let date = parser.date(from: String(stamp.prefix(19)).replacingOccurrences(of: "T", with: " ")) else {
            return "—"
        }
        let seconds = Date().timeIntervalSince(date)
        if seconds < 120 { return activePrefix ? "Active now" : "Just now" }
        let minutes = Int(seconds / 60)
        if minutes < 60 { return "\(minutes)m ago" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours)h ago" }
        let days = hours / 24
        if days == 1 { return "Yesterday" }
        if days < 14 { return "\(days) days ago" }
        return dayFormatter.string(from: date)
    }
}

// MARK: - Sheet chrome

/// Module background, the centered Clash Display title, and the ember
/// chevron at the leading edge that closes the sheet — same glass chip as
/// every back button in the app, so a sheet's top bar matches a pushed
/// screen's.
private struct AccountSheetChrome: ViewModifier {
    let title: String
    @Environment(\.dismiss) private var dismiss

    func body(content: Content) -> some View {
        content
            .cavnarModuleBackground()
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar(title) }
            .toolbar {
                cavnarToolbarItem(placement: .topBarLeading) {
                    Button {
                        Haptic.light()
                        dismiss()
                    } label: {
                        Image(systemName: "chevron.left")
                            .font(.system(size: 17, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                            .cavnarToolbarIconGlass()
                    }
                    .buttonStyle(.plain)
                    .tint(nil)
                }
            }
    }
}

extension View {
    func accountSheetChrome(_ title: String) -> some View {
        modifier(AccountSheetChrome(title: title))
    }
}
