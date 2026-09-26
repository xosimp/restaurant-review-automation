import SwiftUI

// The rest of the web staff portal on the phone (web-vs-iOS parity,
// 9/25/26): Before service, My availability, What I'd like (with the
// "text me when my schedule is posted" opt-in) and Change your PIN. Same
// /staff/api/* routes as templates/staff_portal.html, one for one — there
// are no mobile twins for the staff tier.

// MARK: - Payloads

struct StaffPreshiftItem: Decodable, Hashable {
    let kind: String
    let text: String
}

struct StaffPreshiftResponse: Decodable {
    let ok: Bool
    let items: [StaffPreshiftItem]?
}

struct StaffAvailabilityResponse: Decodable {
    let ok: Bool
    let error: String?
    let unavailableDays: [String]?
    let notes: String?
    let updatedAt: String?

    enum CodingKeys: String, CodingKey {
        case ok, error, notes
        case unavailableDays = "unavailable_days"
        case updatedAt = "updated_at"
    }
}

struct StaffPreferencesResponse: Decodable {
    let ok: Bool
    let error: String?
    let preferredDayparts: [String]?
    let desiredHours: Double?
    let scheduleTexts: Bool?

    enum CodingKeys: String, CodingKey {
        case ok, error
        case preferredDayparts = "preferred_dayparts"
        case desiredHours = "desired_hours"
        case scheduleTexts = "schedule_texts"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
        error = try? c.decodeIfPresent(String.self, forKey: .error)
        preferredDayparts = try? c.decodeIfPresent([String].self, forKey: .preferredDayparts)
        if let d = try? c.decodeIfPresent(Double.self, forKey: .desiredHours) {
            desiredHours = d
        } else if let s = try? c.decodeIfPresent(String.self, forKey: .desiredHours) {
            desiredHours = Double(s)
        } else {
            desiredHours = nil
        }
        scheduleTexts = try? c.decodeIfPresent(Bool.self, forKey: .scheduleTexts)
    }
}

/// Section label and card surfaces the portal's own screens use.
private enum StaffKit {
    static func label(_ text: String) -> some View {
        Text(text)
            .font(.cavnarBody(11, weight: 700))
            .kerning(1.3)
            .foregroundStyle(Color.cavnarEmber2)
            .padding(.top, 6)
    }

    static func note(_ text: String, error: Bool = false) -> some View {
        Text(text)
            .font(.cavnarBody(13))
            .foregroundStyle(error ? Color.cavnarRed : Color.cavnarInk3)
            .fixedSize(horizontal: false, vertical: true)
    }

    /// A day or daypart that is on or off — the web portal's toggle chips.
    static func toggleChip(_ title: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.selection()
            action()
        } label: {
            Text(title)
                .font(.cavnarBody(14, weight: 600))
                .foregroundStyle(on ? Color.white : Color.cavnarInk2)
                .frame(maxWidth: .infinity, minHeight: 40)
                .background(on ? Color.cavnarEmber : Color.cavnarPaper3.opacity(0.5),
                            in: RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

// MARK: - Before service

/// Today's lineup briefing (preshift.py) — staff-safe by construction: no
/// money, no names. Shows nothing when there is nothing, or it failed —
/// it is a bonus, not the page (as the web portal).
struct StaffPreshiftCard: View {
    @Environment(StaffSessionStore.self) private var staff
    @State private var items: [StaffPreshiftItem] = []

    private static let tags = ["volume": "Volume", "watch": "Watch", "stock": "86 risk",
                               "event": "Today", "weather": "Weather"]

    var body: some View {
        Group {
            if !items.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    StaffKit.label("BEFORE SERVICE")
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text((Self.tags[item.kind] ?? item.kind).uppercased())
                                    .font(.cavnarBody(10, weight: 700))
                                    .kerning(0.6)
                                    .foregroundStyle(Color.cavnarEmber2)
                                    .padding(.horizontal, 6)
                                    .padding(.vertical, 2)
                                    .background(Capsule().fill(Color.cavnarEmber.opacity(0.12)))
                                HomeMixedText.make(item.text, size: 14.5, color: .cavnarInk)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(14)
                    .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
                }
                .padding(.bottom, 4)
            }
        }
        .task {
            let r: StaffPreshiftResponse? = try? await staff.authed("/staff/api/preshift")
            items = r?.items ?? []
        }
    }
}

// MARK: - My availability

/// The days I can't work, entered by me; the manager's next draft reads it.
struct StaffAvailabilitySection: View {
    @Environment(StaffSessionStore.self) private var staff
    @State private var blocked: Set<String> = []
    @State private var notes = ""
    @State private var status: String?
    @State private var failed = false
    @State private var saving = false
    @State private var loaded = false

    private static let days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StaffKit.label("MY AVAILABILITY")
            StaffKit.note("Tap the days you can\u{2019}t work. Your manager\u{2019}s next schedule reads this.")
            if loaded {
                HStack(spacing: 5) {
                    ForEach(Self.days, id: \.self) { day in
                        StaffKit.toggleChip(String(day.prefix(3)), on: blocked.contains(day)) {
                            if blocked.contains(day) { blocked.remove(day) } else { blocked.insert(day) }
                        }
                        .accessibilityLabel(blocked.contains(day) ? "\(day), can't work" : day)
                    }
                }
                TextField("Anything else \u{2014} \u{201C}not before 10am\u{201D}, \u{201C}away Oct 3\u{2013}6\u{201D}",
                          text: $notes, axis: .vertical)
                    .font(.cavnarBody(15))
                    .lineLimit(1...3)
                    .padding(12)
                    .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 10))
                    .onChange(of: notes) { _, v in if v.count > 300 { notes = String(v.prefix(300)) } }
                HStack(spacing: 12) {
                    Button {
                        Task { await save() }
                    } label: {
                        Group {
                            if saving { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Save") }
                        }
                        .frame(minWidth: 90)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: saving))
                    .disabled(saving)
                    if let status { StaffKit.note(status, error: failed) }
                }
            } else {
                CavnarSkeletonLines(widths: [1.0, 0.7])
            }
        }
        .task { await load() }
    }

    private func load() async {
        guard let r: StaffAvailabilityResponse = try? await staff.authed("/staff/api/availability"), r.ok else {
            loaded = true
            return
        }
        blocked = Set(r.unavailableDays ?? [])
        notes = r.notes ?? ""
        if let at = r.updatedAt, !at.isEmpty { status = "Last saved " + CavnarDate.mdy(at) }
        loaded = true
    }

    private struct SaveBody: Encodable {
        let unavailableDays: [String]
        let notes: String
        enum CodingKeys: String, CodingKey {
            case notes
            case unavailableDays = "unavailable_days"
        }
    }

    private func save() async {
        saving = true
        failed = false
        defer { saving = false }
        do {
            let r: StaffAvailabilityResponse = try await staff.authed(
                "/staff/api/availability", method: .post,
                body: SaveBody(unavailableDays: Self.days.filter { blocked.contains($0) }, notes: notes))
            if r.ok {
                Haptic.success()
                status = "Saved \u{2014} your manager\u{2019}s next schedule will use it."
            } else {
                failed = true
                status = r.error ?? "Could not save."
            }
        } catch let error as APIClient.APIError {
            failed = true
            status = error.message
        } catch {
            failed = true
            status = "Could not save \u{2014} check your connection."
        }
    }
}

// MARK: - What I'd like

/// Preferred dayparts and hours a week — a wish the draft honours where
/// coverage allows, never over a rule — and the schedule-texts opt-in,
/// its own consent, saved the moment it changes.
struct StaffPreferencesSection: View {
    @Environment(StaffSessionStore.self) private var staff
    @State private var dayparts: Set<String> = []
    @State private var hours = ""
    @State private var texts = false
    @State private var status: String?
    @State private var textsStatus: String?
    @State private var failed = false
    @State private var saving = false
    @State private var loaded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StaffKit.label("WHAT I\u{2019}D LIKE")
            StaffKit.note("The shifts you\u{2019}d rather have, and how many hours a week you want.")
            if loaded {
                HStack(spacing: 8) {
                    StaffKit.toggleChip("Days", on: dayparts.contains("morning")) { flip("morning") }
                    StaffKit.toggleChip("Nights", on: dayparts.contains("night")) { flip("night") }
                }
                HStack {
                    Text("Hours a week").font(.cavnarBody(14.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                    Spacer()
                    TextField("\u{2014}", text: $hours)
                        .keyboardType(.numberPad)
                        .multilineTextAlignment(.trailing)
                        .font(.cavnarNumber(16, weight: 600))
                        .frame(width: 70)
                        .onChange(of: hours) { _, v in
                            let digits = String(v.filter(\.isNumber).prefix(2))
                            if digits != v { hours = digits }
                        }
                }
                .padding(12)
                .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 10))
                HStack(spacing: 12) {
                    Button {
                        Task { await save() }
                    } label: {
                        Group {
                            if saving { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Save") }
                        }
                        .frame(minWidth: 90)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: saving))
                    .disabled(saving)
                    if let status { StaffKit.note(status, error: failed) }
                }
                StaffKit.note("A wish, not a rule \u{2014} the schedule honours it where coverage allows.")
                Toggle(isOn: Binding(get: { texts }, set: { on in
                    texts = on
                    Task { await saveTexts(on) }
                })) {
                    Text("Text me when my schedule is posted. Message and data rates may apply; reply STOP to stop.")
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .tint(Color.cavnarEmber)
                .padding(.top, 6)
                if let textsStatus { StaffKit.note(textsStatus) }
            } else {
                CavnarSkeletonLines(widths: [1.0, 0.6])
            }
        }
        .task { await load() }
    }

    private func flip(_ part: String) {
        if dayparts.contains(part) { dayparts.remove(part) } else { dayparts.insert(part) }
    }

    private func load() async {
        guard let r: StaffPreferencesResponse = try? await staff.authed("/staff/api/preferences"), r.ok else {
            loaded = true
            return
        }
        dayparts = Set(r.preferredDayparts ?? [])
        hours = r.desiredHours.map { String(Int($0.rounded())) } ?? ""
        texts = r.scheduleTexts ?? false
        loaded = true
    }

    private struct SaveBody: Encodable {
        let preferredDayparts: [String]
        let desiredHours: Int?
        enum CodingKeys: String, CodingKey {
            case preferredDayparts = "preferred_dayparts"
            case desiredHours = "desired_hours"
        }
        // desired_hours is sent as null when cleared, as the web does.
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(preferredDayparts, forKey: .preferredDayparts)
            try c.encode(desiredHours, forKey: .desiredHours)
        }
    }

    private func save() async {
        saving = true
        failed = false
        defer { saving = false }
        do {
            let order = ["morning", "night"].filter { dayparts.contains($0) }
            let r: StaffPreferencesResponse = try await staff.authed(
                "/staff/api/preferences", method: .post,
                body: SaveBody(preferredDayparts: order, desiredHours: Int(hours)))
            if r.ok {
                Haptic.success()
                status = "Saved \u{2014} the next draft reads it."
            } else {
                failed = true
                status = r.error ?? "Could not save."
            }
        } catch let error as APIClient.APIError {
            failed = true
            status = error.message
        } catch {
            failed = true
            status = "Could not save \u{2014} check your connection."
        }
    }

    private func saveTexts(_ on: Bool) async {
        do {
            let r: StaffPreferencesResponse = try await staff.authed(
                "/staff/api/preferences", method: .post, body: ["schedule_texts": on])
            if r.ok {
                texts = r.scheduleTexts ?? on
                textsStatus = texts ? "Saved \u{2014} you\u{2019}ll get a text when a week is posted."
                                    : "Saved \u{2014} no schedule texts."
            } else {
                texts = !on
                textsStatus = r.error ?? "Could not save."
            }
        } catch {
            texts = !on
            textsStatus = "Could not save \u{2014} check your connection."
        }
    }
}

// MARK: - Change your PIN

/// Needs the current PIN — a shared device left signed in must not let the
/// next person lock out its owner. The change ends this person's staff
/// sessions (this one too), so the app signs out and they sign in again
/// with the new PIN, as the web portal does.
struct StaffChangePinSection: View {
    @Environment(StaffSessionStore.self) private var staff
    @State private var current = ""
    @State private var newPin = ""
    @State private var confirm = ""
    @State private var message: String?
    @State private var ok = false
    @State private var saving = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StaffKit.label("CHANGE YOUR PIN")
            VStack(spacing: 8) {
                pinField("Current PIN", text: $current)
                pinField("New PIN", text: $newPin)
                pinField("Confirm new PIN", text: $confirm)
            }
            Button {
                Task { await save() }
            } label: {
                Group {
                    if saving { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Save new PIN") }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(saving || current.isEmpty || newPin.isEmpty)
            if let message {
                Text(message)
                    .font(.cavnarBody(13.5, weight: 600))
                    .foregroundStyle(ok ? Color.cavnarGreen : Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func pinField(_ placeholder: String, text: Binding<String>) -> some View {
        SecureField(placeholder, text: text)
            .keyboardType(.numberPad)
            .textContentType(.oneTimeCode)
            .font(.cavnarNumber(17, weight: 600))
            .padding(12)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 10))
            .onChange(of: text.wrappedValue) { _, v in
                let digits = String(v.filter(\.isNumber).prefix(8))
                if digits != v { text.wrappedValue = digits }
            }
    }

    private struct SaveBody: Encodable {
        let currentPin: String
        let newPin: String
        enum CodingKeys: String, CodingKey {
            case currentPin = "current_pin"
            case newPin = "new_pin"
        }
    }

    private func save() async {
        ok = false
        guard newPin == confirm else {
            message = "The two new PINs don\u{2019}t match."
            return
        }
        saving = true
        defer { saving = false }
        do {
            let r: StaffOKResponse = try await staff.authed(
                "/staff/api/pin", method: .post, body: SaveBody(currentPin: current, newPin: newPin))
            guard r.ok else {
                message = r.error ?? "Could not change your PIN."
                return
            }
            Haptic.success()
            ok = true
            message = "PIN changed. Signing you back in\u{2026}"
            current = ""; newPin = ""; confirm = ""
            try? await Task.sleep(nanoseconds: 1_400_000_000)
            staff.signOut()
        } catch let error as APIClient.APIError {
            message = error.message
        } catch {
            message = "Could not change your PIN."
        }
    }
}
