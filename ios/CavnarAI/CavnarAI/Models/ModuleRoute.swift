import Foundation

/// A lightweight, Hashable stand-in for "push to this module's screen" —
/// shared by every screen that navigates into a module (Home's KPI grid,
/// the Modules tab, Home's needs-attention list) via a NavigationPath
/// rather than NavigationLink, so the haptic that accompanies the tap
/// fires from a deterministic Button action closure instead of racing
/// NavigationLink's own gesture recognition.
struct ModuleRoute: Hashable {
    let key: String
    let label: String
}
