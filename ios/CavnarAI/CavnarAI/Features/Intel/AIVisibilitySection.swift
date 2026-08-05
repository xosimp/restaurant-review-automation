import SwiftUI

struct AIVisibilitySection: View {
    let viewModel: AIVisibilityViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Button {
                Task { await viewModel.check() }
            } label: {
                if viewModel.isChecking {
                    HStack(spacing: 8) {
                        ProgressView().tint(.white)
                        Text("Checking…")
                    }
                    .frame(maxWidth: .infinity)
                } else {
                    Text(viewModel.result == nil ? "Check my AI visibility" : "Check again")
                        .frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(viewModel.isChecking)

            if let result = viewModel.result {
                if !result.ok {
                    Text(result.error ?? "Couldn't check AI visibility.")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarRed)
                } else {
                    scoreCard(result)
                    if let queries = result.queries {
                        queriesCard(queries)
                    }
                    if let checklist = result.checklist {
                        checklistCard(checklist)
                    }
                }
            }
        }
    }

    private func scoreCard(_ result: AIVisibilityResult) -> some View {
        HStack(spacing: 0) {
            scoreTile(value: "\(result.aiScore ?? 0)%", label: "AI mention rate")
            Divider()
            scoreTile(value: "\(result.gbpScore ?? 0)/100", label: "Profile score")
        }
        .cavnarCard()
    }

    private func scoreTile(value: String, label: String) -> some View {
        VStack(spacing: 4) {
            Text(value).font(.cavnarNumber(22, weight: 500)).foregroundStyle(Color.cavnarInk).cavnarNumberGlow()
            Text(label).font(.cavnarBody(10)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }

    private func queriesCard(_ queries: [AIVisibilityQuery]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("AI search queries").font(.cavnarBody(11, weight: 700)).foregroundStyle(Color.cavnarInk3)
            ForEach(queries) { q in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(q.query).font(.cavnarBody(12, weight: 600)).foregroundStyle(Color.cavnarInk)
                        Spacer()
                        Image(systemName: q.appeared ? "checkmark.circle.fill" : "xmark.circle")
                            .foregroundStyle(q.appeared ? Color.cavnarGreen : Color.cavnarInk3)
                    }
                    Text(q.answer)
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarInk3)
                        .lineLimit(3)
                }
                .padding(.vertical, 4)
            }
        }
        .cavnarCard()
    }

    private func checklistCard(_ checklist: [AIVisibilityChecklistItem]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Your AI visibility roadmap").font(.cavnarBody(11, weight: 700)).foregroundStyle(Color.cavnarInk3)
            ForEach(checklist) { item in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: item.done ? "checkmark.circle.fill" : "circle")
                        .font(.system(size: 14))
                        .foregroundStyle(item.done ? Color.cavnarGreen : Color.cavnarInk3)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.label).font(.cavnarBody(12, weight: 600)).foregroundStyle(Color.cavnarInk)
                        if !item.done {
                            Text(item.action).font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
                        }
                    }
                }
                .padding(.vertical, 3)
            }
        }
        .cavnarCard()
    }
}
