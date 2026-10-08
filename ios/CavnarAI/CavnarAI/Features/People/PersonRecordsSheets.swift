import SwiftUI

// Rename, "the same person as…" and erase, opened from the person sheet's
// overflow menu (parity audit 10/7/26 #87) — the web's person sheet
// (dashboard.html memPersonHtml: Rename, Merge, Erase their record).

/// A new spelling for them: every rating, note and shift follows, and the
/// old spelling stays as another name for them.
struct PersonRenameSheet: View {
    let name: String
    let save: (String) async -> Bool

    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var saving = false

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 14) {
                TextField("Their name", text: $text)
                    .cavnarTextFieldStyle()
                    .textInputAutocapitalization(.words)
                    .autocorrectionDisabled()
                Text("Every rating, note and shift follows; the old spelling stays as another name for them.")
                    .font(.cavnarBody(CavnarType.secondary))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                Button {
                    Haptic.light()
                    Task {
                        saving = true
                        let ok = await save(text)
                        saving = false
                        if ok { dismiss() }
                    }
                } label: {
                    Group { if saving { CavnarShimmerText(text: "Renaming\u{2026}") } else { Text("Rename") } }
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: saving || unchanged))
                .disabled(saving || unchanged)
                Spacer()
            }
            .padding(20)
            .accountSheetChrome("Rename")
        }
        .onAppear { text = name }
    }

    private var unchanged: Bool {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty || t == name
    }
}

/// Who they are the same person as — everyone else on the list, searchable,
/// with who is off the roster said. Picking one asks before merging.
struct PersonMergePicker: View {
    let people: [PeopleListRow]
    let name: String
    /// The list came back; `error` says why it didn't (re-audit 10/8/26):
    /// a failure shows Try again, an empty list says so — the skeleton is
    /// only for the read in flight.
    var loaded = true
    var error: String?
    var retry: () async -> Void = {}
    let pick: (PeopleListRow) -> Void

    @State private var query = ""

    var body: some View {
        NavigationStack {
            List {
                Section {
                    if let error {
                        StaffLoadFailed(what: "the list", message: error, retry: retry)
                            .listRowBackground(Color.clear)
                    } else if !loaded {
                        CavnarSkeletonLines(widths: [0.6, 0.8, 0.5])
                            .listRowBackground(Color.clear)
                    } else if people.isEmpty {
                        Text("Nobody else is on the list yet.")
                            .font(.cavnarBody(CavnarType.body))
                            .foregroundStyle(Color.cavnarInk3)
                            .listRowBackground(Color.clear)
                    }
                    ForEach(filtered) { p in
                        Button {
                            Haptic.selection()
                            pick(p)
                        } label: {
                            HStack {
                                Text(p.name)
                                    .font(.cavnarBody(CavnarType.body, weight: 600))
                                    .foregroundStyle(Color.cavnarInk)
                                Spacer()
                                if !p.active {
                                    Text("off the roster")
                                        .font(.cavnarBody(CavnarType.caption))
                                        .foregroundStyle(Color.cavnarInk3)
                                }
                            }
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .listRowBackground(Color.cavnarPaper2.opacity(0.6))
                    }
                } footer: {
                    Text("Pick who \(name) is the same person as. Their records move there; you can undo it for 30 days.")
                        .font(.cavnarBody(CavnarType.caption))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            .scrollContentBackground(.hidden)
            .searchable(text: $query, prompt: "Find someone")
            .accountSheetChrome("The same person as\u{2026}")
        }
    }

    private var filtered: [PeopleListRow] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        return q.isEmpty ? people : people.filter { $0.name.lowercased().contains(q) }
    }
}

/// Erasing cannot be undone, so the name typed back is the confirmation —
/// the web's tier-3 confirm (DESIGN_SYSTEM §10). Owner-only: the sheet's
/// menu offers it only to the account holder, and the server refuses
/// anyone else.
struct PersonEraseSheet: View {
    let name: String
    var error: String?
    let erase: (String) async -> Bool

    @State private var typed = ""
    @State private var erasing = false

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 14) {
                Text("Erase \(name)\u{2019}s record for good?")
                    .font(.cavnarHeadline(22))
                    .foregroundStyle(Color.cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                Text("Every rating, note, setting, shift and learned pattern about them is deleted and cannot be brought back. Schedules already published stay as they were sent.")
                    .font(.cavnarBody(CavnarType.secondary))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                TextField("Type \(name) to confirm", text: $typed)
                    .cavnarTextFieldStyle()
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                if let error {
                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Button(role: .destructive) {
                    Haptic.warning()
                    Task {
                        erasing = true
                        _ = await erase(typed)
                        erasing = false
                    }
                } label: {
                    Group {
                        if erasing { CavnarShimmerText(text: "Erasing\u{2026}", color: .cavnarRed) }
                        else { Text("Erase for good").foregroundStyle(Color.cavnarRed) }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: !matches || erasing))
                .disabled(!matches || erasing)
                Spacer()
            }
            .padding(20)
            .accountSheetChrome("Erase their record")
        }
    }

    private var matches: Bool { PersonSheetViewModel.typedNameMatches(typed, name) }
}
