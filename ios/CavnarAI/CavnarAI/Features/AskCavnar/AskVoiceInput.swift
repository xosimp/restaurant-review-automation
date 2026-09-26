import AVFoundation
import Observation
import Speech
import SwiftUI
import UIKit

// MARK: - Voice Ask
//
// The mic in Ask's composer. Speech is transcribed live INTO the question
// field — the owner reads it, edits it if they like, and taps send. Nothing
// is ever sent by voice alone: a misheard figure ("fifteen" for "fifty")
// asked on the owner's behalf would be an answer to a question they never
// asked.
//
// Apple's Speech framework, on the device whenever the recognizer supports
// it for the owner's language (requiresOnDeviceRecognition), so the audio
// does not leave the phone; where it cannot, Apple's own recognizer does
// the work. No audio ever reaches Cavnar AI's servers — only the text the
// owner chooses to send, exactly as if they had typed it — which is why
// PrivacyInfo.xcprivacy declares nothing new for this.
//
// Listening stops on a tap of the mic, after a short silence once words
// have come, after a longer silence with no words at all, or at a hard cap
// just under the recognizer's own one-minute limit. The header orb runs its
// reserved `listening` state while the mic is live (CavnarOrb.swift).

@MainActor
@Observable
final class AskVoiceInput {
    enum Phase: Equatable {
        case idle
        /// Asking for permission / spinning the engine up.
        case starting
        case listening
    }

    private(set) var phase: Phase = .idle
    /// A plain-English line for the composer when voice can't run: access
    /// denied, not supported, or nothing heard. Nil otherwise.
    var notice: String?
    /// True when the notice is about a permission the owner turned off, so
    /// the composer offers a way to Settings.
    private(set) var noticeNeedsSettings = false

    var isListening: Bool { phase == .listening }
    var isActive: Bool { phase != .idle }

    /// After words have arrived, this much quiet ends the take.
    static let silenceAfterSpeech: Duration = .seconds(2)
    /// With no words at all, this much quiet ends it.
    static let silenceBeforeSpeech: Duration = .seconds(8)
    /// Under SFSpeechRecognizer's own one-minute limit per request.
    static let maxTake: Duration = .seconds(55)

    @ObservationIgnored private var engine: AVAudioEngine?
    @ObservationIgnored private var request: SFSpeechAudioBufferRecognitionRequest?
    @ObservationIgnored private var task: SFSpeechRecognitionTask?
    @ObservationIgnored private var silenceTimer: Task<Void, Never>?
    @ObservationIgnored private var capTimer: Task<Void, Never>?
    /// Bumped on every start and stop, so a late callback from an earlier
    /// take can never write into the field.
    @ObservationIgnored private var session = 0
    /// The take whose final result may still land after a tap on stop.
    @ObservationIgnored private var finishingSession = -1
    @ObservationIgnored private var heardAnything = false
    /// What was in the field when listening began — speech is appended to it.
    @ObservationIgnored private var base = ""
    /// The last text this take wrote, so a final result that arrives after
    /// the owner started editing never overwrites their edit.
    @ObservationIgnored private var lastWritten = ""
    @ObservationIgnored private var read: () -> String = { "" }
    @ObservationIgnored private var write: (String) -> Void = { _ in }

    /// Tap on the mic: starts listening, or stops a take in progress.
    func toggle(read: @escaping () -> String, write: @escaping (String) -> Void) async {
        if phase != .idle {
            Haptic.light()
            stop()
            return
        }
        await start(read: read, write: write)
    }

    func start(read: @escaping () -> String, write: @escaping (String) -> Void) async {
        guard phase == .idle else { return }
        notice = nil
        noticeNeedsSettings = false
        phase = .starting
        session += 1
        let mySession = session

        // Both permissions, speech first (its prompt explains what the mic
        // is for). A denial is said plainly, with a way to Settings.
        let speech = await Self.speechAuthorization()
        guard mySession == session else { return }
        switch speech {
        case .authorized: break
        case .denied:
            return fail("Speech recognition is off for Cavnar AI. Turn it on in Settings to ask by voice.", settings: true)
        case .restricted:
            return fail("Speech recognition isn't allowed on this iPhone.", settings: false)
        default:
            return fail("Voice needs speech recognition. Try the mic again to allow it.", settings: false)
        }
        guard await Self.microphoneGranted() else {
            return fail("Microphone access is off for Cavnar AI. Turn it on in Settings to ask by voice.", settings: true)
        }
        guard mySession == session else { return }

        guard let recognizer = SFSpeechRecognizer(locale: Locale.current) ?? SFSpeechRecognizer(),
              recognizer.isAvailable else {
            return fail("Voice isn't available right now. Type your question instead.", settings: false)
        }

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.taskHint = .dictation
        request.addsPunctuation = true
        // On the phone whenever the recognizer can do it for this language.
        if recognizer.supportsOnDeviceRecognition {
            request.requiresOnDeviceRecognition = true
        }

        let engine = AVAudioEngine()
        do {
            let audio = AVAudioSession.sharedInstance()
            try audio.setCategory(.record, mode: .measurement, options: .duckOthers)
            try audio.setActive(true, options: .notifyOthersOnDeactivation)
            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            guard format.sampleRate > 0, format.channelCount > 0 else {
                Self.deactivateAudioSession()
                return fail("No microphone is available right now.", settings: false)
            }
            input.installTap(onBus: 0, bufferSize: 1024, format: format, block: Self.tapBlock(for: request))
            engine.prepare()
            try engine.start()
        } catch {
            engine.inputNode.removeTap(onBus: 0)
            Self.deactivateAudioSession()
            return fail("Couldn't start the microphone. Try again.", settings: false)
        }

        self.engine = engine
        self.request = request
        self.read = read
        self.write = write
        base = read().trimmingCharacters(in: .whitespacesAndNewlines)
        lastWritten = read()
        heardAnything = false
        finishingSession = -1
        task = recognizer.recognitionTask(with: request, resultHandler: Self.resultHandler { [weak self] text, isFinal, failed in
            Task { @MainActor in self?.receive(text: text, isFinal: isFinal, failed: failed, session: mySession) }
        })
        phase = .listening
        Haptic.medium()
        armSilence(Self.silenceBeforeSpeech, session: mySession)
        capTimer = Task { [weak self] in
            try? await Task.sleep(for: Self.maxTake)
            guard !Task.isCancelled else { return }
            self?.stopIfCurrent(mySession)
        }
    }

    /// Ends the take. The recognizer is asked to finish, so the last words
    /// still land in the field (unless the owner has already edited it).
    func stop() {
        guard phase != .idle else { return }
        finishingSession = phase == .listening ? session : -1
        session += 1
        teardown(finish: true)
        phase = .idle
    }

    // MARK: Private

    private func stopIfCurrent(_ s: Int) {
        guard s == session, phase == .listening else { return }
        if !heardAnything {
            notice = "Didn't catch anything. Tap the mic and try again."
            noticeNeedsSettings = false
        }
        Haptic.light()
        stop()
    }

    private func receive(text: String?, isFinal: Bool, failed: Bool, session s: Int) {
        let live = s == session && phase == .listening
        let finishing = s == finishingSession
        guard live || finishing else { return }
        if let text, !text.isEmpty {
            // A final result after stop applies only while the field still
            // holds what this take wrote — never over the owner's own edit.
            if live || read() == lastWritten {
                let joined = base.isEmpty ? text : base + " " + text
                lastWritten = joined
                write(joined)
            }
            if live {
                heardAnything = true
                armSilence(Self.silenceAfterSpeech, session: s)
            }
        }
        if isFinal || failed {
            if finishing { finishingSession = -1 }
            if live {
                if failed && !heardAnything {
                    notice = "Didn't catch that. Tap the mic and try again."
                    noticeNeedsSettings = false
                }
                stop()
            }
        }
    }

    private func armSilence(_ after: Duration, session s: Int) {
        silenceTimer?.cancel()
        silenceTimer = Task { [weak self] in
            try? await Task.sleep(for: after)
            guard !Task.isCancelled else { return }
            self?.stopIfCurrent(s)
        }
    }

    private func teardown(finish: Bool) {
        silenceTimer?.cancel(); silenceTimer = nil
        capTimer?.cancel(); capTimer = nil
        if let engine {
            engine.stop()
            engine.inputNode.removeTap(onBus: 0)
        }
        engine = nil
        request?.endAudio()
        if finish { task?.finish() } else { task?.cancel() }
        request = nil
        task = nil
        Self.deactivateAudioSession()
    }

    private func fail(_ message: String, settings: Bool) {
        teardown(finish: false)
        notice = message
        noticeNeedsSettings = settings
        phase = .idle
        Haptic.warning()
    }

    // Built outside the main actor: the tap runs on the audio thread and the
    // result handler on the recognizer's queue.

    nonisolated private static func tapBlock(for request: SFSpeechAudioBufferRecognitionRequest) -> AVAudioNodeTapBlock {
        { buffer, _ in request.append(buffer) }
    }

    nonisolated private static func resultHandler(
        _ sink: @escaping @Sendable (String?, Bool, Bool) -> Void
    ) -> (SFSpeechRecognitionResult?, Error?) -> Void {
        { result, error in
            sink(result?.bestTranscription.formattedString, result?.isFinal ?? false, error != nil)
        }
    }

    nonisolated private static func deactivateAudioSession() {
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    nonisolated private static func speechAuthorization() async -> SFSpeechRecognizerAuthorizationStatus {
        let now = SFSpeechRecognizer.authorizationStatus()
        guard now == .notDetermined else { return now }
        return await withCheckedContinuation { c in
            SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0) }
        }
    }

    nonisolated private static func microphoneGranted() async -> Bool {
        switch AVAudioApplication.shared.recordPermission {
        case .granted: return true
        case .denied: return false
        default:
            return await withCheckedContinuation { c in
                AVAudioApplication.requestRecordPermission { c.resume(returning: $0) }
            }
        }
    }
}

/// The composer's mic: a 38pt circle (44pt hit area) beside Send. Quiet
/// paper when idle; solid ember with a stop square while listening, so the
/// live state is unmistakable and one more tap ends it.
struct AskMicButton: View {
    var voice: AskVoiceInput
    var disabled: Bool
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: voice.isListening ? "stop.fill" : "mic")
                .font(.system(size: voice.isListening ? 13 : 16, weight: .bold))
                .foregroundStyle(voice.isListening ? .white : Color.cavnarInk2)
                .frame(width: 38, height: 38)
                .background(Circle().fill(voice.isListening
                    ? AnyShapeStyle(LinearGradient(colors: [Color.cavnarEmber2, Color.cavnarEmber], startPoint: .top, endPoint: .bottom))
                    : AnyShapeStyle(Color.cavnarPaper2)))
                .shadow(color: voice.isListening ? Color.cavnarEmber.opacity(0.4) : .clear, radius: 8, y: 3)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(disabled || voice.phase == .starting)
        .opacity(disabled ? 0.45 : 1)
        .animation(.easeOut(duration: 0.15), value: voice.isListening)
        .accessibilityLabel(voice.isListening ? "Stop listening" : "Ask by voice")
        .accessibilityHint(voice.isListening ? "" : "Say your question. It appears in the field for you to send.")
    }
}

/// The line above the composer while voice is live or can't run.
struct AskVoiceStatus: View {
    var voice: AskVoiceInput

    var body: some View {
        if voice.isListening {
            HStack(spacing: 6) {
                Image(systemName: "waveform")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber)
                Text("Listening. Tap stop when you're done, then send.")
                    .font(.cavnarBody(13, weight: 600))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .padding(.horizontal, 4)
            .transition(.opacity)
        } else if let notice = voice.notice {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(notice)
                    .font(.cavnarBody(13, weight: 600))
                    .foregroundStyle(Color.cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
                if voice.noticeNeedsSettings, let url = URL(string: UIApplication.openSettingsURLString) {
                    Button("Settings") { UIApplication.shared.open(url) }
                        .font(.cavnarBody(13, weight: 700))
                        .foregroundStyle(Color.cavnarEmber)
                        .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 4)
            .transition(.opacity)
        }
    }
}
