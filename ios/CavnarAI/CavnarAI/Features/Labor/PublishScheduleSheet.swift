import SwiftUI
import Observation

struct StaffContact: Decodable, Identifiable, Hashable {
    let employeeName: String
    let email: String
    let phone: String

    var id: String { employeeName }
    var isReachable: Bool { !email.isEmpty }

    enum CodingKeys: String, CodingKey {
        case employeeName = "employee_name"
        case email, phone
    }
}

/// Who was sent the schedule, and who has actually opened it — the
/// difference between "I sent it" and "the closing server has seen it".
struct ScheduleShareStatus: Decodable, Identifiable, Hashable {
    let employeeName: String
    let sentTo: String?
    let sentAt: String?
    let viewedAt: String?
    let viewCount: Int

    var id: String { employeeName }
    var hasViewed: Bool { viewedAt != nil }

    enum CodingKeys: String, CodingKey {
        case employeeName = "employee_name"
        case sentTo = "sent_to"
        case sentAt = "sent_at"
        case viewedAt = "viewed_at"
        case viewCount = "view_count"
    }
}

struct PublishResult: Decodable {
    let ok: Bool
    let sent: [Sent]
    let unreachable: [Unreachable]
    let failed: [Failed]
    let error: String?
    let status: String?
    // True when the owner sent past the publish gate's blockers — said
    // in the result so "sent" never quietly hides that it was.
    let acknowledged: Bool?
    /// This week had already gone to staff; nothing was sent twice.
    let alreadyPublished: Bool?
    /// Published to the staff portal with no emails (nobody has an address).
    let note: String?

    enum CodingKeys: String, CodingKey {
        case ok, sent, unreachable, failed, error, status, acknowledged, note
        case alreadyPublished = "already_published"
    }

    struct Sent: Decodable, Identifiable {
        let employeeName: String
        let sentTo: String
        let shifts: Int
        var id: String { employeeName }
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case sentTo = "sent_to"
            case shifts
        }
    }

    struct Unreachable: Decodable, Identifiable {
        let employeeName: String
        let reason: String
        var id: String { employeeName }
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case reason
        }
    }

    struct Failed: Decodable, Identifiable {
        let employeeName: String
        let error: String
        var id: String { employeeName }
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case error
        }
    }
}

@Observable
@MainActor
final class PublishScheduleViewModel {
    var contacts: [StaffContact] = []
    var status: [ScheduleShareStatus] = []
    var weekLabel: String?
    var isLoading = false
    var errorMessage: String?

    var isPublishing = false
    var lastResult: PublishResult?
    // Which schedule_history row to send. Nil sends the latest, which is
    // what the Labor tab means; History passes the row it is looking at.
    var scheduleId: Int?
    // The publish gate's answer when the week has something the owner
    // should read first — needs-review rows, a rule break, a weak score.
    // Sending again with `acknowledge: true` is the owner saying they did.
    var blockers: [String] = []
    var acknowledgeBlockers = false
    // A refusal that is not a gate: the login cannot send (403), or the
    // server said no. Separate from errorMessage, which hides the whole
    // sheet behind an error when nothing has loaded.
    var publishError: String?

    var editingContact: StaffContact?
    var isSavingContact = false
    var contactError: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct ContactsResponse: Decodable {
        let ok: Bool
        let contacts: [StaffContact]
        let reachable: Int
        let weekStart: String?
        let weekEnd: String?

        enum CodingKeys: String, CodingKey {
            case ok, contacts, reachable
            case weekStart = "week_start"
            case weekEnd = "week_end"
        }
    }

    private struct StatusResponse: Decodable { let ok: Bool; let status: [ScheduleShareStatus] }
    private typealias OKErrorResponse = APIClient.OKResponse

    var reachableCount: Int { contacts.filter(\.isReachable).count }

    func load() async {
        isLoading = contacts.isEmpty
        errorMessage = nil
        defer { isLoading = false }
        do {
            let r: ContactsResponse = try await client.send("/mobile/api/labor/staff-contacts")
            contacts = r.contacts
            if let start = r.weekStart {
                weekLabel = r.weekEnd.map { "\(start) – \($0)" } ?? start
            }
            let s: StatusResponse? = try? await client.send(
                "/mobile/api/labor/schedule-share-status", hapticOnError: false)
            status = s?.status ?? []
        } catch let error as APIClient.APIError {
            if contacts.isEmpty { errorMessage = error.message }
        } catch is CancellationError {
            // View went away mid-fetch; not a failure.
        } catch {
            if contacts.isEmpty { errorMessage = "Couldn't load your staff list." }
        }
    }

    private struct ContactBody: Encodable {
        let employeeName: String
        let email: String
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case email
        }
    }

    @discardableResult
    func saveContact(_ name: String, email: String) async -> Bool {
        isSavingContact = true
        contactError = nil
        defer { isSavingContact = false }
        do {
            let r: OKErrorResponse = try await client.send(
                "/mobile/api/labor/staff-contacts", method: .post,
                body: ContactBody(employeeName: name, email: email))
            if r.ok {
                await load()
                return true
            }
            contactError = r.error ?? "Couldn't save that."
            return false
        } catch let error as APIClient.APIError {
            contactError = error.message
            return false
        } catch {
            contactError = "Couldn't save that."
            return false
        }
    }

    private struct PublishBody: Encodable {
        let scheduleId: Int?
        let acknowledge: Bool
        enum CodingKeys: String, CodingKey {
            case acknowledge
            case scheduleId = "schedule_id"
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encodeIfPresent(scheduleId, forKey: .scheduleId)
            try c.encode(acknowledge, forKey: .acknowledge)
        }
    }

    /// 409 from the gate: `{needs_ack, blockers, schedule_id}`.
    private struct GateResponse: Decodable {
        let needsAck: Bool?
        let blockers: [String]?
        let scheduleId: Int?
        enum CodingKeys: String, CodingKey {
            case blockers
            case needsAck = "needs_ack"
            case scheduleId = "schedule_id"
        }
    }

    func publish() async {
        guard !isPublishing else { return }
        isPublishing = true
        publishError = nil
        defer { isPublishing = false }
        do {
            let result: PublishResult = try await client.send(
                "/mobile/api/labor/publish-schedule", method: .post,
                body: PublishBody(scheduleId: scheduleId, acknowledge: acknowledgeBlockers))
            lastResult = result
            if result.ok {
                Haptic.success()
                blockers = []
                acknowledgeBlockers = false
            } else {
                publishError = result.error ?? "Couldn't send the schedule."
            }
            await load()
        } catch let error as APIClient.APIError {
            if error.status == 409, let gate = error.decodeBody(GateResponse.self), gate.needsAck == true {
                // Not a failure: the week has something to read first.
                blockers = gate.blockers ?? []
                if let id = gate.scheduleId { scheduleId = id }
                acknowledgeBlockers = false
                Haptic.warning()
            } else {
                // 403 lands here too — a member who can draft but not send
                // gets the server's own sentence, not a generic one.
                publishError = error.message
            }
        } catch {
            publishError = "Couldn't send the schedule."
        }
    }
}

/// Sends each member of staff their own shifts. The labor module generated
/// schedules and stopped at a CSV download — the people who actually work
/// the shifts never saw it.
struct PublishScheduleSheet: View {
    @State private var viewModel = PublishScheduleViewModel()
    @Environment(\.dismiss) private var dismiss

    /// The schedule_history row to send; nil means the latest.
    init(scheduleId: Int? = nil) {
        let vm = PublishScheduleViewModel()
        vm.scheduleId = scheduleId
        _viewModel = State(initialValue: vm)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    if viewModel.isLoading && viewModel.contacts.isEmpty {
                        CavnarSkeletonLines(widths: [1.0, 0.8, 0.6, 0.45])
                    } else if let error = viewModel.errorMessage, viewModel.contacts.isEmpty {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
                    } else if viewModel.contacts.isEmpty {
                        emptyState
                    } else {
                        if let result = viewModel.lastResult, result.ok {
                            resultCard(result)
                        }
                        staffCard
                        if !viewModel.blockers.isEmpty {
                            blockersCard
                        }
                        publishButton
                        if let error = viewModel.publishError {
                            Text(error)
                                .font(.cavnarBody(14))
                                .foregroundStyle(Color.cavnarRed)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if !viewModel.status.isEmpty {
                            statusCard
                        }
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Send to staff")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Send to staff")
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        dismiss()
                    } label: {
                        Text("Done").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .task { await viewModel.load() }
            .sheet(item: $viewModel.editingContact) { contact in
                StaffContactSheet(viewModel: viewModel, contact: contact)
            }
        }
    }

    private var staffCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .lastTextBaseline) {
                Text("THIS WEEK'S STAFF")
                    .font(.cavnarBody(13.5, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                Spacer()
                HomeMixedText.make("\(viewModel.reachableCount) of \(viewModel.contacts.count) reachable",
                                   size: 12.5, weight: 700, color: .cavnarInk3)
            }
            if let week = viewModel.weekLabel {
                Text(week).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
            }
            VStack(spacing: 0) {
                ForEach(Array(viewModel.contacts.enumerated()), id: \.element.id) { index, contact in
                    Button {
                        Haptic.light()
                        viewModel.editingContact = contact
                    } label: {
                        HStack(spacing: 10) {
                            Circle()
                                .fill(contact.isReachable ? Color.cavnarGreen : Color.cavnarAmber)
                                .frame(width: 7, height: 7)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(contact.employeeName)
                                    .font(.cavnarBody(15, weight: 600))
                                    .foregroundStyle(Color.cavnarInk)
                                Text(contact.isReachable ? contact.email : "No email yet — tap to add")
                                    .font(.cavnarBody(13))
                                    .foregroundStyle(contact.isReachable ? Color.cavnarInk3 : Color.cavnarAmber)
                            }
                            Spacer(minLength: 8)
                            Image(systemName: "chevron.right")
                                .font(.system(size: 11, weight: .bold))
                                .foregroundStyle(Color.cavnarEmber2)
                        }
                        .padding(.vertical, 11)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    if index < viewModel.contacts.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
            Text("Everyone gets a private link to their own shifts only — no logins, nothing else on the roster.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .cavnarCard()
    }

    /// Disabled while blockers stand unacknowledged: the gate said read
    /// these first, and the toggle in blockersCard is the reading.
    private var sendBlocked: Bool {
        viewModel.reachableCount == 0 || viewModel.isPublishing
            || (!viewModel.blockers.isEmpty && !viewModel.acknowledgeBlockers)
    }

    private var publishButton: some View {
        Button {
            Task { await viewModel.publish() }
        } label: {
            Group {
                if viewModel.isPublishing {
                    CavnarShimmerText(text: "Sending…")
                } else if !viewModel.blockers.isEmpty {
                    Text(viewModel.acknowledgeBlockers ? "Send anyway to \(viewModel.reachableCount) staff"
                                                       : "Read the notes above first")
                } else {
                    Text(viewModel.reachableCount > 0
                         ? "Send to \(viewModel.reachableCount) staff"
                         : "Add an email address first")
                }
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: sendBlocked))
        .disabled(sendBlocked)
    }

    /// What the gate wants read before the week goes out. Every line is
    /// the server's own — a needs-review count, a rule break, a weak
    /// score — and the switch is the owner saying they have read them.
    private var blockersCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 6) {
                Image(systemName: "hand.raised.fill")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(Color.cavnarAmber)
                Text("BEFORE THIS GOES OUT")
                    .font(.cavnarBody(13.5, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarAmber)
            }
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(viewModel.blockers.enumerated()), id: \.offset) { _, line in
                    HStack(alignment: .top, spacing: 8) {
                        Circle().fill(Color.cavnarAmber).frame(width: 5, height: 5).padding(.top, 7)
                        HomeMixedText.make(line, size: 14.5, color: .cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            AccountSwitchRow(label: "I've read these — send anyway",
                             detail: "The week goes out as it is. The result will say it was sent with these acknowledged.",
                             isOn: $viewModel.acknowledgeBlockers, showsDivider: false)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                .fill(Color.cavnarAmber.opacity(0.08)))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                .strokeBorder(Color.cavnarAmber.opacity(0.35), lineWidth: 1))
    }

    private func resultCard(_ result: PublishResult) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let line = result.alreadyPublished == true
                ? "This week was already sent to staff — nothing went out twice." : result.note {
                Text(line)
                    .font(.cavnarBody(14, weight: 600))
                    .foregroundStyle(Color.cavnarInk2)
            }
            if result.acknowledged == true {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "hand.raised.fill")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarAmber)
                    Text("Sent with acknowledged blockers")
                        .font(.cavnarBody(14, weight: 700))
                        .foregroundStyle(Color.cavnarAmber)
                }
                .padding(.bottom, 2)
            }
            ForEach(result.sent) { sent in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarGreen)
                    HomeMixedText.make("\(sent.employeeName) — \(sent.shifts) shifts sent",
                                       size: 14, weight: 600, color: .cavnarInk)
                }
            }
            ForEach(result.unreachable) { person in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "exclamationmark.circle.fill")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarAmber)
                    Text("\(person.employeeName) — \(person.reason)")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            ForEach(result.failed) { failure in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarRed)
                    Text("\(failure.employeeName) — \(failure.error)")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("WHO'S SEEN IT")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(spacing: 0) {
                ForEach(Array(viewModel.status.enumerated()), id: \.element.id) { index, row in
                    HStack(spacing: 10) {
                        Text(row.employeeName)
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk)
                        Spacer(minLength: 8)
                        if row.hasViewed {
                            HomeMixedText.make(row.viewCount > 1 ? "Opened \(row.viewCount) times" : "Opened",
                                               size: 13, weight: 700, color: .cavnarGreen)
                        } else {
                            Text("Not opened yet")
                                .font(.cavnarBody(13, weight: 600))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    .padding(.vertical, 10)
                    if index < viewModel.status.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
        }
        .cavnarCard()
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("No schedule yet")
                .font(.cavnarHeadline(17))
                .foregroundStyle(Color.cavnarInk)
            Text("Generate next week's schedule first — then you can send everyone their own shifts.")
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .cavnarCard()
    }
}

private enum StaffContactField: Hashable, CaseIterable { case email }

private struct StaffContactSheet: View {
    let viewModel: PublishScheduleViewModel
    let contact: StaffContact

    @Environment(\.dismiss) private var dismiss
    @State private var email = ""
    @FocusState private var focusedField: StaffContactField?

    private var canSubmit: Bool {
        !viewModel.isSavingContact && (email.isEmpty || email.contains("@"))
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("Where should \(contact.employeeName)'s schedule go? They'll get a private link to their own shifts — no login needed.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    CavnarFloatingField(
                        icon: "envelope", placeholder: "Email address", text: $email,
                        autocapitalization: .never, focus: $focusedField, field: .email
                    )

                    if let error = viewModel.contactError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    VStack(spacing: 10) {
                        Button {
                            Task {
                                if await viewModel.saveContact(contact.employeeName, email: email) {
                                    dismiss()
                                }
                            }
                        } label: {
                            Group {
                                if viewModel.isSavingContact {
                                    CavnarShimmerText(text: "Saving…")
                                } else {
                                    Text("Save")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit))
                        .disabled(!canSubmit)

                        Button { dismiss() } label: {
                            Text("Cancel").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle(contact.employeeName)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar(contact.employeeName) }
            .keyboardNavToolbar($focusedField)
        }
        .onAppear {
            email = contact.email
            focusedField = .email
        }
    }
}
