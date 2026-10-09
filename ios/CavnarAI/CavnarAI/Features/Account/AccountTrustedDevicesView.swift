import SwiftUI

/// Opened from Security's "Skip-code devices" row (2FA on only). The devices
/// that were remembered for 30 days at a 2FA sign-in — distinct from
/// Devices (live sessions): forgetting one here means that device is asked
/// for a code again next sign-in, whether or not it's signed in right now.
struct AccountTrustedDevicesView: View {
    let viewModel: AccountViewModel
    @State private var confirmingForgetAll = false
    @State private var postedLabel: String?
    @Environment(\.dismiss) private var dismiss

    private func symbol(for device: TrustedDevice) -> String {
        let label = device.label ?? ""
        if label.hasPrefix("iPhone") { return "iphone" }
        if label.hasPrefix("iPad") { return "ipad" }
        if label.hasPrefix("Mac") { return "laptopcomputer" }
        if label.hasPrefix("Windows") { return "desktopcomputer" }
        return "globe"
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Skip-code devices") {
                        GlowBadge(systemImage: "checkmark.shield", size: 64)
                    } subtitle: {
                        Text("\(viewModel.trustedDevices.count)").font(.cavnarNumber(CavnarType.body, weight: 600))
                            + Text(viewModel.trustedDevices.count == 1 ? " device skips the 2FA code" : " devices skip the 2FA code")
                    }

                    Text("A device on this list was remembered for 30 days at sign-in, so it isn't asked for a code. Forget one and it's asked next time.")
                        .font(.cavnar(.body))
                        .foregroundStyle(Color.cavnarInk2)

                    if let error = viewModel.trustedDevicesError {
                        Text(error).font(.cavnar(.body)).foregroundStyle(Color.cavnarRedText)
                    }

                    if viewModel.isLoadingTrustedDevices && viewModel.trustedDevices.isEmpty {
                        CavnarLoadingOrb().padding(.top, 20).frame(maxWidth: .infinity)
                    } else if viewModel.trustedDevices.isEmpty {
                        AccountSection(kicker: "Remembered") {
                            Text("No remembered devices — every sign-in asks for a code.")
                                .font(.cavnar(.body))
                                .foregroundStyle(Color.cavnarInk2)
                                .padding(.vertical, 9)
                        }
                    } else {
                        AccountSection(kicker: "Remembered") {
                            ForEach(Array(viewModel.trustedDevices.enumerated()), id: \.element.id) { index, device in
                                HStack(spacing: 12) {
                                    Image(systemName: symbol(for: device))
                                        .font(.system(size: 16, weight: .medium))
                                        .foregroundStyle(Color.cavnarInk2)
                                        .frame(width: 34, height: 34)
                                        .background(Color.white.opacity(0.05))
                                        .clipShape(RoundedRectangle(cornerRadius: 10))
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(device.label ?? "Device").font(.cavnarBody(CavnarType.body, weight: 700)).foregroundStyle(Color.cavnarInk)
                                        Text("Remembered " + AccountRelativeTime.describe(device.createdAt).lowercased()
                                             + (device.lastUsedAt != nil ? " · used " + AccountRelativeTime.describe(device.lastUsedAt).lowercased() : ""))
                                            .font(.cavnarNumber(CavnarType.secondary))
                                            .foregroundStyle(Color.cavnarInk3)
                                    }
                                    Spacer(minLength: 8)
                                    AccountActionChip(symbol: "xmark", tone: .cavnarRed, accessibilityLabel: "Forget \(device.label ?? "device")") {
                                        Task {
                                            if await viewModel.revokeTrustedDevice(device.id) { Haptic.success() }
                                        }
                                    }
                                }
                                .padding(.vertical, 9)
                                if index < viewModel.trustedDevices.count - 1 { AccountRowDivider() }
                            }
                        }

                        Button {
                            Haptic.light()
                            confirmingForgetAll = true
                        } label: {
                            Text("Forget all devices").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Skip-code devices")
            .task { await viewModel.loadTrustedDevices() }
            .cavnarPostedOverlay(postedLabel, tone: .removed) { dismiss() }
            .confirmationDialog("Forget every remembered device?", isPresented: $confirmingForgetAll, titleVisibility: .visible) {
                Button("Forget all", role: .destructive) {
                    Task {
                        if await viewModel.revokeAllTrustedDevices() {
                            Haptic.success()
                            postedLabel = "All devices forgotten"
                        }
                    }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Every device, including this one, will be asked for a 2FA code at its next sign-in.")
            }
        }
    }
}
