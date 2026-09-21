import SwiftUI

/// "What's connected · what's measurable" — the retention audit's #7.
///
/// admin_ops computes completeness and churn risk for Will; the owner had
/// no equivalent, so a thin dashboard read as a thin product rather than a
/// missing count. This card says, per module, whether data is flowing,
/// what the product can therefore read, and — when it cannot — the one
/// action that would change that. It shipped web-only in the first pass;
/// the owner is on the phone.
///
/// Never a score. A number here would be a grade on the owner, and the
/// point is the next step. The server drops the card entirely once every
/// module is connected.
struct HomeReadinessCard: View {
    let readiness: HomeReadiness
    var onOpenModule: (String) -> Void

    var body: some View {
        if !readiness.modules.isEmpty, readiness.complete != true {
            VStack(alignment: .leading, spacing: 10) {
                HomeSectionHeader(kicker: "What's connected", title: "What Cavnar AI can read",
                                  trailing: readiness.total.map { "\(readiness.connected ?? 0) of \($0)" })
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(readiness.modules.enumerated()), id: \.element.id) { index, m in
                        row(m, showsDivider: index < readiness.modules.count - 1)
                    }
                }
                .cavnarCard()
            }
        }
    }

    private func row(_ m: HomeReadiness.Module, showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Circle()
                    .fill(m.connected ? Color.cavnarGreen : Color.cavnarPaper3)
                    .frame(width: 8, height: 8)
                    .padding(.top, 7)
                VStack(alignment: .leading, spacing: 3) {
                    Text(m.label)
                        .font(.cavnarBody(15, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    if m.connected {
                        let reading = (m.measurable ?? []).map { $0.lowercased() }
                        Text(reading.isEmpty ? "Connected" : "Reading " + reading.joined(separator: ", "))
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk3)
                    } else if let next = m.next {
                        Button {
                            Haptic.light()
                            onOpenModule(m.module ?? m.key)
                        } label: {
                            Text(next + " →")
                                .font(.cavnarBody(13, weight: 700))
                                .foregroundStyle(Color.cavnarEmber2)
                        }
                        .buttonStyle(.plain)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 10)
            if showsDivider {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
            }
        }
    }
}
