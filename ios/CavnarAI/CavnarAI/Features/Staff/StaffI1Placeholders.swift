import SwiftUI

// TEMPORARY — delete this whole file when I1's sign-in/session wave merges.
//
// Me (StaffMeView) presents five sheets that I1 owns: Forgot PIN, Change
// PIN, Delete my account, Email and the location switcher. They did not
// exist on this branch when Me was built, so these stand-ins keep the app
// compiling with the same names and the same `init(store:)` I1 was asked
// for. Each says plainly that the step isn't in this build — none of them
// calls a route. At merge, I1's real views replace these one for one; if
// both files are present the compiler reports the duplicate type names.

private struct StaffI1Pending: View {
    let title: String
    let line: String

    var body: some View {
        NavigationStack {
            ScrollView {
                CavnarEmptyHearth(title: title, message: line)
                    .padding(20)
            }
            .accountSheetChrome(title)
        }
    }
}

struct StaffForgotPinView: View {
    let store: StaffSessionStore
    init(store: StaffSessionStore) { self.store = store }
    var body: some View {
        StaffI1Pending(title: "Forgot PIN", line: "Ask your manager to reset your PIN for now.")
    }
}

struct StaffChangePinView: View {
    let store: StaffSessionStore
    init(store: StaffSessionStore) { self.store = store }
    var body: some View {
        StaffI1Pending(title: "Change PIN", line: "Changing your PIN here isn\u{2019}t in this build yet.")
    }
}

struct StaffDeleteAccountView: View {
    let store: StaffSessionStore
    init(store: StaffSessionStore) { self.store = store }
    var body: some View {
        StaffI1Pending(title: "Delete my account", line: "Ask your manager to remove your login for now.")
    }
}

struct StaffEmailEditView: View {
    let store: StaffSessionStore
    init(store: StaffSessionStore) { self.store = store }
    var body: some View {
        StaffI1Pending(title: "Email", line: "Ask your manager to update your email for now.")
    }
}

struct StaffLocationSwitcherView: View {
    let store: StaffSessionStore
    init(store: StaffSessionStore) { self.store = store }
    var body: some View {
        StaffI1Pending(title: "Locations", line: "Sign in at the other location with its code and your PIN there.")
    }
}
