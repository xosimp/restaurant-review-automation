import SwiftUI

/// Me → Your attendance: the caller's own watched shifts over the last 30
/// days, each in the restaurant's words — on time, late (by how much),
/// called out, no-show, covered, and "Drop approved" for a drop the manager
/// let go that nobody picked up (schedule audit 10/3/26 E-5), which is not
/// a miss. Read from GET /staff/api/stats; nothing here is a score.
struct StaffAttendanceSection: View {
    let store: StaffPortalStore

    private var attendance: StaffAttendance? { store.stats.value?.attendance }

    var body: some View {
        if let a = attendance {
            AccountSection(kicker: "Your attendance") {
                VStack(alignment: .leading, spacing: 0) {
                    if let message = a.message, a.recent.isEmpty {
                        Text(message)
                            .font(.cavnarBody(CavnarType.body))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.vertical, 10)
                    } else {
                        HomeMixedText.make("\(a.shiftsChecked) shift\(a.shiftsChecked == 1 ? "" : "s") checked in the last "
                                           + "\(a.days ?? 30) days", size: CavnarType.secondary, color: .cavnarInk3)
                            .padding(.vertical, 8)
                        ForEach(Array(a.recent.prefix(10).enumerated()), id: \.element.id) { index, entry in
                            AccountRowDivider()
                            HStack(alignment: .firstTextBaseline, spacing: 10) {
                                HomeMixedText.make(entry.dateLabel ?? CavnarDate.mdy(entry.date),
                                                   size: CavnarType.body, weight: 600, color: .cavnarInk)
                                Spacer(minLength: 8)
                                HomeMixedText.make(entry.outcomeLabel
                                                   + (entry.minutesLate.map { $0 > 0 ? ", \($0) min" : "" } ?? ""),
                                                   size: CavnarType.body, weight: 600,
                                                   color: entry.isMiss ? .cavnarAmber : .cavnarInk2)
                            }
                            .frame(minHeight: 40)
                            .accessibilityElement(children: .combine)
                        }
                        Text("From the shifts your restaurant checked. A drop your manager approved isn\u{2019}t a miss.")
                            .font(.cavnarBody(CavnarType.caption))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.vertical, 9)
                    }
                }
            }
        }
    }
}
