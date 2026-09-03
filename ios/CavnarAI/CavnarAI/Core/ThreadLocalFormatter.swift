import Foundation

/// One formatter instance per thread.
///
/// Foundation's formatters are expensive to construct — which is why they were
/// `static let` singletons — but they are not Sendable and carry internal
/// mutable caching state. Sharing one across isolation domains (the APIClient
/// actor's decode path and @MainActor view bodies both reach
/// `Review.formattedDate`) is a genuine data race, flagged by the compiler
/// under SWIFT_STRICT_CONCURRENCY and an error in the Swift 6 language mode.
/// It surfaces as rare, unreproducible garbage dates or a crash inside
/// CoreFoundation (audit 2.2).
///
/// Thread-local instances keep the build-once-not-per-call performance intent
/// without the sharing.
final class ThreadLocalFormatter<T: AnyObject>: @unchecked Sendable {
    private let key: String
    private let make: @Sendable () -> T

    init(_ make: @escaping @Sendable () -> T) {
        self.key = "ai.cavnar.formatter.\(UUID().uuidString)"
        self.make = make
    }

    var value: T {
        if let existing = Thread.current.threadDictionary[key] as? T { return existing }
        let fresh = make()
        Thread.current.threadDictionary[key] = fresh
        return fresh
    }
}
