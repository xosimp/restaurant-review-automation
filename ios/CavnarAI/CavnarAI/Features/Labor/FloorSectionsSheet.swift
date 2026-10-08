import SwiftUI

// The restaurant's named floor sections ("Patio", "Bar", "Section 3") —
// the names a server's shift can be put in (employee audit V12). The web
// names them in the Schedule Studio's shift pane; on the phone they are an
// editable list, opened from the shift's Section menu and from the rules
// sheet beside the dining-section count (iOS parity, 10/7/26).
//
// POST /mobile/api/labor/schedule/sections only takes the whole list, so
// each add or remove re-reads the list the server holds, sends it with the
// one change, and adopts what the server answers — a section someone named
// on the web since this opened is never dropped. Whoever may draft the
// schedule (SCHEDULE_DRAFT, or an admin) may change it: the GET's
// `can_edit` is that same check.

/// POST labor/schedule/sections `{sections: [names]}`.
struct FloorSectionsBody: Encodable {
    let sections: [String]
}

@Observable
@MainActor
final class FloorSectionsStore {
    static let maxSections = 30
    static let maxLength = 40

    var names: [String] = []
    var canEdit = false
    var loaded = false
    var loadError: String?
    var busy: String?
    var error: String?
    var note: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct NamesResponse: Decodable {
        let ok: Bool?
        let sections: [String]?
        let canEdit: Bool?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case ok, sections, error
            case canEdit = "can_edit"
        }
    }

    /// A name as the server stores it: spaces collapsed, at most 40 characters.
    nonisolated static func clean(_ raw: String) -> String {
        String(raw.split(whereSeparator: \.isWhitespace).joined(separator: " ").prefix(maxLength))
    }

    /// The list with one name added, or nil with the reason it can't be.
    nonisolated static func adding(_ raw: String, to list: [String]) -> (list: [String]?, refusal: String?) {
        let name = clean(raw)
        guard !name.isEmpty else { return (nil, "Name the section.") }
        if let have = list.first(where: { $0.caseInsensitiveCompare(name) == .orderedSame }) {
            return (nil, "\(have) is already a section.")
        }
        guard list.count < maxSections else { return (nil, "You can name up to \(maxSections) sections.") }
        return (list + [name], nil)
    }

    nonisolated static func removing(_ name: String, from list: [String]) -> [String] {
        list.filter { $0.caseInsensitiveCompare(name) != .orderedSame }
    }

    private func fetch() async throws -> NamesResponse {
        try await client.send("/mobile/api/labor/schedule/sections", hapticOnError: false)
    }

    func load() async {
        do {
            let r = try await fetch()
            guard r.ok != false else { loadError = r.error ?? "The sections couldn\u{2019}t be read."; return }
            names = r.sections ?? []
            canEdit = r.canEdit ?? false
            loaded = true
            loadError = nil
        } catch is CancellationError {
        } catch let e as APIClient.APIError {
            loadError = e.message
        } catch {
            loadError = "The sections couldn\u{2019}t be read."
        }
    }

    /// Adds one name. Returns true once the server has it.
    @discardableResult
    func add(_ raw: String) async -> Bool {
        await change(busyKey: "+") { base in
            let r = Self.adding(raw, to: base)
            return (r.list, r.refusal)
        }
    }

    /// Takes one name off the list — and off every shift that was in it.
    @discardableResult
    func remove(_ name: String) async -> Bool {
        await change(busyKey: name) { base in (Self.removing(name, from: base), nil) }
    }

    private func change(busyKey: String, _ apply: ([String]) -> ([String]?, String?)) async -> Bool {
        guard busy == nil else { return false }
        busy = busyKey
        error = nil
        note = nil
        defer { busy = nil }
        // The list the server holds now, not the one this sheet opened with.
        let base = ((try? await fetch())?.sections) ?? names
        let (next, refusal) = apply(base)
        guard let next else {
            names = base
            error = refusal
            Haptic.error()
            return false
        }
        do {
            let r: NamesResponse = try await client.send(
                "/mobile/api/labor/schedule/sections", method: .post, body: FloorSectionsBody(sections: next),
                hapticOnError: false, retryTransient: false)
            guard r.ok == true, let saved = r.sections else {
                error = r.error ?? "Couldn\u{2019}t save the sections."
                Haptic.error()
                return false
            }
            names = saved
            note = saved.isEmpty ? "Sections removed" : "Sections saved"
            Haptic.success()
            return true
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t save the sections."
        }
        Haptic.error()
        return false
    }
}

/// The section names as a list: add one at the bottom, remove one with its
/// ✕ (confirmed — it comes off every shift). Each change saves at once.
struct FloorSectionsSheet: View {
    var onChanged: (() async -> Void)? = nil

    @State private var store = FloorSectionsStore()
    @State private var newName = ""
    @State private var removing: String?
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("The sections a server\u{2019}s shift can be put in \u{2014} Patio, Bar, Section 3. The staff app shows each server theirs beside the shift.")
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    if !store.loaded {
                        if let error = store.loadError {
                            ScheduleNotice(text: error, tone: .cavnarRed) {
                                Button {
                                    Haptic.light()
                                    Task { await store.load() }
                                } label: { Text("Try again").frame(maxWidth: .infinity) }
                                    .buttonStyle(CavnarSecondaryButtonStyle())
                            }
                        } else {
                            CavnarSkeletonLines(widths: [1.0, 0.7, 0.85])
                        }
                    } else {
                        list
                        if store.canEdit { addRow } else {
                            Text("Your login can see the sections but not change them.")
                                .font(.cavnarBody(13.5, weight: 600))
                                .foregroundStyle(Color.cavnarAmber)
                        }
                        status
                    }
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.immediately)
            .accountSheetChrome("Floor sections")
        }
        .task { await store.load() }
        .confirmationDialog(removing.map { "Remove \($0)? It comes off every shift it is on." } ?? "",
                            isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }),
                            titleVisibility: .visible) {
            if let name = removing {
                Button("Remove \(name)", role: .destructive) {
                    removing = nil
                    Task { if await store.remove(name) { await onChanged?() } }
                }
            }
            Button("Keep it", role: .cancel) { removing = nil }
        }
    }

    @ViewBuilder
    private var list: some View {
        AccountKicker(text: store.names.isEmpty ? "Sections" : "Sections \u{00B7} \(store.names.count)")
        if store.names.isEmpty {
            Text("No sections named yet.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
                .italic()
        } else {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(store.names.enumerated()), id: \.element) { i, name in
                    let last = i == store.names.count - 1
                    if store.canEdit {
                        AccountActionRow(label: name, symbol: "xmark", tone: .cavnarRed,
                                         busy: store.busy == name, showsDivider: !last) {
                            removing = name
                        }
                    } else {
                        AccountKVRow(label: name, showsDivider: !last) { EmptyView() }
                    }
                }
            }
            .accountCard()
        }
    }

    private var trimmed: String { FloorSectionsStore.clean(newName) }

    private var addRow: some View {
        HStack(spacing: 10) {
            TextField("Add a section, e.g. Patio", text: $newName)
                .cavnarTextFieldStyle()
                .textInputAutocapitalization(.words)
                .autocorrectionDisabled()
                .focused($focused)
                .submitLabel(.done)
                .onSubmit { add() }
                .onChange(of: newName) { _, v in
                    if v.count > FloorSectionsStore.maxLength { newName = String(v.prefix(FloorSectionsStore.maxLength)) }
                }
            Button("Add") { add() }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: trimmed.isEmpty || store.busy != nil))
                .disabled(trimmed.isEmpty || store.busy != nil)
                .fixedSize()
        }
    }

    @ViewBuilder
    private var status: some View {
        if store.busy != nil {
            CavnarShimmerText(text: "Saving\u{2026}", color: .cavnarInk3)
        } else if let error = store.error {
            Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                .fixedSize(horizontal: false, vertical: true)
        } else if let note = store.note {
            Text(note).font(.cavnarBody(14)).foregroundStyle(Color.cavnarGreen)
        }
    }

    private func add() {
        let name = trimmed
        guard !name.isEmpty else { return }
        Task {
            if await store.add(name) {
                newName = ""
                await onChanged?()
            }
        }
    }
}
