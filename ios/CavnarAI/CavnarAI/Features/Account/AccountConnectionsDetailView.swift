import SwiftUI

/// Pushed from Account's "Connections" row. Read-only status — connecting
/// a POS/social/GBP account is an OAuth redirect flow that stays a desktop
/// action for now (see mobile_api.py's Account/Settings section docstring).
struct AccountConnectionsDetailView: View {
    let connections: AccountConnections

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                connectionRow("Google Business", systemImage: "checkmark.seal", connections.googleBusiness)
                divider()
                connectionRow("Instagram & Facebook", systemImage: "camera", connections.instagram)
                divider()
                connectionRow("Toast POS", systemImage: "creditcard", connections.toast)
                divider()
                connectionRow("Square POS", systemImage: "creditcard", connections.square)
                divider()
                connectionRow("Clover POS", systemImage: "creditcard", connections.clover)
            }
            .cavnarCard()
            .padding(20)
        }
        .cavnarModuleBackground()
        .navigationTitle("Connections")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func connectionRow(_ label: String, systemImage: String, _ status: ConnectionStatus) -> some View {
        HStack(spacing: 13) {
            Image(systemName: systemImage)
                .font(.system(size: 15))
                .foregroundStyle(Color.cavnarInk3)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(label).font(.cavnarBody(14, weight: 600)).foregroundStyle(Color.cavnarInk)
                if status.connected, let lastSynced = status.lastSynced {
                    Text("Last synced \(lastSynced)").font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer()
            HStack(spacing: 5) {
                Circle()
                    .fill(status.connected ? Color.cavnarGreen : Color.cavnarInk3.opacity(0.4))
                    .frame(width: 6, height: 6)
                Text(status.connected ? "Connected" : "Not connected")
                    .font(.cavnarBody(12, weight: 600))
                    .foregroundStyle(status.connected ? Color.cavnarGreen : Color.cavnarInk3)
            }
        }
        .padding(.vertical, 4)
    }

    private func divider() -> some View {
        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
    }
}
