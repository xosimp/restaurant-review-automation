#if DEBUG
import Foundation
import QuartzCore
import Darwin
import MachO

/// Debug-only, opt-in (CAVNAR_DEBUG_FRAME_LOG=1): a main-thread hitch
/// profiler that ships with the app so a landing stutter can be measured
/// in the simulator instead of guessed at. A CADisplayLink stamps every
/// vsync the main thread serviced; a watcher thread notices when the stamp
/// goes stale (the main thread is busy), suspends the main thread, walks
/// its frame pointers, resumes it, and logs the stack — a poor man's Time
/// Profiler, for the exact frames that skipped. `mark()` logs named
/// moments (sign-in, Home mounted…) on the same clock so a stall can be
/// attributed to what the app was doing.
enum DebugFrameWatchdog {
    static let enabled = ProcessInfo.processInfo.environment["CAVNAR_DEBUG_FRAME_LOG"] != nil
    private static let t0 = CACurrentMediaTime()
    private static var lastTick: Double = 0
    private static var link: CADisplayLink?
    private static var mainThread: thread_t = 0
    private static var stackLo: UInt = 0
    private static var stackHi: UInt = 0
    // Preallocated so nothing allocates while the main thread is suspended
    // (it may hold the malloc lock).
    private static let frameBuffer = UnsafeMutablePointer<UInt>.allocate(capacity: 256)

    static func now() -> Double { CACurrentMediaTime() - t0 }

    static func mark(_ label: String) {
        guard enabled else { return }
        NSLog("FRAMELOG mark %.3f %@", now(), label)
    }

    @MainActor
    static func start() {
        guard enabled else { return }
        mainThread = pthread_mach_thread_np(pthread_self())
        let addr = UInt(bitPattern: pthread_get_stackaddr_np(pthread_self()))
        let size = UInt(pthread_get_stacksize_np(pthread_self()))
        stackHi = addr
        stackLo = addr - size
        let target = Ticker()
        let l = CADisplayLink(target: target, selector: #selector(Ticker.tick(_:)))
        l.add(to: .main, forMode: .common)
        link = l
        lastTick = now()
        let watcher = Thread { watch() }
        watcher.name = "cavnar.framelog"
        watcher.qualityOfService = .userInteractive
        watcher.start()
        NSLog("FRAMELOG start")
    }

    private final class Ticker: NSObject {
        private var previous: Double = 0
        @objc func tick(_ link: CADisplayLink) {
            let t = DebugFrameWatchdog.now()
            if previous > 0 {
                let gap = t - previous
                // One skipped frame at 60Hz is a 33ms gap; log anything worse.
                if gap > 0.034 {
                    NSLog("FRAMELOG hitch %.3f gap=%.0fms", previous, gap * 1000)
                }
            }
            previous = t
            DebugFrameWatchdog.lastTick = t
        }
    }

    // Up to 200 samples of 250 frames each, captured as raw addresses while
    // the stall is in progress and only symbolized (dladdr takes the dyld
    // lock the main thread also wants) once it is over — so measuring the
    // stall doesn't lengthen it.
    private static let sampleCapacity = 200
    private static let sampleDepth = 250
    private static let sampleStore = UnsafeMutablePointer<UInt>.allocate(capacity: 200 * 250)
    private static let sampleCounts = UnsafeMutablePointer<Int>.allocate(capacity: 200)
    private static let sampleTimes = UnsafeMutablePointer<Double>.allocate(capacity: 200)

    private static func watch() {
        var inStall = false
        var stallStart: Double = 0
        var samples = 0
        while true {
            usleep(4000)
            let age = now() - lastTick
            if age > 0.040 {
                if !inStall { inStall = true; stallStart = lastTick; samples = 0 }
                if samples < sampleCapacity {
                    let n = captureMainThread(into: sampleStore + samples * sampleDepth)
                    sampleCounts[samples] = n
                    sampleTimes[samples] = now()
                    samples += 1
                    usleep(11000)
                }
            } else if inStall {
                inStall = false
                NSLog("FRAMELOG stall %.3f–%.3f (%.0fms) samples=%d", stallStart, lastTick, (lastTick - stallStart) * 1000, samples)
                for i in 0..<samples { report(sampleStore + i * sampleDepth, count: sampleCounts[i], at: sampleTimes[i]) }
            }
        }
    }

    private static func report(_ frames: UnsafeMutablePointer<UInt>, count: Int, at t: Double) {
        var lines: [String] = []
        for i in 0..<count {
            var info = Dl_info()
            let pc = frames[i]
            if dladdr(UnsafeRawPointer(bitPattern: pc), &info) != 0, let sym = info.dli_sname {
                let lib = info.dli_fname.map { String(cString: $0).split(separator: "/").last.map(String.init) ?? "" } ?? ""
                lines.append("\(lib)!\(String(cString: sym))")
            } else {
                lines.append(String(format: "0x%lx", pc))
            }
        }
        NSLog("FRAMELOG sample %.3f\n    %@", t, lines.joined(separator: "\n    "))
    }

    #if arch(arm64)
    /// Suspends the main thread just long enough to copy its frame pointers
    /// out. Nothing in here allocates or takes a lock the main thread could
    /// be holding.
    private static func captureMainThread(into out: UnsafeMutablePointer<UInt>) -> Int {
        var state = arm_thread_state64_t()
        var count = mach_msg_type_number_t(MemoryLayout<arm_thread_state64_t>.size / MemoryLayout<natural_t>.size)
        guard thread_suspend(mainThread) == KERN_SUCCESS else { return 0 }
        let kr = withUnsafeMutablePointer(to: &state) { ptr in
            ptr.withMemoryRebound(to: natural_t.self, capacity: Int(count)) {
                thread_get_state(mainThread, thread_state_flavor_t(ARM_THREAD_STATE64), $0, &count)
            }
        }
        var n = 0
        if kr == KERN_SUCCESS {
            out[n] = UInt(state.__pc); n += 1
            out[n] = UInt(state.__lr); n += 1
            var fp = UInt(state.__fp)
            while n < sampleDepth, fp >= stackLo, fp < stackHi, fp & 0xF == 0 {
                let next = UnsafePointer<UInt>(bitPattern: fp)!.pointee
                let ret = UnsafePointer<UInt>(bitPattern: fp + 8)!.pointee
                if ret == 0 { break }
                out[n] = ret; n += 1
                if next <= fp { break }
                fp = next
            }
        }
        thread_resume(mainThread)
        return n
    }

    private static func sampleMainThread(at t: Double) {
        var state = arm_thread_state64_t()
        var count = mach_msg_type_number_t(MemoryLayout<arm_thread_state64_t>.size / MemoryLayout<natural_t>.size)
        guard thread_suspend(mainThread) == KERN_SUCCESS else { return }
        let kr = withUnsafeMutablePointer(to: &state) { ptr in
            ptr.withMemoryRebound(to: natural_t.self, capacity: Int(count)) {
                thread_get_state(mainThread, thread_state_flavor_t(ARM_THREAD_STATE64), $0, &count)
            }
        }
        var n = 0
        if kr == KERN_SUCCESS {
            frameBuffer[n] = UInt(state.__pc); n += 1
            frameBuffer[n] = UInt(state.__lr); n += 1
            var fp = UInt(state.__fp)
            while n < 250, fp >= stackLo, fp < stackHi, fp & 0xF == 0 {
                let next = UnsafePointer<UInt>(bitPattern: fp)!.pointee
                let ret = UnsafePointer<UInt>(bitPattern: fp + 8)!.pointee
                if ret == 0 { break }
                frameBuffer[n] = ret; n += 1
                if next <= fp { break }
                fp = next
            }
        }
        thread_resume(mainThread)
        var lines: [String] = []
        for i in 0..<n {
            var info = Dl_info()
            let pc = frameBuffer[i]
            if dladdr(UnsafeRawPointer(bitPattern: pc), &info) != 0, let sym = info.dli_sname {
                let lib = info.dli_fname.map { String(cString: $0).split(separator: "/").last.map(String.init) ?? "" } ?? ""
                lines.append("\(lib)!\(String(cString: sym))")
            } else {
                lines.append(String(format: "0x%lx", pc))
            }
        }
        NSLog("FRAMELOG sample %.3f\n    %@", t, lines.joined(separator: "\n    "))
    }
    #else
    private static func captureMainThread(into out: UnsafeMutablePointer<UInt>) -> Int { 0 }
    private static func sampleMainThread(at t: Double) {}
    #endif
}
#else
enum DebugFrameWatchdog {
    static func mark(_ label: String) {}
}
#endif
