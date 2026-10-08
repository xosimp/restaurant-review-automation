import SwiftUI
import Contacts
import ContactsUI

/// The system contact picker (CNContactPickerViewController) for "Request a
/// review" (parity audit 10/7/26 #91). It runs out of process, so the app
/// never asks for — and never gets — access to the whole address book: only
/// the one contact the owner taps comes back. Filling a phone number never
/// says the guest agreed to be texted; that stays the sheet's own toggle.
struct GuestContactPick: Equatable {
    var name: String
    var email: String
    var phone: String

    /// The fields the request form takes from one contact: the full name,
    /// the first email and the first phone number it carries.
    static func from(_ contact: CNContact) -> GuestContactPick {
        let name = [contact.givenName, contact.familyName]
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        let email = contact.isKeyAvailable(CNContactEmailAddressesKey)
            ? (contact.emailAddresses.first.map { String($0.value) } ?? "") : ""
        let phone = contact.isKeyAvailable(CNContactPhoneNumbersKey)
            ? (contact.phoneNumbers.first?.value.stringValue ?? "") : ""
        return GuestContactPick(name: name, email: email, phone: phone)
    }
}

/// Presents the picker over whatever is on screen while `isPresented` is
/// true. A UIViewControllerRepresentable host, because the picker must be
/// presented modally by UIKit — embedding it as a SwiftUI view leaves it
/// blank.
struct GuestContactPicker: UIViewControllerRepresentable {
    @Binding var isPresented: Bool
    var onPick: (GuestContactPick) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIViewController(context: Context) -> UIViewController {
        let host = UIViewController()
        host.view.backgroundColor = .clear
        return host
    }

    func updateUIViewController(_ host: UIViewController, context: Context) {
        context.coordinator.parent = self
        guard isPresented, host.presentedViewController == nil else { return }
        let picker = CNContactPickerViewController()
        picker.delegate = context.coordinator
        picker.displayedPropertyKeys = [CNContactGivenNameKey, CNContactFamilyNameKey,
                                        CNContactEmailAddressesKey, CNContactPhoneNumbersKey]
        DispatchQueue.main.async {
            guard host.presentedViewController == nil, host.view.window != nil else { return }
            host.present(picker, animated: true)
        }
    }

    final class Coordinator: NSObject, CNContactPickerDelegate {
        var parent: GuestContactPicker
        init(_ parent: GuestContactPicker) { self.parent = parent }

        func contactPicker(_ picker: CNContactPickerViewController, didSelect contact: CNContact) {
            parent.onPick(GuestContactPick.from(contact))
            parent.isPresented = false
        }

        func contactPickerDidCancel(_ picker: CNContactPickerViewController) {
            parent.isPresented = false
        }
    }
}
