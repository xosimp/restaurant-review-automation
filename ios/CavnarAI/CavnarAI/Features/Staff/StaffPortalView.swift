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

    enum Tab: String, CaseIterable {
        case schedule = "Schedule"
        case tasks = "Tasks"
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
            sectionLabel("NEXT 7 DAYS")
            ForEach(week) { day in
                dayRow(day)
            }
        } else if loadError != nil {
            emptyCard(loadError ?? "")
        } else {
            loadingCard("Loading your shifts…")
        }
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
            if tasks.role == nil {
                emptyCard("Your job role isn't set yet, so there's no checklist to show.")
            } else if (tasks.tasks ?? []).isEmpty {
                emptyCard("Nothing on the \(tasks.role ?? "") checklist today.")
            } else {
                let items = tasks.tasks ?? []
                sectionLabel("\((tasks.role ?? "").uppercased()) · TODAY")
                Text("\(items.filter(\.done).count) of \(items.count) done")
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk3)
                ForEach(items) { task in
                    Button {
                        Haptic.light()
                        Task { await toggle(task) }
                    } label: {
                        HStack(spacing: 12) {
                            Image(systemName: task.done ? "checkmark.circle.fill" : "circle")
                                .font(.system(size: 20))
                                .foregroundStyle(task.done ? Color.cavnarEmber : Color.cavnarInk3)
                            Text(task.label)
                                .font(.cavnarBody(15.5))
                                .strikethrough(task.done)
                                .foregroundStyle(task.done ? Color.cavnarInk3 : Color.cavnarInk)
                            Spacer(minLength: 0)
                        }
                        .padding(13)
                        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 10))
                    }
                }
            }
        } else {
            loadingCard("Loading your tasks…")
        }
    }

    // MARK: - Profile

    @ViewBuilder
    private var profileSection: some View {
        if let profile {
            infoRow("Name", profile.name)
            infoRow("Restaurant", profile.restaurant)
            Button {
                Haptic.light()
                staff.signOut()
            } label: {
                Text("Sign out").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .padding(.top, 14)
        } else {
            loadingCard("Loading…")
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

    private func loadingCard(_ text: String) -> some View {
        Text(text).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
    }

    // MARK: - Loading

    private func reload() async {
        async let me: StaffProfileResponse? = try? staff.authed("/staff/api/me")
        async let sh: StaffShiftsResponse? = try? staff.authed("/staff/api/shifts")
        async let tk: StaffTasksResponse? = try? staff.authed("/staff/api/tasks?date=\(Self.taskDay())")
        let (meResp, shResp, tkResp) = await (me, sh, tk)
        profile = meResp?.employee
        shifts = shResp
        tasks = tkResp
        if meResp == nil { loadError = "Could not load your portal." }
    }

    private func toggle(_ task: StaffTask) async {
        struct Body: Encodable {
            let templateID: Int
            let taskDate: String
            let done: Bool
            enum CodingKeys: String, CodingKey {
                case templateID = "template_id"
                case taskDate = "task_date"
                case done
            }
        }
        // One day for the tick and the re-read: the refetch used to send no
        // date and read the server's UTC day, so a task ticked at 8pm in
        // Chicago came back unticked (MOD-EMP-5).
        let day = Self.taskDay()
        let body = Body(templateID: task.id, taskDate: day, done: !task.done)
        _ = try? await staff.authed("/staff/api/tasks/complete", method: .post, body: body) as StaffOKResponse
        tasks = try? await staff.authed("/staff/api/tasks?date=\(day)")
    }

    /// The phone's own calendar day, written the way the server reads it:
    /// Gregorian and POSIX, so a device set to another calendar or locale
    /// still sends this year's ISO date.
    private static func taskDay(_ date: Date = Date()) -> String {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: date)
    }
}
