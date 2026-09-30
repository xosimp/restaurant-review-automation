import SwiftUI

/// The employee's whole app. Deliberately not a stripped-down dashboard —
/// it is a different product with three screens, and nothing owner-facing
/// can appear here because the owner routes are unreachable with a staff
/// token (auth._console_denied) rather than merely hidden.
struct StaffPortalView: View {
    @Environment(StaffSessionStore.self) private var staff
    @State private var tab: Tab = .schedule
    @State private var shifts: StaffShiftsResponse?
    @State private var tasks: StaffTasksResponse?
    @State private var profile: StaffProfile?
    @State private var loadError: String?
    /// A shift the employee can't make: hand it back or swap it (Friction #49).
    @State private var changing: ShiftChange?

    struct ShiftChange: Identifiable {
        let day: StaffWeekDay
        let mode: StaffShiftChangeSheet.Mode
        var id: String { day.date + (mode == .swap ? "|swap" : "|drop") }
    }

    enum Tab: String, CaseIterable {
        case schedule = "Schedule"
        case tasks = "Tasks"
        // Time off and shift changes — the web portal's two forms (U3-21).
        case requests = "Requests"
        case profile = "Profile"
    }

    var body: some View {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            VStack(alignment: .leading, spacing: 0) {
                header
                picker
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        switch tab {
                        case .schedule: scheduleSection
                        case .tasks:    tasksSection
                        case .requests:
                            // The web portal's "Availability & time off":
                            // the days I can't work and what I'd like, then
                            // time off and shift changes (9/25/26 parity).
                            StaffAvailabilitySection()
                            StaffPreferencesSection()
                                .padding(.top, 8)
                            StaffRequestsView()
                                .padding(.top, 8)
                        case .profile:  profileSection
                        }
                    }
                    .padding(.top, 4)
                    .padding(.bottom, 40)
                }
            }
            .padding(.horizontal, 20)
        }
        .task { await reload() }
        .sheet(item: $changing, onDismiss: { Task { await reload() } }) { change in
            StaffShiftChangeSheet(day: change.day, mode: change.mode)
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text((profile?.restaurant ?? "").uppercased())
                .font(.cavnarBody(11, weight: 700))
                .kerning(1.6)
                .foregroundStyle(Color.cavnarEmber2)
            Text(profile?.name ?? " ")
                .font(.cavnarHeadline(25))
                .foregroundStyle(Color.cavnarInk)
            Text(todayLine)
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
        }
        .padding(.top, 18)
        .padding(.bottom, 16)
    }

    private var todayLine: String {
        guard let shifts, shifts.published == true else { return " " }
        guard let today = shifts.today else { return "You're off today." }
        return "Today: \(today.timeRange)"
    }

    private var picker: some View {
        HStack(spacing: 7) {
            ForEach(Tab.allCases, id: \.self) { option in
                Button {
                    Haptic.selection()
                    tab = option
                } label: {
                    Text(option.rawValue)
                        .font(.cavnarBody(14, weight: 600))
                        .foregroundStyle(tab == option ? Color.white : Color.cavnarInk3)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .background(tab == option ? Color.cavnarEmber : Color.cavnarPaper2,
                                    in: RoundedRectangle(cornerRadius: 10))
                }
            }
        }
        .padding(.bottom, 16)
    }

    // MARK: - Schedule

    @ViewBuilder
    private var scheduleSection: some View {
        if let shifts, shifts.published == false {
            emptyCard("No schedule has been posted yet. It'll show up here as soon as your manager publishes one.")
        } else if let week = shifts?.week {
            // "When do I work next?" answered first (density #13), with
            // the swap / can't-make-it actions on it — then the week.
            if let next = Self.nextShift(week) {
                nextShiftCard(next.day, when: next.when)
            }
            // Today's lineup read, as the web portal shows it (preshift.py).
            StaffPreshiftCard()
            sectionLabel("NEXT 7 DAYS")
            ForEach(week) { day in
                dayRow(day)
            }
        } else if loadError != nil {
            emptyCard(loadError ?? "")
        } else {
            loadingCard("Loading your shifts")
        }
    }

    /// The first day in the week with a shift, and how to say when it is:
    /// "Today", "Tomorrow" (the day after today in the list), or the
    /// weekday. Nil when every day is off.
    static func nextShift(_ week: [StaffWeekDay]) -> (day: StaffWeekDay, when: String)? {
        guard let i = week.firstIndex(where: { !$0.off && $0.shift != nil }) else { return nil }
        let day = week[i]
        if day.isToday { return (day, "Today") }
        if i > 0, week[i - 1].isToday { return (day, "Tomorrow") }
        return (day, day.weekday)
    }

    private func nextShiftCard(_ day: StaffWeekDay, when: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("NEXT SHIFT")
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .kerning(1.4)
                .foregroundStyle(Color.cavnarEmber2)
            if let shift = day.shift {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(when)
                        .font(.cavnarHeadline(CavnarType.section))
                        .foregroundStyle(Color.cavnarInk)
                    Text(shift.timeRange)
                        .font(.cavnarNumber(CavnarType.tileNumber, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                }
                if let role = shift.role, !role.isEmpty {
                    Text(role)
                        .font(.cavnarBody(CavnarType.body))
                        .foregroundStyle(Color.cavnarInk2)
                }
                if shift.shiftStart != nil {
                    HStack(spacing: 18) {
                        Button("Swap with a colleague") { changing = ShiftChange(day: day, mode: .swap) }
                        Button("Can\u{2019}t make it") { changing = ShiftChange(day: day, mode: .drop) }
                    }
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                    .buttonStyle(.plain)
                    .padding(.top, 2)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.cavnarEmber.opacity(0.35), lineWidth: 1))
        .padding(.bottom, 8)
        .accessibilityElement(children: .contain)
    }

    private func dayRow(_ day: StaffWeekDay) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Rectangle()
                .fill(day.off ? Color.cavnarPaper3
                              : (day.isToday ? Color.cavnarGreen : Color.cavnarEmber))
                .frame(width: 3)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(day.weekday)
                        .font(.cavnarBody(15, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                    Text(day.isToday ? "today" : day.date)
                        .font(.cavnarBody(12.5))
                        .foregroundStyle(Color.cavnarInk3)
                }
                if let shift = day.shift {
                    Text(shift.timeRange)
                        .font(.cavnarNumber(17, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    if let role = shift.role, !role.isEmpty {
                        Text(role)
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk2)
                    }
                } else {
                    Text("Off")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer(minLength: 0)
            if day.shift?.shiftStart != nil {
                Menu {
                    Button("Can\u{2019}t work this") { changing = ShiftChange(day: day, mode: .drop) }
                    Button("Swap with a colleague") { changing = ShiftChange(day: day, mode: .swap) }
                } label: {
                    Image(systemName: "ellipsis")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .accessibilityLabel("Change this shift")
            }
        }
        .padding(.vertical, 12)
        .padding(.trailing, 14)
        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
        .opacity(day.off ? 0.62 : 1)
    }

    // MARK: - Tasks

    @ViewBuilder
    private var tasksSection: some View {
        if let tasks {
            // Your sheets today (task_sheets.py), with due times and proof.
            StaffTaskSheetsSection(response: tasks) {
                self.tasks = try? await staff.authed("/staff/api/tasks")
            }
        } else {
            loadingCard("Loading your sheets")
        }
    }

    // MARK: - Profile

    @ViewBuilder
    private var profileSection: some View {
        if let profile {
            infoRow("Name", profile.name)
            infoRow("Restaurant", profile.restaurant)
            StaffChangePinSection()
                .padding(.top, 10)
            Button {
                Haptic.light()
                staff.signOut()
            } label: {
                Text("Sign out").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .padding(.top, 14)
        } else {
            loadingCard("Loading your profile")
        }
    }

    private func infoRow(_ key: String, _ value: String) -> some View {
        HStack {
            Text(key).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
            Spacer()
            Text(value).font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarInk)
        }
        .padding(13)
        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 10))
    }

    // MARK: - Shared pieces

    private func sectionLabel(_ text: String) -> some View {
        Text(text)
            .font(.cavnarBody(11, weight: 700))
            .kerning(1.3)
            .foregroundStyle(Color.cavnarEmber2)
            .padding(.top, 6)
    }

    private func emptyCard(_ text: String) -> some View {
        Text(text)
            .font(.cavnarBody(14))
            .foregroundStyle(Color.cavnarInk3)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
    }

    /// The house loading state (DESIGN_SYSTEM §10): the sliding ember line
    /// with a plain label under it — never a bare "Loading…".
    private func loadingCard(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            CavnarSkeletonBar(height: 3).frame(width: 180)
            Text(text).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: - Loading

    private func reload() async {
        async let me: StaffProfileResponse? = try? staff.authed("/staff/api/me")
        async let sh: StaffShiftsResponse? = try? staff.authed("/staff/api/shifts")
        async let tk: StaffTasksResponse? = try? staff.authed("/staff/api/tasks")
        let (meResp, shResp, tkResp) = await (me, sh, tk)
        profile = meResp?.employee
        shifts = shResp
        tasks = tkResp
        if meResp == nil { loadError = "Could not load your portal." }
    }
}
