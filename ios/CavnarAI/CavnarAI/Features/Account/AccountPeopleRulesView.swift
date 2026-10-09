import SwiftUI
import Observation
@preconcurrency import Vision

/// Account → People (parity audit 10/7/26 #62) — the web's People section:
/// what staff can look up in the app — the house rules their questions are
/// answered from, read-only docs by job, and certifications with the expiry
/// reminder. iOS readability round: the phone reads the rules and docs and
/// adds a certificate on the spot; writing the rules and the docs is the
/// web's (one row each), and "Who has access" left — it repeated Account's
/// Team rows.
/// /mobile/api/house-rules, /staff-docs, /staff-certs (staff_knowledge_routes,
/// one body per route with the web). Every add, change and removal saves on
/// its own; anything staff will read asks first.
///
/// `account/people` — a notification, the command sheet, the certificate
/// expiry email's "Open certifications" — opens this screen.
@Observable
@MainActor
final class PeopleRulesViewModel {
    private(set) var houseRules: KnowledgeDoc?
    private(set) var canEditRules = false
    private(set) var rulesLoaded = false
    private(set) var docs: [KnowledgeDoc] = []
    private(set) var docKinds: [StaffDocKind] = []
    private(set) var canEditDocs = false
    private(set) var docsLoaded = false
    private(set) var certs: [KnowledgeCert] = []
    private(set) var roster: [String] = []
    private(set) var remindDays: Int?
    private(set) var canEditCerts = false
    private(set) var certsLoaded = false
    /// A read that failed, in a sentence: the section shows it with Try
    /// again instead of a skeleton that never resolves (re-audit 10/8/26).
    private(set) var rulesError: String?
    private(set) var docsError: String?
    private(set) var certsError: String?
    var busy = false
    var message: String?
    var error: String?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    static let rulesPath = "/mobile/api/house-rules"
    static let docsPath = "/mobile/api/staff-docs"
    static let certsPath = "/mobile/api/staff-certs"
    static func docRemovePath(_ id: Int) -> String { "/mobile/api/staff-docs/\(id)/remove" }
    static func certRemovePath(_ id: Int) -> String { "/mobile/api/staff-certs/\(id)/remove" }

    func load() async {
        async let r: Void = loadRules()
        async let d: Void = loadDocs()
        async let c: Void = loadCerts()
        _ = await (r, d, c)
    }

    /// The sentence a failed read shows: the server's own, else a plain
    /// "couldn't reach".
    nonisolated static func loadFailure(_ error: Error?, refused: String? = nil) -> String {
        if let refused, !refused.isEmpty { return refused }
        if let e = error as? APIClient.APIError, !e.message.isEmpty { return e.message }
        return "Couldn\u{2019}t reach Cavnar AI."
    }

    func loadRules() async {
        do {
            let r: HouseRulesResponse = try await client.send(Self.rulesPath, hapticOnError: false)
            guard r.ok else { rulesError = Self.loadFailure(nil, refused: r.error); return }
            houseRules = r.houseRules
            canEditRules = r.canEdit
            rulesLoaded = true
            rulesError = nil
        } catch {
            rulesError = Self.loadFailure(error)
        }
    }

    func loadDocs() async {
        do {
            let r: StaffDocsResponse = try await client.send(Self.docsPath, hapticOnError: false)
            guard r.ok else { docsError = Self.loadFailure(nil, refused: r.error); return }
            docs = r.docs
            docKinds = r.kinds
            canEditDocs = r.canEdit
            docsLoaded = true
            docsError = nil
        } catch {
            docsError = Self.loadFailure(error)
        }
    }

    func loadCerts() async {
        do {
            let r: StaffCertsResponse = try await client.send(Self.certsPath, hapticOnError: false)
            guard r.ok else { certsError = Self.loadFailure(nil, refused: r.error); return }
            certs = r.certs
            roster = r.roster
            remindDays = r.remindDays
            canEditCerts = r.canEdit
            certsLoaded = true
            certsError = nil
        } catch {
            certsError = Self.loadFailure(error)
        }
    }

    /// One write; `then` re-reads what it changed. Returns whether it saved.
    @discardableResult
    private func post(_ path: String, body: some Encodable & Sendable, ok: String, then: () async -> Void) async -> Bool {
        busy = true
        error = nil
        message = nil
        defer { busy = false }
        do {
            let r: APIClient.OKResponse = try await client.send(path, method: .post, body: body, retryTransient: false)
            guard r.ok else { error = r.error ?? "Couldn\u{2019}t save that."; return false }
            Haptic.success()
            message = ok
            await then()
            return true
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t reach Cavnar AI."
        }
        return false
    }

    func saveRules(_ body: String) async -> Bool {
        await post(Self.rulesPath, body: HouseRulesBody(body: body), ok: "Saved \u{2014} staff see it now.") { await loadRules() }
    }

    func saveDoc(_ body: StaffDocSaveBody) async -> Bool {
        await post(Self.docsPath, body: body, ok: "Saved.") { await loadDocs() }
    }

    func removeDoc(_ doc: KnowledgeDoc) async {
        await post(Self.docRemovePath(doc.id), body: TeamEmptyBody(), ok: "Removed.") { await loadDocs() }
    }

    func saveCert(_ body: StaffCertSaveBody) async -> Bool {
        await post(Self.certsPath, body: body, ok: "Saved.") { await loadCerts() }
    }

    func removeCert(_ cert: KnowledgeCert) async {
        await post(Self.certRemovePath(cert.id), body: TeamEmptyBody(), ok: "Removed.") { await loadCerts() }
    }
}

struct AccountPeopleRulesView: View {
    let accountViewModel: AccountViewModel
    let isOwner: Bool

    @State private var model = PeopleRulesViewModel()
    @State private var editingDoc: StaffDocEditorTarget?
    @State private var editingCert: StaffCertEditorTarget?
    @State private var removingCert: KnowledgeCert?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "House rules & docs") {
                        GlowBadge(systemImage: "person.3", size: 64)
                    } subtitle: {
                        Text("What staff can look up in the app")
                    }
                    if let m = model.message {
                        Text(m).cavnarText(.secondary, color: .cavnarGreen)
                    }
                    if let e = model.error {
                        Text(e).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    rules
                    docs
                    certs
                }
                .padding(20)
            }
            .cavnarEmberRefreshable { await model.load() }
            .accountSheetChrome("People")
        }
        .task { await model.load() }
        .sheet(item: $editingDoc) { target in
            StaffDocEditor(target: target, kinds: model.docKinds) { await model.saveDoc($0) }
                .presentationDetents([.large])
        }
        .sheet(item: $editingCert) { target in
            StaffCertEditor(target: target, roster: model.roster) { await model.saveCert($0) }
                .presentationDetents([.large])
        }
        .confirmationDialog(removingCert.map { "Remove \($0.employeeName)\u{2019}s \($0.certLabel) certificate?" } ?? "",
                            isPresented: Binding(get: { removingCert != nil }, set: { if !$0 { removingCert = nil } }),
                            titleVisibility: .visible) {
            Button("Remove it", role: .destructive) {
                if let c = removingCert { Task { await model.removeCert(c) } }
                removingCert = nil
            }
            Button("Keep it", role: .cancel) { removingCert = nil }
        } message: { Text("The schedule stops counting it, and nobody is reminded before it expires.") }
    }

    // MARK: House rules

    @ViewBuilder
    private var rules: some View {
        if model.rulesLoaded {
            AccountSection(kicker: "House rules") {
                VStack(alignment: .leading, spacing: 10) {
                    // One line (iOS readability round — it was a paragraph).
                    Text("Staff\u{2019}s questions in the app are answered from these.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let hr = model.houseRules, !hr.body.isEmpty {
                        Text(hr.body)
                            .cavnarText(.body)
                            .fixedSize(horizontal: false, vertical: true)
                        if let at = hr.updatedAt {
                            HomeMixedText.make("Saved " + CavnarDate.mdy(at), role: .caption)
                        }
                    } else {
                        Text("No house rules yet.").cavnarText(.body)
                    }
                    // Writing them is the web's: staff read every line the
                    // moment it saves.
                    if model.canEditRules {
                        CavnarWebLinkRow(title: "House rules", path: "account/people")
                    }
                }
                .padding(.vertical, 10)
            }
        } else if let e = model.rulesError {
            loadFailed("House rules", what: "the house rules", message: e) { await model.loadRules() }
        } else {
            CavnarSkeletonLines(widths: [0.4, 1.0, 0.8])
        }
    }

    /// A section whose read failed: its kicker, the sentence, Try again —
    /// never a skeleton that never resolves (re-audit 10/8/26).
    private func loadFailed(_ kicker: String, what: String, message: String,
                            retry: @escaping () async -> Void) -> some View {
        AccountSection(kicker: kicker) {
            StaffLoadFailed(what: what, message: message, retry: retry)
                .padding(.vertical, 10)
        }
    }

    // MARK: Docs

    @ViewBuilder
    private var docs: some View {
        if model.docsLoaded {
            VStack(alignment: .leading, spacing: 8) {
                CavnarKicker("Staff docs")
                Text("Menu specs, allergens and how you do things \u{2014} read only in the app.")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                VStack(alignment: .leading, spacing: 0) {
                    if model.docs.isEmpty {
                        Text("No docs yet.")
                            .cavnarText(.body)
                            .padding(.vertical, 12)
                    }
                    ForEach(Array(model.docs.enumerated()), id: \.element.id) { i, d in
                        docRow(d, divider: i < model.docs.count - 1 || model.canEditDocs)
                    }
                    // Writing and changing docs is the web's (iOS
                    // readability round); the phone reads them.
                    if model.canEditDocs {
                        CavnarWebLinkRow(title: "Staff docs", path: "account/people")
                    }
                }
                .accountCard()
            }
        } else if let e = model.docsError {
            loadFailed("Staff docs", what: "the staff docs", message: e) { await model.loadDocs() }
        } else {
            CavnarSkeletonLines(widths: [0.3, 0.9])
        }
    }

    private func docRow(_ d: KnowledgeDoc, divider: Bool) -> some View {
        Button {
            Haptic.light()
            editingDoc = StaffDocEditorTarget(doc: d, readOnly: true)
        } label: {
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(d.title)
                            .cavnarText(.label)
                            .lineLimit(2)
                        HomeMixedText.make(d.metaLine, role: .caption)
                            .lineLimit(2)
                    }
                    Spacer(minLength: 6)
                    AccountDisclosureChip()
                }
                .padding(.vertical, 11)
                if divider { AccountRowDivider() }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    // MARK: Certifications

    @ViewBuilder
    private var certs: some View {
        if model.certsLoaded {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    CavnarKicker("Certifications")
                    Spacer()
                    if model.canEditCerts {
                        addButton("Add a certificate") { editingCert = StaffCertEditorTarget(cert: nil) }
                    }
                }
                HomeMixedText.make("Who holds which certificate until when. The holder and you are told "
                                   + (model.remindDays.map { "\($0) days" } ?? "a month")
                                   + " before one expires.", role: .secondary)
                    .fixedSize(horizontal: false, vertical: true)
                VStack(alignment: .leading, spacing: 0) {
                    if model.certs.isEmpty {
                        Text("No certificates on file yet.")
                            .cavnarText(.body)
                            .padding(.vertical, 12)
                    }
                    ForEach(Array(model.certs.enumerated()), id: \.element.id) { i, c in
                        certRow(c, divider: i < model.certs.count - 1)
                    }
                }
                .accountCard()
            }
        } else if let e = model.certsError {
            loadFailed("Certifications", what: "the certifications", message: e) { await model.loadCerts() }
        } else {
            CavnarSkeletonLines(widths: [0.3, 0.9])
        }
    }

    private func certRow(_ c: KnowledgeCert, divider: Bool) -> some View {
        Button {
            guard model.canEditCerts else { return }
            Haptic.light()
            editingCert = StaffCertEditorTarget(cert: c)
        } label: {
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(c.employeeName)
                            .cavnarText(.label)
                            .lineLimit(1)
                        Text(c.certLabel.capitalized + ((c.note ?? "").isEmpty ? "" : " \u{00B7} \(c.note!)"))
                            .cavnarText(.caption)
                            .lineLimit(2)
                    }
                    Spacer(minLength: 6)
                    AccountChip(text: c.expiryLine, muted: c.status == "current" || c.status == "no_expiry",
                                tint: c.status == "expired" ? .cavnarRed : (c.status == "expiring" ? .cavnarAmber : nil))
                }
                .padding(.vertical, 10)
                if divider { AccountRowDivider() }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .contextMenu {
            if model.canEditCerts {
                Button { editingCert = StaffCertEditorTarget(cert: c) } label: { Label("Edit", systemImage: "pencil") }
                Button(role: .destructive) { removingCert = c } label: { Label("Remove", systemImage: "trash") }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func addButton(_ label: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            Label("Add", systemImage: "plus")
                .cavnarText(.label, color: .cavnarEmber2)
                .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

// MARK: - Doc editor

struct StaffDocEditorTarget: Identifiable {
    let doc: KnowledgeDoc?
    var readOnly = false
    var id: String { doc.map { "doc-\($0.id)" } ?? "doc-new" }
}

/// One doc: its kind (a native picker), title, the jobs it is for, and the
/// text. Saving asks first — staff read it the moment it saves.
struct StaffDocEditor: View {
    let target: StaffDocEditorTarget
    let kinds: [StaffDocKind]
    let save: (StaffDocSaveBody) async -> Bool

    @Environment(\.dismiss) private var dismiss
    @State private var kind = "menu_spec"
    @State private var title = ""
    @State private var roles = ""
    @State private var text = ""
    @State private var confirming = false
    @State private var saving = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if target.readOnly, let d = target.doc {
                        Text(d.title).font(.cavnarHeadline(CavnarText.title.size)).foregroundStyle(Color.cavnarInk)
                        HomeMixedText.make(d.metaLine, size: CavnarType.caption, color: .cavnarInk3)
                        Text(d.body).font(.cavnarBody(CavnarType.body)).foregroundStyle(Color.cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        AccountSection(kicker: "Doc") {
                            AccountKVRow(label: "Kind") {
                                Picker("Kind", selection: $kind) {
                                    ForEach(kinds) { Text($0.label).tag($0.kind) }
                                }
                                .pickerStyle(.menu)
                                .tint(Color.cavnarEmber2)
                            }
                            VStack(alignment: .leading, spacing: 10) {
                                TextField("Title", text: $title)
                                    .cavnarTextFieldStyle()
                                    .onChange(of: title) { _, v in if v.count > 120 { title = String(v.prefix(120)) } }
                                TextField("For jobs \u{2014} Server, Bartender (blank is everyone)", text: $roles)
                                    .cavnarTextFieldStyle()
                                    .textInputAutocapitalization(.words)
                                TextField("Text", text: $text, axis: .vertical)
                                    .lineLimit(6...20)
                                    .cavnarTextFieldStyle()
                                    .onChange(of: text) { _, v in if v.count > 20000 { text = String(v.prefix(20000)) } }
                            }
                            .padding(.vertical, 10)
                        }
                        if let error {
                            Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Button {
                            Haptic.light()
                            if title.trimmingCharacters(in: .whitespaces).isEmpty { error = "Give it a title." }
                            else { error = nil; confirming = true }
                        } label: {
                            Group {
                                if saving { CavnarShimmerText(text: "Saving\u{2026}") }
                                else { Text(target.doc == nil ? "Add doc" : "Save changes") }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: saving))
                        .disabled(saving)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome(target.doc == nil ? "New doc" : (target.readOnly ? "Doc" : "Edit doc"))
        }
        .onAppear {
            if let d = target.doc {
                kind = d.kind
                title = d.title
                roles = d.roles.joined(separator: ", ")
                text = d.body
            } else if let first = kinds.first {
                kind = first.kind
            }
        }
        .confirmationDialog("Save it for staff?", isPresented: $confirming, titleVisibility: .visible) {
            Button(target.doc == nil ? "Add it" : "Save it") {
                Task {
                    saving = true
                    let ok = await save(StaffDocSaveBody(id: target.doc?.id, kind: kind, title: title, body: text,
                                                         roles: StaffDocSaveBody.roles(from: roles)))
                    saving = false
                    if ok { dismiss() } else { error = "It wasn\u{2019}t saved \u{2014} see the message on People." }
                }
            }
            Button("Not yet", role: .cancel) {}
        } message: {
            Text("Staff it is for read it in the app right away.")
        }
    }
}

// MARK: - Certificate editor, with a scan

struct StaffCertEditorTarget: Identifiable {
    let cert: KnowledgeCert?
    var id: String { cert.map { "cert-\($0.id)" } ?? "cert-new" }
}

/// One person's certificate: who (the roster), which (a picker of the
/// usual kinds, or the owner's own word), when it expires and was issued
/// (date pickers), a note — and a VisionKit scan of the card that fills the
/// dates and kind it can read, on the phone, for the owner to check. The
/// image never leaves the phone.
struct StaffCertEditor: View {
    let target: StaffCertEditorTarget
    let roster: [String]
    let save: (StaffCertSaveBody) async -> Bool

    static let kinds = ["food handler", "alcohol", "allergen", "manager", "first aid"]
    private static let other = "__other__"

    @Environment(\.dismiss) private var dismiss
    @State private var person = ""
    @State private var kindChoice = "food handler"
    @State private var customKind = ""
    @State private var expires = ""
    @State private var issued = ""
    @State private var note = ""
    @State private var scanning = false
    @State private var reading = false
    @State private var scanNote: String?
    @State private var saving = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if DocumentCameraView.isAvailable {
                        Button {
                            Haptic.light()
                            scanning = true
                        } label: {
                            Label(reading ? "Reading the card\u{2026}" : "Scan the certificate", systemImage: "doc.viewfinder")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSoftButtonStyle(isDisabled: reading))
                        .disabled(reading)
                    }
                    if let scanNote {
                        Text(scanNote)
                            .font(.cavnarBody(CavnarType.secondary))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    AccountSection(kicker: "Certificate") {
                        AccountKVRow(label: "Who") {
                            if target.cert != nil {
                                AccountValue(text: person)
                            } else {
                                Picker("Who", selection: $person) {
                                    Text("Pick someone").tag("")
                                    ForEach(roster, id: \.self) { Text($0).tag($0) }
                                }
                                .pickerStyle(.menu)
                                .tint(Color.cavnarEmber2)
                            }
                        }
                        AccountKVRow(label: "Kind") {
                            if target.cert != nil {
                                AccountValue(text: currentKind.capitalized)
                            } else {
                                Picker("Kind", selection: $kindChoice) {
                                    ForEach(Self.kinds, id: \.self) { Text($0.capitalized).tag($0) }
                                    Text("Something else").tag(Self.other)
                                }
                                .pickerStyle(.menu)
                                .tint(Color.cavnarEmber2)
                            }
                        }
                        if target.cert == nil && kindChoice == Self.other {
                            TextField("Certificate name", text: $customKind)
                                .cavnarTextFieldStyle()
                                .padding(.vertical, 8)
                        }
                        AccountKVRow(label: "Expires") {
                            dateChip($expires, name: "Expires")
                        }
                        AccountKVRow(label: "Issued", showsDivider: false) {
                            dateChip($issued, name: "Issued")
                        }
                    }
                    AccountSection(kicker: "Note") {
                        TextField("Optional \u{2014} certificate number, issuer", text: $note)
                            .cavnarTextFieldStyle()
                            .padding(.vertical, 8)
                            .onChange(of: note) { _, v in if v.count > 200 { note = String(v.prefix(200)) } }
                    }
                    if let error {
                        Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Button {
                        Haptic.light()
                        Task { await submit() }
                    } label: {
                        Group {
                            if saving { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Save") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: saving))
                    .disabled(saving)
                }
                .padding(20)
            }
            .accountSheetChrome(target.cert == nil ? "New certificate" : "Certificate")
        }
        .onAppear {
            if let c = target.cert {
                person = c.employeeName
                if Self.kinds.contains(c.cert) { kindChoice = c.cert } else { kindChoice = Self.other; customKind = c.cert }
                expires = c.expiresOn ?? ""
                issued = c.issuedOn ?? ""
                note = c.note ?? ""
            }
        }
        .fullScreenCover(isPresented: $scanning) {
            DocumentCameraView { pages in
                scanning = false
                guard let page = pages.first else { return }
                Task { await read(page) }
            }
            .ignoresSafeArea()
        }
    }

    private var currentKind: String {
        kindChoice == Self.other ? customKind.trimmingCharacters(in: .whitespaces) : kindChoice
    }

    private func dateChip(_ iso: Binding<String>, name: String) -> some View {
        HStack(spacing: 6) {
            CavnarDateChip(iso: iso, accessibilityName: name)
            if !iso.wrappedValue.isEmpty {
                Button {
                    Haptic.light()
                    iso.wrappedValue = ""
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clear \(name.lowercased())")
            }
        }
    }

    /// Reads the scanned card on the phone (Vision text recognition) and
    /// fills what it found; the owner checks it before saving.
    private func read(_ image: UIImage) async {
        reading = true
        defer { reading = false }
        let lines = await CertificateScanReader.recognize(image)
        let found = CertificateScanReader.parse(lines: lines)
        var filled: [String] = []
        if let e = found.expires { expires = e; filled.append("expires " + CavnarDate.mdy(e)) }
        if let i = found.issued { issued = i; filled.append("issued " + CavnarDate.mdy(i)) }
        if target.cert == nil, let k = found.kind { kindChoice = k; filled.append(k) }
        if filled.isEmpty {
            scanNote = "Couldn\u{2019}t read a date off the card \u{2014} pick the expiry yourself."
        } else {
            scanNote = "Read from the scan: " + filled.joined(separator: ", ") + ". Check it before you save."
            Haptic.success()
        }
    }

    private func submit() async {
        let who = person.trimmingCharacters(in: .whitespaces)
        guard !who.isEmpty else { error = "Whose certificate is it?"; return }
        guard !currentKind.isEmpty else { error = "Name the certificate."; return }
        error = nil
        saving = true
        defer { saving = false }
        let ok = await save(StaffCertSaveBody(employeeName: who, cert: currentKind, expiresOn: expires,
                                              issuedOn: issued, note: note.trimmingCharacters(in: .whitespaces)))
        if ok { dismiss() } else { error = "It wasn\u{2019}t saved \u{2014} see the message on People." }
    }
}

/// What a scanned certificate says, read on the phone: the expiry and issue
/// dates and the kind of card. Pure parsing is separate from Vision so it
/// can be tested.
enum CertificateScanReader {
    struct Found: Equatable {
        var expires: String?
        var issued: String?
        var kind: String?
    }

    static func recognize(_ image: UIImage) async -> [String] {
        guard let cg = image.cgImage else { return [] }
        return await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let request = VNRecognizeTextRequest()
                request.recognitionLevel = .accurate
                request.usesLanguageCorrection = false
                do {
                    try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
                } catch {
                    cont.resume(returning: [])
                    return
                }
                let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
                cont.resume(returning: lines)
            }
        }
    }

    /// The kind from the card's words — alcohol and allergen before
    /// manager, manager before food handler ("ServSafe Manager" is not a
    /// food handler card).
    static func kind(in text: String) -> String? {
        let t = text.lowercased()
        if t.contains("alcohol") || t.contains("basset") || t.contains("tips certified") { return "alcohol" }
        if t.contains("allergen") { return "allergen" }
        if t.contains("manager") { return "manager" }
        if t.contains("food handler") || t.contains("food safety") || t.contains("foodhandler") { return "food handler" }
        if t.contains("first aid") || t.contains("cpr") { return "first aid" }
        return nil
    }

    private static let months = ["jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
                                 "sep": 9, "oct": 10, "nov": 11, "dec": 12]

    /// Every date on a line, as ISO days: 12/31/2027, 12/31/27, 12-31-2027,
    /// 2027-12-31, Dec 31, 2027, 31 Dec 2027.
    static func dates(in line: String) -> [String] {
        var out: [String] = []
        func add(_ y: Int, _ m: Int, _ d: Int) {
            let year = y < 100 ? 2000 + y : y
            guard (1...12).contains(m), (1...31).contains(d), (2000...2099).contains(year) else { return }
            out.append(String(format: "%04d-%02d-%02d", year, m, d))
        }
        let ns = line as NSString
        let patterns: [(String, (NSTextCheckingResult) -> Void)] = [
            (#"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"#, { r in
                add(Int(ns.substring(with: r.range(at: 1))) ?? 0, Int(ns.substring(with: r.range(at: 2))) ?? 0,
                    Int(ns.substring(with: r.range(at: 3))) ?? 0) }),
            (#"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b"#, { r in
                add(Int(ns.substring(with: r.range(at: 3))) ?? 0, Int(ns.substring(with: r.range(at: 1))) ?? 0,
                    Int(ns.substring(with: r.range(at: 2))) ?? 0) }),
            (#"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b"#, { r in
                let mon = months[String(ns.substring(with: r.range(at: 1)).lowercased().prefix(3))] ?? 0
                add(Int(ns.substring(with: r.range(at: 3))) ?? 0, mon, Int(ns.substring(with: r.range(at: 2))) ?? 0) }),
            (#"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b"#, { r in
                let mon = months[String(ns.substring(with: r.range(at: 2)).lowercased().prefix(3))] ?? 0
                add(Int(ns.substring(with: r.range(at: 3))) ?? 0, mon, Int(ns.substring(with: r.range(at: 1))) ?? 0) }),
        ]
        for (pattern, handle) in patterns {
            guard let re = try? NSRegularExpression(pattern: pattern) else { continue }
            for r in re.matches(in: line, range: NSRange(location: 0, length: ns.length)) { handle(r) }
        }
        return out
    }

    /// The expiry is the date on a line that says "exp" (or the line after
    /// one that says it alone), else the latest date on the card; the issue
    /// date the one beside "issue"/"date of", else the earliest when there
    /// are two or more.
    static func parse(lines: [String]) -> Found {
        var found = Found(kind: kind(in: lines.joined(separator: " ")))
        var all: [String] = []
        for (i, line) in lines.enumerated() {
            let lower = line.lowercased()
            var here = dates(in: line)
            if here.isEmpty, i + 1 < lines.count, lower.contains("exp") || lower.contains("issue") {
                here = dates(in: lines[i + 1])
            }
            all += dates(in: line)
            if found.expires == nil, lower.contains("exp") || lower.contains("valid until") || lower.contains("valid thru") {
                found.expires = here.last
            } else if found.issued == nil, lower.contains("issue") || lower.contains("date of") || lower.contains("certified on") {
                found.issued = here.first
            }
        }
        let sorted = Array(Set(all)).sorted()
        if found.expires == nil, sorted.count >= 1 { found.expires = sorted.last }
        if found.issued == nil, sorted.count >= 2, let first = sorted.first, first != found.expires { found.issued = first }
        return found
    }
}
