import SwiftUI

/// The app's screen title (Core/AppChrome.swift) carries the location
/// switcher, which the Share extension has no part of. ViewModifiers.swift
/// — compiled here for the Cavnar button styles and the loading bar —
/// names it in `cavnarTitleToolbar`, which this sheet never uses; this is
/// the title alone, in the same face.
struct CavnarScreenTitle: View {
    let title: String

    var body: some View {
        Text(title)
            .font(.cavnarHeadline(17))
            .foregroundStyle(Color.cavnarInk)
            .lineLimit(1)
    }
}
