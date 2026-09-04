import SwiftUI

/// Set, change or remove the app passcode — opened from Security & devices.
/// One passcode pad, a headline that slides between steps, and the same
/// posted-check overlay every other successful Account write gets.
struct AppPasscodeSheet: View {
    enum Mode: String, Identifiable {
        case create, change, remove
        var id: String { rawValue }
    }

    let mode: Mode
    @Environment(SessionStore.self) private var sessionStore
    @Environment(\.dismiss) private var dismiss

    private enum Step { case current, new, confirm }

    @State private var step: Step
    @State private var code = ""
    @State private var firstEntry = ""
    @State private var isError = false
    @State private var errorText: String?
    @State private var postedLabel: String?
    @State private var postedTone: CavnarPostedTone = .success
    // Drives the headline/caption slide between steps.
    @State private var stepIndex = 0

    init(mode: Mode) {
        self.mode = mode
        _step = State(initialValue: mode == .create ? .new : .current)
    }

    private var title: String {
        switch mode {
        case .create: return "Set a passcode"
        case .change: return "Change passcode"
        case .remove: return "Remove passcode"
        }
    }

    private var heroSubtitle: String {
        switch mode {
        case .create: return "Reopen the app without Face ID"
        case .change: return "Pick six new digits"
        case .remove: return "The app will stop asking for it"
        }
    }

    private var headline: String {
        switch step {
        case .current: return "Enter your current one"
        case .new: return "Choose six digits"
        case .confirm: return "Enter it once more"
        }
    }

    private var caption: String {
        switch step {
        case .current: return "Confirm it's you first."
        case .new: return "You'll use this whenever Face ID is off."
        case .confirm: return "Just so there are no typos."
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 38) {
                    AccountHero(title: title) {
                        GlowBadge(systemImage: "lock.fill", size: 64)
                    } subtitle: {
                        Text(heroSubtitle)
                    }

                    VStack(spacing: 8) {
                        Text(headline)
                            .font(.cavnarHeadline(22))
                            .foregroundStyle(Color.cavnarInk)
                            .multilineTextAlignment(.center)
                        Text(errorText ?? caption)
                            .font(.cavnarBody(15))
                            .foregroundStyle(errorText == nil ? Color.cavnarInk3 : Color.cavnarRed)
                            .multilineTextAlignment(.center)
                            .lineSpacing(3)
                    }
                    .padding(.horizontal, 12)
                    .padding(.top, 22)
                    .id(stepIndex)
                    .transition(.asymmetric(
                        insertion: .move(edge: .trailing).combined(with: .opacity),
                        removal: .move(edge: .leading).combined(with: .opacity)
                    ))

                    CavnarPasscodePad(code: $code, isError: isError) { entered in
                        handle(entered)
                    }
                    .padding(.top, 10)
                }
                .frame(maxWidth: .infinity)
                .padding(20)
                .animation(.easeOut(duration: 0.32), value: stepIndex)
                .animation(.easeOut(duration: 0.2), value: errorText)
            }
            .accountSheetChrome("App passcode")
            .cavnarPostedOverlay(postedLabel, tone: postedTone) { dismiss() }
        }
    }

    private func handle(_ entered: String) {
        switch step {
        case .current:
            switch sessionStore.unlockWithPasscode(entered, consumeUnlock: false) {
            case .unlocked:
                if mode == .remove {
                    sessionStore.removeAppPasscode()
                    Haptic.success()
                    postedTone = .removed
                    postedLabel = "Passcode removed"
                } else {
                    advance(to: .new)
                }
            case .wrong(let remaining):
                fail(remaining > 0 && remaining <= 3
                     ? "Wrong passcode · \(remaining) attempt\(remaining == 1 ? "" : "s") left"
                     : "Wrong passcode — try again")
            case .lockedOut(let seconds):
                fail("Too many attempts. Try again in \(lockoutText(seconds)).")
            }
        case .new:
            firstEntry = entered
            advance(to: .confirm)
        case .confirm:
            if entered == firstEntry {
                sessionStore.setAppPasscode(entered)
                Haptic.success()
                postedTone = .success
                postedLabel = mode == .change ? "Passcode changed" : "Passcode set"
            } else {
                firstEntry = ""
                fail("Those didn't match — start over")
                Task { @MainActor in
                    try? await Task.sleep(for: .seconds(0.7))
                    advance(to: .new, keepError: true)
                }
            }
        }
    }

    private func advance(to next: Step, keepError: Bool = false) {
        code = ""
        if !keepError { errorText = nil }
        isError = false
        step = next
        stepIndex += 1
    }

    private func fail(_ message: String) {
        Haptic.error()
        errorText = message
        isError = true
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(0.5))
            code = ""
            isError = false
        }
    }

    private func lockoutText(_ seconds: Int) -> String {
        seconds >= 60 ? "\(Int((Double(seconds) / 60).rounded(.up))) min" : "\(seconds)s"
    }
}
