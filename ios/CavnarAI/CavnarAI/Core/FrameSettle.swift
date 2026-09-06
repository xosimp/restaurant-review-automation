import QuartzCore

/// Waits until the main thread is drawing smoothly again — a run of vsyncs
/// serviced on time — or until a deadline, whichever comes first. Used to
/// sequence expensive mounts and the reveals that follow them: mount first,
/// let the frame rate recover, then start the crossfade, so the animation
/// itself never has to share frames with the build.
@MainActor
enum FrameSettle {
    static func wait(smoothFrames: Int = 3, timeout: TimeInterval = 0.7) async {
        await withCheckedContinuation { continuation in
            Waiter(needed: smoothFrames, deadline: CACurrentMediaTime() + timeout) {
                continuation.resume()
            }.start()
        }
    }

    private final class Waiter {
        private let needed: Int
        private let deadline: CFTimeInterval
        private var done: (() -> Void)?
        private var link: CADisplayLink?
        private var previous: CFTimeInterval = 0
        private var run = 0

        init(needed: Int, deadline: CFTimeInterval, done: @escaping () -> Void) {
            self.needed = needed
            self.deadline = deadline
            self.done = done
        }

        func start() {
            let link = CADisplayLink(target: self, selector: #selector(tick(_:)))
            link.add(to: .main, forMode: .common)
            self.link = link
        }

        @objc private func tick(_ link: CADisplayLink) {
            let now = link.timestamp
            // One 60Hz frame is 16.7ms; anything past ~21ms means a frame
            // was skipped, and the run starts over.
            if previous > 0 {
                run = (now - previous) < 0.021 ? run + 1 : 0
            }
            previous = now
            if run >= needed || now >= deadline {
                link.invalidate()
                self.link = nil
                let finish = done
                done = nil
                finish?()
            }
        }
    }
}
