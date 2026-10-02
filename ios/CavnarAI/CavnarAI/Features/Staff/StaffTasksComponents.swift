import SwiftUI
import UIKit

// The task sheet's own pieces: the check disc, the progress bar, the camera,
// the proof thumbnail and the note a line says under itself.

// MARK: - Check disc

/// The design system's check disc (DESIGN_SYSTEM §4 "Check, warning and
/// miss marks", the web `.cv-ok`) at the size a tick needs: a soft green
/// disc lit from the top-left with a dark tick and a faint halo when done;
/// a hollow ring when not (red once the line is overdue). A tick still
/// travelling, or parked offline, is the done disc held back a little.
struct StaffCheckDisc: View {
    var done: Bool
    var overdue: Bool = false
    var pending: StaffLineOverlay.State? = nil
    var size: CGFloat = 28
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            if done {
                Circle()
                    .stroke(Color.cavnarGreen.opacity(0.16), lineWidth: 3)
                    .frame(width: size + 6, height: size + 6)
                Circle().fill(Color.cavnarGreen)
                Circle().fill(RadialGradient(colors: [Color.white.opacity(0.28), Color.white.opacity(0)],
                                             center: UnitPoint(x: 0.35, y: 0.3),
                                             startRadius: 0, endRadius: size * 0.62))
                StaffTickMark()
                    .stroke(Color.black.opacity(0.72), style: StrokeStyle(lineWidth: max(2, size * 0.09),
                                                                          lineCap: .round, lineJoin: .round))
                    .frame(width: size * 0.46, height: size * 0.36)
                    .offset(y: size * 0.01)
            } else {
                Circle()
                    .strokeBorder(overdue ? Color.cavnarRed : Color.cavnarInk3.opacity(0.65), lineWidth: 1.75)
            }
        }
        .frame(width: size, height: size)
        .shadow(color: done ? Color.cavnarGreen.opacity(0.32) : .clear, radius: 5, y: 2)
        .opacity(pending == nil ? 1 : (pending == .sending ? 0.75 : 0.55))
        .scaleEffect(done || reduceMotion ? 1 : 0.94)
        .animation(reduceMotion ? nil : .cavnarEase(0.2), value: done)
        .accessibilityHidden(true)
    }
}

/// The disc's tick, drawn as a path so it scales with Dynamic Type.
struct StaffTickMark: Shape {
    func path(in rect: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: rect.minX, y: rect.midY + rect.height * 0.05))
        p.addLine(to: CGPoint(x: rect.minX + rect.width * 0.36, y: rect.maxY))
        p.addLine(to: CGPoint(x: rect.maxX, y: rect.minY))
        return p
    }
}

// MARK: - Progress

/// A sheet's progress as a thin ember capsule (the web `.ts-bar`), in place
/// of the system ProgressView. Eases to its new length; still under Reduce
/// Motion.
struct StaffEmberProgressBar: View {
    var done: Int
    var total: Int
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var fraction: CGFloat { total > 0 ? min(1, max(0, CGFloat(done) / CGFloat(total))) : 0 }

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.cavnarPaper3.opacity(0.7))
                Capsule()
                    .fill(LinearGradient(colors: [Color.cavnarEmber, Color.cavnarEmber2],
                                         startPoint: .leading, endPoint: .trailing))
                    .frame(width: fraction > 0 ? max(6, geo.size.width * fraction) : 0)
            }
        }
        .frame(height: 6)
        .animation(reduceMotion ? nil : .cavnarEase(0.35), value: fraction)
        .accessibilityHidden(true)
    }
}

// MARK: - Camera

/// The camera, for a photo that proves a line is done (UX-24). The
/// library is the second choice; an old photo shouldn't be the easy one.
struct StaffCameraPicker: UIViewControllerRepresentable {
    var onPicked: (UIImage?) -> Void

    static var isAvailable: Bool { UIImagePickerController.isSourceTypeAvailable(.camera) }

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.cameraCaptureMode = .photo
        picker.allowsEditing = false
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ uiViewController: UIImagePickerController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(onPicked: onPicked) }

    @MainActor
    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let onPicked: (UIImage?) -> Void
        init(onPicked: @escaping (UIImage?) -> Void) { self.onPicked = onPicked }

        func imagePickerController(_ picker: UIImagePickerController,
                                   didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            onPicked(info[.originalImage] as? UIImage)
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            onPicked(nil)
        }
    }
}

// MARK: - Proof thumbnail

/// The proof photo on a ticked line, read once and kept in memory.
struct StaffProofThumbnail: View {
    let token: String
    let store: StaffTasksStore

    var body: some View {
        Group {
            if let image = store.thumbnails[token] {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFill()
            } else {
                RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .fill(Color.cavnarPaper3.opacity(0.6))
                    .overlay(Image(systemName: "photo").font(.system(size: 15)).foregroundStyle(Color.cavnarInk3))
            }
        }
        .frame(width: 56, height: 56)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        .accessibilityElement()
        .accessibilityLabel("Proof photo")
        .task(id: token) { await store.loadThumbnail(token) }
    }
}

// MARK: - The line's own note

/// What a line says under itself (UX-13): the critical alert in red with
/// what to do, a refusal in red, a quiet word otherwise.
struct StaffLineNoteView: View {
    let note: StaffTasksStore.LineNote

    var body: some View {
        switch note {
        case let .alert(alert, offline):
            VStack(alignment: .leading, spacing: 4) {
                Label {
                    Text(alert.title).font(.cavnarBody(CavnarType.body, weight: 700))
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").font(.system(size: 13, weight: .bold))
                }
                .foregroundStyle(Color.cavnarRed)
                if let message = alert.message, !message.isEmpty {
                    Text(Self.body(of: message, title: alert.title))
                        .font(.cavnarBody(CavnarType.secondary))
                        .foregroundStyle(Color.cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !offline, alert.managerAlerted != true {
                    Text("The app couldn't reach a manager for you, so tell them in person.")
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                } else if !offline {
                    Text("Your manager has been sent this too.")
                        .font(.cavnarBody(CavnarType.caption))
                        .foregroundStyle(Color.cavnarInk3)
                }
                // A one-tap "Message manager" belongs here once the staff
                // app has a message thread to open (employee audit I3).
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.cavnarRedBg, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control).strokeBorder(Color.cavnarRed.opacity(0.5), lineWidth: 1))
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(.isStaticText)
        case let .error(text):
            Text(text)
                .font(.cavnarBody(CavnarType.secondary))
                .foregroundStyle(Color.cavnarRed)
                .fixedSize(horizontal: false, vertical: true)
        case let .info(text):
            Text(text)
                .font(.cavnarBody(CavnarType.secondary))
                .foregroundStyle(Color.cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
        case let .queued(text):
            Label {
                Text(text).fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "icloud.slash").font(.system(size: 12, weight: .semibold))
            }
            .font(.cavnarBody(CavnarType.secondary))
            .foregroundStyle(Color.cavnarAmber)
        }
    }

    /// The server's message without its leading "Tell your manager now: ",
    /// which the title already says.
    static func body(of message: String, title: String) -> String {
        let prefix = title + ": "
        return message.hasPrefix(prefix) ? String(message.dropFirst(prefix.count)) : message
    }
}
