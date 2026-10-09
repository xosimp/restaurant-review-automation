import SwiftUI

/// Me → Your attendance: the caller's own watched shifts over the last 30
/// days, each in the restaurant's words — on time, late (by how much),
/// called out, no-show, covered, and "Drop approved" for a drop the manager
/// let go that nobody picked up (schedule audit 10/3/26 E-5), which is not
/// a miss. Read from GET /staff/api/stats; nothing here is a score.
///
/// One line by default — "10 shifts checked · 9 on time · 1 late" — that
/// opens to the shifts themselves (iOS readability round, 10/8/26).
struct StaffAttendanceSection: View {
    let store: StaffPortalStore

    @State private var expanded = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var attendance: StaffAttendance? { store.stats.value?.attendance }

    var body: some View {
        if let a = attendance {
            AccountSection(kicker: "Your attendance") {
                VStack(alignment: .leading, spacing: 0) {
                    if let message = a.message, a.recent.isEmpty {
                        Text(message)
                            .cavnarText(.body)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.vertical, 10)
                    } else {
                        Button {
                            Haptic.light()
                            if reduceMotion { expanded.toggle() } else {
                                withAnimation(.easeOut(duration: 0.22)) { expanded.toggle() }
                            }
                        } label: {
                            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                                CavnarMixedText(a.summaryLine, role: .body, color: .cavnarInk)
                                Spacer(minLength: CavnarSpace.xs)
                                Image(systemName: "chevron.down")
                                    .font(.cavnar(.caption).weight(.semibold))
                                    .foregroundStyle(Color.cavnarInk3)
                                    .rotationEffect(.degrees(expanded ? 180 : 0))
                                    .accessibilityHidden(true)
                            }
                            .padding(.vertical, CavnarSpace.xs)
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                        if expanded {
                            ForEach(Array(a.recent.prefix(10).enumerated()), id: \.element.id) { _, entry in
                                AccountRowDivider()
                                HStack(alignment: .firstTextBaseline, spacing: 10) {
                                    CavnarMixedText(entry.dateLabel ?? CavnarDate.mdy(entry.date), role: .label)
                                    Spacer(minLength: 8)
                                    CavnarMixedText(entry.outcomeLabel
                                                    + (entry.minutesLate.map { $0 > 0 ? ", \($0) min" : "" } ?? ""),
                                                    role: .label,
                                                    color: entry.isMiss ? .cavnarAmber : .cavnarInk2)
                                }
                                .frame(minHeight: 40)
                                .accessibilityElement(children: .combine)
                            }
                            Text("An approved drop isn\u{2019}t a miss.")
                                .cavnarText(.caption, color: .cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                                .padding(.vertical, 9)
                        }
                    }
                }
            }
        }
    }

}

extension StaffAttendance {
    /// "10 shifts in 30 days · 9 on time · 1 late": the outcomes counted
    /// from the shifts listed (on time first, then the most frequent, ties
    /// in the order they happened), each in the restaurant's own word. When
    /// the server lists fewer shifts than it checked, the counts say which:
    /// "· last 8: 7 on time · 1 late".
    var summaryLine: String {
        var counts: [String: Int] = [:]
        var order: [String] = []
        for entry in recent {
            let word = entry.outcomeLabel
            if counts[word] == nil { order.append(word) }
            counts[word, default: 0] += 1
        }
        let onTime = Entry.onTimeLabel
        let ranked = order.enumerated().sorted { a, b in
            let l = a.element, r = b.element
            if (l == onTime) != (r == onTime) { return l == onTime }
            let cl = counts[l] ?? 0, cr = counts[r] ?? 0
            return cl != cr ? cl > cr : a.offset < b.offset
        }.map(\.element)
        let parts = ranked.map { "\(counts[$0] ?? 0) \($0.lowercased())" }.joined(separator: " \u{00B7} ")
        let head = "\(shiftsChecked) shift\(shiftsChecked == 1 ? "" : "s") in \(days ?? 30) days"
        guard !parts.isEmpty else { return head }
        if recent.count < shiftsChecked {
            return "\(head) \u{00B7} last \(recent.count): \(parts)"
        }
        return "\(head) \u{00B7} \(parts)"
    }
}

extension StaffAttendance.Entry {
    /// The on-time word the summary leads with.
    static let onTimeLabel = "On time"
}
