import SwiftUI
import UIKit
import VisionKit

/// The system document scanner (VisionKit): edge detection, perspective
/// correction and a clean white page, without leaving Cavnar for the Camera
/// app and without the invoice landing in the personal photo library
/// (Friction audit #28, U3-8).
struct DocumentCameraView: UIViewControllerRepresentable {
    /// The scanned pages, first page first. Empty when the owner cancelled.
    var onFinish: ([UIImage]) -> Void

    /// False on the Simulator and on devices without a camera — the photo
    /// picker is then the only way in, and the scan button isn't shown.
    static var isAvailable: Bool { VNDocumentCameraViewController.isSupported }

    func makeUIViewController(context: Context) -> VNDocumentCameraViewController {
        let controller = VNDocumentCameraViewController()
        controller.delegate = context.coordinator
        return controller
    }

    func updateUIViewController(_ controller: VNDocumentCameraViewController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(onFinish: onFinish) }

    final class Coordinator: NSObject, VNDocumentCameraViewControllerDelegate {
        let onFinish: ([UIImage]) -> Void

        init(onFinish: @escaping ([UIImage]) -> Void) { self.onFinish = onFinish }

        func documentCameraViewController(_ controller: VNDocumentCameraViewController,
                                          didFinishWith scan: VNDocumentCameraScan) {
            let pages = (0..<scan.pageCount).map { scan.imageOfPage(at: $0) }
            onFinish(pages)
        }

        func documentCameraViewControllerDidCancel(_ controller: VNDocumentCameraViewController) {
            onFinish([])
        }

        func documentCameraViewController(_ controller: VNDocumentCameraViewController,
                                          didFailWithError error: Error) {
            onFinish([])
        }
    }
}
