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
            .multilineTextAlignment(.leading)
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
            .background(muted ? Color.white.opacity(0.04) : Color.cavnarEmber.opacity(0.12))
            .overlay(
                Capsule().strokeBorder(muted ? Color.white.opacity(0.08) : Color.cavnarEmber.opacity(0.3), lineWidth: 1)
            )
            .clipShape(Capsule())
        // Deliberately no .lineLimit(1) — a chip built from a full
        // sentence (the "vibe" fact) needs to wrap within
        // AccountFlowLayout's per-item measurement below, not truncate
        // or run off the row. Every other chip here is short enough it
        // never wraps in practice.
    }
}

/// Wraps chips onto as many lines as they need — no built-in SwiftUI
/// stack does this.
struct AccountFlowLayout: Layout {
    var spacing: CGFloat = 6
    var lineSpacing: CGFloat = 6

    // A chip's IDEAL (unwrapped) width can exceed the whole row's
    // available width — a full-sentence "vibe" chip did exactly that.
    // The old version measured every item with .unspecified (no width
    // constraint) and placed it at that full width regardless of fit;
    // the FIRST item on a row skipped the wrap check entirely (nothing
    // to wrap around yet), so it just rendered past the screen edge
    // instead of wrapping or shrinking. Re-measuring an oversized item
    // AT the row's own width lets Text's own multi-line layout wrap it
    // within the pill instead.
    private func measure(_ view: LayoutSubview, maxWidth: CGFloat) -> CGSize {
        let ideal = view.sizeThatFits(.unspecified)
        guard maxWidth.isFinite, ideal.width > maxWidth else { return ideal }
        return view.sizeThatFits(ProposedViewSize(width: maxWidth, height: nil))
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for view in subviews {
            let size = measure(view, maxWidth: width)
            if x > 0, x + size.width > width {
                x = 0
                y += rowHeight + lineSpacing
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
            let size = measure(view, maxWidth: bounds.width)
            if x > bounds.minX, x - bounds.minX + size.width > bounds.width {
                x = bounds.minX
                y += rowHeight + lineSpacing
                rowHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(width: size.width, height: size.height))
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
///
/// Uses its own card padding instead of the shared `.cavnarCard()` — every
/// row here reserves 9pt of its own top/bottom padding, so `.cavnarCard()`'s
/// uniform 16pt inset would stack extra onto the FIRST row's top gap and
/// the LAST row's bottom gap versus the gap between two middle rows —
/// exactly the "top row's heading sits lower" device feedback. 10pt
/// vertical (16pt stays on the horizontal) makes edge and mid-row gaps
/// equal: 10+9 == 9+1+9. (Was 14/13 — every row measured ~64pt and the
/// cards read as mostly empty space; see the row types below.) Scoped to
/// Account only — the shared `.cavnarCard()` elsewhere isn't touched.
private struct AccountCardStyle: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(Color.cavnarPaper2.opacity(0.6))
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.card)
                    .strokeBorder(Color.cavnarPaper3.opacity(0.5), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }
}

extension View {
    /// The Account card inset for content whose rows carry their own
    /// vertical padding (AccountKVRow, AccountDisplayRow, backup-code and
    /// invoice rows). `.cavnarCard()`'s uniform 16pt stacked on top of a
    /// row's own 9-10pt is what made "Product updates & tips" and the
    /// backup-code list sit in a card that was mostly air above and below
    /// the first/last row.
    func accountCard() -> some View { modifier(AccountCardStyle()) }
}

struct AccountSection<Content: View>: View {
    let kicker: String
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: kicker)
            VStack(alignment: .leading, spacing: 0) { content() }
                .modifier(AccountCardStyle())
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
/// height (`Self.rowHeight`) regardless of what sits in the trailing slot
/// or how this row is constructed at the call site.
///
/// This was a `.frame(minHeight:)` floor of 24 — a floor, not a fixed
/// size, on the theory that every row's real content would naturally
/// settle around the same height anyway. Direct pixel measurement proved
/// that wrong: Security's three Sign-in rows (Password/2FA/Sign-in
/// notifications) are built from the identical component with identical
/// label/trailing font sizes, yet 2FA rendered at 51pt against its
/// siblings' 64pt each — a real, visible 13pt gap, not a perception
/// issue. Every content- or structure-based theory that could explain it
/// (conditional vs. unconditional construction, divider placement, label
/// length, trailing content) was tested against a counter-example in this
/// same card and disproven — whatever SwiftUI is actually doing here
/// wasn't fully traceable through the source alone. A `.frame(height:)`
/// (not minHeight) sidesteps the question entirely: 38 is the content
/// height the working rows already settle at (64pt total − 26pt padding),
/// so this makes every row match that, by construction, regardless of
/// whatever was suppressing it for specific rows.
struct AccountKVRow<Trailing: View>: View {
    // 30 + 9pt top/bottom = 48pt rows, comfortably over the 44pt HIG tap
    // minimum. Was 38 + 13 (64pt), which left every card mostly air.
    static var rowHeight: CGFloat { 30 }

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
            // A floor, not a fixed height. The exact-height version kept rows
            // aligned at the default text size, but `.frame(height:)` is a
            // hard constraint — once Dynamic Type scaling landed (see
            // Font+Cavnar's relativeTo:) it clipped labels outright instead of
            // letting the row grow (audit 7.2).
            .frame(minHeight: Self.rowHeight)
            .padding(.vertical, 9)
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
        // The dot carries the on/off state through colour alone, which fails
        // both VoiceOver and colour-blind users (WCAG 1.4.1). Restate it in
        // the label rather than adding a second visual (audit 7.5).
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(text), \(on ? "active" : "inactive")")
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
        // .firstTextBaseline, not the HStack default .center — a custom
        // Apfel Grotezk caption and an SF Symbol at a different point
        // size don't share the same visual center, so a center-aligned
        // pairing can read subtly misaligned; baseline alignment is the
        // correct pairing for text next to a glyph regardless of either
        // font's own metrics.
        HStack(alignment: .firstTextBaseline, spacing: 4) {
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

/// A plain caption, no pencil — for AccountDisplayRow's label, where the
/// value isn't edited inline (Email opens its own sheet; Locations opens
/// its own list). Sharing AccountFieldLabel's exact type/tracking keeps
/// every row in a card visually paired, without implying a pencil-tap
/// edits it directly.
private struct AccountCaptionLabel: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.cavnarBody(13, weight: 700))
            .tracking(0.8)
            .foregroundStyle(Color.cavnarInk3)
    }
}

/// The one editable-field row both AccountField and AccountEditor build
/// on. Both used to be separate implementations — a single-line
/// TextField for one, a TextEditor for the other — and the TextEditor
/// side is a known SwiftUI trouble spot: `.fixedSize(vertical: true)`
/// inside a ScrollView sizes it unreliably (confirmed here — "Menu
/// highlights" rendered dramatically taller than "Brand voice" and
/// "Never says" despite all three being built identically empty
/// placeholders). `TextField(_:text:axis:)` sizes deterministically for
/// both the single-line and growing cases, so there's now exactly one
/// code path and Contact/"How the AI writes for you" can't drift apart.
private struct AccountFieldRow<Field: Hashable>: View {
    let label: String
    var placeholder: String = ""
    @Binding var text: String
    var focus: FocusState<Field?>.Binding
    let field: Field
    var keyboardType: UIKeyboardType = .default
    var isNumber: Bool = false
    var multiline: Bool = false
    var showsDivider: Bool = true

    private var isFocused: Bool { focus.wrappedValue == field }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                AccountFieldLabel(text: label)
                Group {
                    if multiline {
                        TextField(placeholder, text: $text, axis: .vertical)
                            .lineLimit(1...6)
                    } else {
                        TextField(placeholder, text: $text)
                    }
                }
                .font(isNumber ? .cavnarNumber(17, weight: 600) : .cavnarBody(17, weight: multiline ? 400 : 700))
                .foregroundStyle(multiline ? Color.cavnarInk2 : Color.cavnarInk)
                .keyboardType(keyboardType)
                .focused(focus, equals: field)
            }
            .padding(.vertical, 9)
            .contentShape(Rectangle())
            .onTapGesture { focus.wrappedValue = field }
            // One line, not two: the row's divider IS the focus cue. It sits
            // at Paper3 between rows, and lights ember (1.5pt) while this
            // field is active. A separate underline above the divider read
            // as a doubled line on the Invite Team Member sheet.
            if showsDivider || isFocused {
                Rectangle()
                    .fill(
                        isFocused
                            ? LinearGradient(colors: [Color.cavnarEmber, Color.cavnarEmber.opacity(0.35)], startPoint: .leading, endPoint: .trailing)
                            : LinearGradient(colors: [Color.cavnarPaper3.opacity(0.5), Color.cavnarPaper3.opacity(0.5)], startPoint: .leading, endPoint: .trailing)
                    )
                    .frame(height: isFocused ? 1.5 : 1)
                    .animation(.easeOut(duration: 0.2), value: isFocused)
            }
        }
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

    var body: some View {
        AccountFieldRow(label: label, text: $text, focus: focus, field: field,
                        keyboardType: keyboardType, isNumber: isNumber, multiline: false, showsDivider: showsDivider)
    }
}

struct AccountEditor<Field: Hashable>: View {
    let label: String
    let placeholder: String
    @Binding var text: String
    var focus: FocusState<Field?>.Binding
    let field: Field
    var showsDivider: Bool = true

    var body: some View {
        AccountFieldRow(label: label, placeholder: placeholder, text: $text, focus: focus, field: field,
                        multiline: true, showsDivider: showsDivider)
    }
}

/// A read-only counterpart with the identical label/value/reserved-
/// underline footprint as AccountFieldRow — for a fact that opens its
/// own flow to change (Email's "Update", Locations' own list) rather
/// than editing inline. Sharing the exact same vertical rhythm is what
/// makes every row in Contact measure the same height regardless of
/// which kind it is.
struct AccountDisplayRow<Trailing: View>: View {
    let label: String
    let value: String
    var showsDivider: Bool = true
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    AccountCaptionLabel(text: label)
                    Text(value)
                        .font(.cavnarBody(17, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                trailing()
            }
            .padding(.vertical, 9)
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
            .padding(.vertical, 9)
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

    /// Whole days since a backend stamp; a huge number when unparseable so
    /// "stale" checks treat an unknown date as old rather than fresh.
    static func daysSince(_ stamp: String?) -> Int {
        guard let stamp, let date = parser.date(from: String(stamp.prefix(19)).replacingOccurrences(of: "T", with: " ")) else { return 10_000 }
        return Int(Date().timeIntervalSince(date) / 86_400)
    }

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
                    // Without a label VoiceOver reads the SF Symbol name
                    // ("chevron dot left") instead of the action (audit 7.5).
                    .accessibilityLabel("Back")
                }
            }
    }
}

extension View {
    func accountSheetChrome(_ title: String) -> some View {
        modifier(AccountSheetChrome(title: title))
    }
}


// MARK: - Actionable controls
//
// Every actionable thing in Account is one of three shapes, so a row reads
// the same way on every sheet:
//   AccountStateSwitch   on/off settings — red ✕ | ember ✓, the active side
//                        filled. Replaces both the orange "Turn on/off"
//                        links and the system Toggle.
//   AccountDisclosureChip  "this row opens something" — a 28pt ember chip
//                        with a chevron, the whole row tappable.
//   AccountActionChip    a one-shot action on the row itself — the same
//                        28pt chip with a glyph (red ✕ to remove/forget/
//                        disconnect, ember + to add).
// All three are ≤30pt tall, so they sit inside AccountKVRow's 48pt without
// changing any row or card geometry.

struct AccountStateSwitch: View {
    @Binding var isOn: Bool
    var busy: Bool = false
    var disabled: Bool = false
    /// Move the thumb the instant it's tapped and reconcile with `isOn`
    /// afterwards. On by default. Off for a switch whose "on" opens a flow
    /// that may be cancelled (2FA setup) — there the thumb only moves once
    /// the source of truth actually changes.
    var optimistic: Bool = true

    private static let cell: CGFloat = 36
    private static let height: CGFloat = 30
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    // The tapped-but-not-yet-confirmed position. Network-backed switches
    // used to sit still until the round-trip finished and then snap — with
    // a shimmer sweeping across in the meantime, which read as "an orange
    // line dragging over to the other side". Now the thumb moves on tap,
    // and if the save fails (`busy` ends with `isOn` unchanged) it eases back.
    @State private var pending: Bool?

    private var shown: Bool { pending ?? isOn }

    var body: some View {
        ZStack(alignment: shown ? .trailing : .leading) {
            // Track: hairline capsule with the thin divider between the two
            // sides. The divider is drawn under the thumb so it only ever
            // shows on the inactive side.
            Capsule()
                .fill(Color.white.opacity(0.05))
                .overlay(Capsule().strokeBorder(Color.cavnarPaper3.opacity(0.9), lineWidth: 1))
            Rectangle()
                .fill(Color.cavnarPaper3.opacity(0.9))
                .frame(width: 1, height: Self.height - 12)
                .frame(maxWidth: .infinity)

            // Thumb: fills the active side. Ember gradient for on, red for off.
            Capsule()
                .fill(
                    shown
                        ? LinearGradient(colors: [Color.cavnarEmber2, Color.cavnarEmber], startPoint: .topLeading, endPoint: .bottomTrailing)
                        : LinearGradient(colors: [Color.cavnarRed.opacity(0.95), Color.cavnarRed.opacity(0.8)], startPoint: .topLeading, endPoint: .bottomTrailing)
                )
                .frame(width: Self.cell, height: Self.height - 4)
                .padding(2)
                .shadow(color: (shown ? Color.cavnarEmber : Color.cavnarRed).opacity(0.45), radius: 6, y: 1)
                .opacity(busy ? 0.75 : 1)

            HStack(spacing: 0) {
                glyph("xmark", active: !shown)
                glyph("checkmark", active: shown)
            }
        }
        .frame(width: Self.cell * 2 + 4, height: Self.height)
        .opacity(disabled ? 0.45 : 1)
        .contentShape(Capsule())
        .onTapGesture {
            guard !disabled, !busy else { return }
            Haptic.selection()
            let target = !isOn
            withAnimation(reduceMotion ? nil : .easeOut(duration: 0.22)) {
                if optimistic { pending = target }
                isOn = target
            }
        }
        .onChange(of: isOn) { _, _ in
            withAnimation(reduceMotion ? nil : .easeOut(duration: 0.22)) { pending = nil }
        }
        .onChange(of: busy) { _, nowBusy in
            // Save finished: the source of truth either changed (handled
            // above) or didn't — either way the thumb follows it now.
            if !nowBusy { withAnimation(reduceMotion ? nil : .easeOut(duration: 0.22)) { pending = nil } }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(shown ? "On" : "Off")
        .accessibilityAddTraits(.isButton)
    }

    private func glyph(_ name: String, active: Bool) -> some View {
        Image(systemName: name)
            .font(.system(size: 12, weight: .bold))
            .foregroundStyle(active ? Color.cavnarInk : Color.cavnarInk3.opacity(0.55))
            .frame(width: Self.cell, height: Self.height)
            .animation(.easeOut(duration: 0.2), value: active)
    }
}

/// The 28pt ember chip that says "this row opens something".
struct AccountDisclosureChip: View {
    var body: some View {
        Image(systemName: "chevron.right")
            .font(.system(size: 12, weight: .bold))
            .foregroundStyle(Color.cavnarEmber)
            .frame(width: 28, height: 28)
            .background(Color.cavnarEmber.opacity(0.14), in: Circle())
    }
}

/// The same chip carrying a one-shot action — red ✕ to remove/forget/
/// disconnect, ember + to add, and so on. The chip is the whole hit area.
struct AccountActionChip: View {
    let symbol: String
    var tone: Color = .cavnarEmber
    var accessibilityLabel: String
    let action: () -> Void

    var body: some View {
        Button {
            Haptic.light()
            action()
        } label: {
            Image(systemName: symbol)
                .font(.system(size: 12, weight: .bold))
                .foregroundStyle(tone)
                .frame(width: 28, height: 28)
                .background(tone.opacity(0.14), in: Circle())
                .overlay(Circle().strokeBorder(tone.opacity(0.35), lineWidth: 1))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(accessibilityLabel)
    }
}

/// A row that opens something: label, optional value on the right, chevron
/// chip. The whole row is the button.
struct AccountNavRow: View {
    let label: String
    var value: String? = nil
    var valueIsNumber: Bool = false
    var showsDivider: Bool = true
    let action: () -> Void

    var body: some View {
        Button {
            Haptic.light()
            action()
        } label: {
            AccountKVRow(label: label, showsDivider: showsDivider) {
                HStack(spacing: 10) {
                    if let value {
                        Text(value)
                            .font(valueIsNumber ? .cavnarNumber(15, weight: 600) : .cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk2)
                            .lineLimit(1)
                    }
                    AccountDisclosureChip()
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

/// An on/off setting: label (optionally with a second line of detail) and
/// the state switch. `onChange` fires after the flip so a network-backed
/// setting can save; `busy` shows the switch mid-save.
struct AccountSwitchRow: View {
    let label: String
    var detail: String? = nil
    @Binding var isOn: Bool
    var busy: Bool = false
    var disabled: Bool = false
    var optimistic: Bool = true
    var showsDivider: Bool = true

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(label).font(.cavnarBody(16)).foregroundStyle(Color.cavnarInk3)
                    if let detail {
                        Text(detail).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3.opacity(0.8))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 8)
                AccountStateSwitch(isOn: $isOn, busy: busy, disabled: disabled, optimistic: optimistic)
            }
            .frame(minHeight: AccountKVRow<EmptyView>.rowHeight)
            .padding(.vertical, 9)
            if showsDivider { AccountRowDivider() }
        }
    }
}

/// A one-shot action row: label and an action chip on the right.
struct AccountActionRow: View {
    let label: String
    var detail: String? = nil
    let symbol: String
    var tone: Color = .cavnarEmber
    var busy: Bool = false
    var showsDivider: Bool = true
    let action: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(label).font(.cavnarBody(16)).foregroundStyle(tone == .cavnarRed ? Color.cavnarRed : Color.cavnarInk3)
                    if let detail {
                        Text(detail).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3.opacity(0.8))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 8)
                if busy {
                    CavnarShimmerLine(color: tone).frame(width: 28)
                } else {
                    AccountActionChip(symbol: symbol, tone: tone, accessibilityLabel: label, action: action)
                }
            }
            .frame(minHeight: AccountKVRow<EmptyView>.rowHeight)
            .padding(.vertical, 9)
            if showsDivider { AccountRowDivider() }
        }
    }
}
