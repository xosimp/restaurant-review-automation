# Audit Pass 5 — AI Interaction & Streaming Audit

**Surfaces audited:** `Features/AskCavnar/` (conversational), `DesignSystem/TypewriterText.swift` + `DesignSystem/AIConsultantView.swift` (inline insights), and the backend contract in `ask_cavnar.py`
**Focus:** streaming UX, context/token management, memory growth, malformed-response robustness

---

## Executive summary

Token-budget management is **handled correctly, but entirely on the server**. `ask_cavnar.py`'s `_sanitize_history` (lines 381–407) caps history at 12 messages, caps each turn at 800 characters, enforces strict user/assistant alternation, and truncates the question to 500 characters. That is a genuinely careful implementation and it means the client cannot blow the context window no matter how long a session runs.

The problems are all on the **client side of that contract**: the app is unaware of every limit the server enforces. It uploads history the server will discard, lets users type questions the server will silently cut at 500 characters, and renders answers that may have been truncated at `max_tokens=320` with no indication they were cut off. Separately, what looks like streaming is not streaming — the answer is fully materialised server-side, then fake-typed client-side, which makes perceived latency *worse* than plain text would be.

| # | Severity | Finding |
|---|---|---|
| 5.1 | CRITICAL | Truncated answers (`max_tokens`) render as if complete — no `stop_reason` surfaced |
| 5.2 | WARNING | Questions over 500 chars are silently cut server-side with no client warning |
| 5.3 | WARNING | "Streaming" is simulated — full blocking generation, then a client-side typewriter |
| 5.4 | WARNING | Client uploads unbounded history the server will discard |
| 5.5 | WARNING | Markdown safety relies solely on prompt instruction, with no render fallback |
| 5.6 | OPTIMIZATION | Conversation is destroyed when the sheet is dismissed |
| — | ✅ PASS | Server-side context management is thorough and correct |

*(The O(n²) reveal loop and per-frame text measurement that also live on this surface are documented in Pass 3 §3.1–§3.3.)*

---

## 5.1 CRITICAL — A truncated answer is indistinguishable from a complete one

**Files:** `ask_cavnar.py` lines 430 (`max_tokens=320`) and 439 (`return extract_text(message).strip()`); `Features/AskCavnar/AskCavnarViewModel.swift` lines 43–47, 66–68

The backend caps generation at 320 tokens and then returns **only the text**:

```python
max_tokens=320,
# ...
return extract_text(message).strip()
```

`stop_reason` is never inspected and never forwarded. The client's response model has no field for it:

```swift
private struct AskResponse: Decodable {
    let ok: Bool
    let answer: String?
    let error: String?
}
```

So when Claude hits the ceiling mid-sentence — which the code comment at `ask_cavnar.py:424–429` explicitly acknowledges is possible ("a safety ceiling against a rare run-on answer… rather than risking a mid-sentence cutoff") — the owner sees an answer that simply **stops**, presented with the same confidence as a complete one. For a product whose entire value is trustworthy operational advice, silently serving a half-sentence recommendation about labor cuts or a supplier decision is the most consequential defect on this surface.

Notably, this project already does the right thing elsewhere: `labor.py:973–995` explicitly checks `msg.stop_reason` for exactly this condition. Ask Cavnar just never got the same treatment.

**Before** (`ask_cavnar.py:410–439`):
```python
def ask(restaurant, question, history=None):
    ...
    return extract_text(message).strip()
```

**After** — return truncation state alongside the text:
```python
def ask(restaurant, question, history=None):
    """...Returns (answer_text, was_truncated)."""
    ...
    message = create_with_retry(...)
    # max_tokens is a safety ceiling, not a target — but when it IS hit the
    # answer stops mid-sentence, and the client currently renders that as if
    # it were complete. Surface it the same way labor.py already does.
    truncated = getattr(message, "stop_reason", None) == "max_tokens"
    return extract_text(message).strip(), truncated
```
```python
# mobile_api.py — the /mobile/api/ask-cavnar route
answer, truncated = ask_cavnar.ask(restaurant, question, history=history)
return jsonify(ok=True, answer=answer, truncated=truncated)
```

**Client** (`AskCavnarViewModel.swift`):
```swift
struct ChatMessage: Identifiable {
    let id = UUID()
    let text: String
    let isUser: Bool
    /// True when the model hit max_tokens — the answer stops mid-thought and
    /// must not be presented as a finished one.
    var wasTruncated: Bool = false
}

private struct AskResponse: Decodable {
    let ok: Bool
    let answer: String?
    let error: String?
    let truncated: Bool?
}

// in submit():
messages.append(ChatMessage(text: text, isUser: false, wasTruncated: response.truncated == true))
```
```swift
// AskCavnarView.swift — ChatBubble, below the TypewriterText
if message.wasTruncated {
    Button {
        Haptic.light()
        onContinue?()          // resend as "continue where you left off"
    } label: {
        Label("Answer was cut short — tap to continue", systemImage: "text.append")
            .font(.cavnarBody(13.5, weight: 600))
            .foregroundStyle(Color.cavnarAmber)
    }
    .buttonStyle(.plain)
    .padding(.top, 6)
}
```

---

## 5.2 WARNING — Questions over 500 characters are silently truncated

**Files:** `ask_cavnar.py` line 420 (`question.strip()[:500]`); `Features/AskCavnar/AskCavnarView.swift` lines 185–189

```python
messages = _sanitize_history(history) + [{"role": "user", "content": question.strip()[:500]}]
```

The input field has `lineLimit(1...5)` and no character limit, counter, or validation:

```swift
TextField("How can I help?", text: $viewModel.question, axis: .vertical)
    .lineLimit(1...5)
```

An owner describing a nuanced situation — "we've got a 3-star trend on service since we changed the closing shift, and my Saturday bartender…" — easily passes 500 characters. The tail is discarded server-side without a word, and Claude answers a question the owner never finished asking. The owner has no way to know why the answer missed the point.

**After** — enforce the limit visibly at the point of entry:
```swift
// AskCavnarViewModel.swift
/// Mirrors ask_cavnar.py's own `question.strip()[:500]` cap. Enforced here
/// so the user sees the limit rather than having their question silently
/// cut in half server-side.
static let maxQuestionLength = 500

var question = "" {
    didSet {
        if question.count > Self.maxQuestionLength {
            question = String(question.prefix(Self.maxQuestionLength))
        }
    }
}

var remainingCharacters: Int { Self.maxQuestionLength - question.count }
```
```swift
// AskCavnarView.swift — inputBar, shown only as the user approaches the cap
if viewModel.remainingCharacters < 80 {
    Text("\(viewModel.remainingCharacters)")
        .font(.cavnarNumber(12, weight: 600))
        .foregroundStyle(viewModel.remainingCharacters <= 0 ? Color.cavnarRed : Color.cavnarInk3)
}
```

---

## 5.3 WARNING — The streaming experience is simulated, which makes perceived latency worse

**Files:** `ask_cavnar.py` line 421 (`create_with_retry`, no `stream=True`); `DesignSystem/TypewriterText.swift` lines 64–71

The actual sequence today:

1. User sends → `LoadingBubble` appears.
2. The backend blocks for the **entire** generation (a 320-token Sonnet answer: typically 2–5s).
3. The complete answer arrives at once.
4. `TypewriterText` then spends **another ~1.4 seconds** revealing text the device already has (`delayNanos` is derived from `1400.0 / Double(total)`, clamped to 16–55 ms/word).

So the app deliberately adds up to 1.4s of latency *after* the data is in hand. Real streaming inverts this: first token in ~300–600 ms, and the reveal cadence *is* the generation. The current design has the cost of a typewriter (jitter, layout thrash, delayed comprehension — see Pass 3) with none of its benefit (early first token).

The animation itself is well-crafted and on-brand, so this is not an argument to delete it — it is an argument to back it with real streaming so the same visual becomes honest.

**After** — stream server-side and drive the same UI from the token feed:
```python
# ask_cavnar.py
def ask_streaming(restaurant, question, history=None):
    """Yields text deltas as they arrive. Same prompt/caps as ask(); the
    difference is the owner sees the first words in ~400ms instead of after
    the whole answer is generated."""
    context = build_context(restaurant)
    system_prompt = ASK_CAVNAR_SYSTEM_PROMPT.format(restaurant_name=restaurant.name, context=context)
    messages = _sanitize_history(history) + [{"role": "user", "content": question.strip()[:500]}]
    with _client.messages.stream(
        model=os.getenv("ASK_CAVNAR_MODEL", "claude-sonnet-5"),
        max_tokens=320,
        system=system_prompt,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            yield ("delta", text)
        final = stream.get_final_message()
        # Structured events rather than an in-band sentinel string: a magic
        # marker inside the text stream is one prompt away from appearing in
        # a genuine answer and being swallowed as a control token.
        yield ("done", getattr(final, "stop_reason", None) == "max_tokens")
```
```python
# mobile_api.py — Server-Sent Events endpoint
@mobile_bp.route("/ask-cavnar/stream", methods=["POST"])
@mobile_login_required
def mobile_ask_cavnar_stream(current_user):
    ...
    def generate():
        for kind, payload in ask_cavnar.ask_streaming(restaurant, question, history=history):
            if kind == "delta":
                yield f"data: {json.dumps({'delta': payload})}\n\n"
            else:
                yield f"data: {json.dumps({'done': True, 'truncated': payload})}\n\n"
        yield "data: [DONE]\n\n"
    return Response(generate(), mimetype="text/event-stream")
```
```swift
// AskCavnarViewModel.swift — consume with URLSession.bytes
func submitStreaming() async {
    // ...build request as today...
    let (bytes, _) = try await URLSession.shared.bytes(for: request)
    var accumulated = ""
    messages.append(ChatMessage(text: "", isUser: false))
    for try await line in bytes.lines {
        guard line.hasPrefix("data: "), line != "data: [DONE]" else { continue }
        let payload = String(line.dropFirst(6))
        guard let data = payload.data(using: .utf8),
              let chunk = try? JSONDecoder().decode(StreamChunk.self, from: data) else { continue }
        accumulated += chunk.delta
        // Replace the trailing placeholder in place — TypewriterText is no
        // longer needed here, the network IS the reveal.
        messages[messages.count - 1] = ChatMessage(text: accumulated, isUser: false)
    }
}
```

*If streaming is deferred:* at minimum cap the artificial reveal at ~600ms total for long answers, so the added delay is not proportional to answer length.

---

## 5.4 WARNING — The client uploads history the server is guaranteed to discard

**Files:** `Features/AskCavnar/AskCavnarViewModel.swift` lines 58–62; `ask_cavnar.py` lines 377–378, 404

The client sends its **entire** message array on every question:
```swift
let history = messages.map { HistoryTurn(role: $0.isUser ? "user" : "assistant", content: $0.text) }
```

The server then keeps only the tail:
```python
_MAX_HISTORY_MESSAGES = 12
_MAX_HISTORY_TURN_LENGTH = 800
# ...
cleaned = cleaned[-_MAX_HISTORY_MESSAGES:]
```

Token cost is therefore safe — this is a **bandwidth and latency** issue, not a billing one. But on the cellular connection of a restaurant back office, a 40-turn session uploads tens of KB of history per question that the server throws away before it ever reaches Claude, adding round-trip time to every single question.

**After** — mirror the server's own cap on the client, so the wire payload matches what will actually be used:
```swift
/// Mirrors ask_cavnar.py's _MAX_HISTORY_MESSAGES. Anything older is dropped
/// server-side anyway; sending it only costs upload time on a weak connection.
private static let maxHistoryMessages = 12

let history = messages
    .suffix(Self.maxHistoryMessages)
    .map { HistoryTurn(role: $0.isUser ? "user" : "assistant", content: String($0.text.prefix(800))) }
```

---

## 5.5 WARNING — Markdown safety depends entirely on the model obeying an instruction

**Files:** `ask_cavnar.py` line 371 (system prompt); `Features/AskCavnar/AskCavnarView.swift` line 299; `DesignSystem/TypewriterText.swift` line 47

The system prompt ends with:

> "No markdown, no bullet points, no headers — plain conversational text only."

and the client renders with a `String`-initialised `Text`, which — unlike a string *literal* — performs **no markdown parsing**:

```swift
Text(words.prefix(visibleWordCount).joined(separator: " "))
```

So the two halves are consistent *as long as the model complies*. When it does not — and formatting-instruction violations are among the most common LLM deviations, especially for list-shaped questions like "what should I focus on?" — the owner sees literal `**Cut Tuesday lunch**` and `- item` markers in the chat bubble. There is no fallback, and nothing logs the deviation.

This is a defense-in-depth gap rather than a live bug, but it is cheap to close.

**After** — strip the common markers at the boundary, so a prompt deviation degrades to clean text instead of visible syntax:
```swift
// New: DesignSystem/PlainTextSanitizer.swift
import Foundation

/// The Ask Cavnar system prompt forbids markdown, and the client renders with
/// a non-literal Text (no markdown parsing) — so an answer that violates the
/// instruction shows raw ** and - markers to the owner. This is the
/// defense-in-depth half: strip the markers rather than trusting compliance.
func cavnarPlainText(_ raw: String) -> String {
    var text = raw
    // **bold** / __bold__ / *italic* → the words themselves
    text = text.replacingOccurrences(of: #"\*\*(.+?)\*\*"#, with: "$1", options: .regularExpression)
    text = text.replacingOccurrences(of: #"__(.+?)__"#, with: "$1", options: .regularExpression)
    text = text.replacingOccurrences(of: #"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)"#, with: "$1", options: .regularExpression)
    // Leading bullet/heading markers at line starts
    text = text.replacingOccurrences(of: #"(?m)^\s*[-•*]\s+"#, with: "", options: .regularExpression)
    text = text.replacingOccurrences(of: #"(?m)^#{1,6}\s+"#, with: "", options: .regularExpression)
    return text
}
```
```swift
// AskCavnarViewModel.submit() — after
let raw = response.ok ? (response.answer ?? "") : (response.error ?? "Something went wrong.")
messages.append(ChatMessage(text: cavnarPlainText(raw), isUser: false, wasTruncated: response.truncated == true))
```

An empty answer (`response.answer` nil-coalescing to `""`) is also currently rendered as a blank bubble — worth guarding in the same place:
```swift
let cleaned = cavnarPlainText(raw)
let display = cleaned.isEmpty
    ? "I didn't get an answer back that time — mind asking again?"
    : cleaned
```

---

## 5.6 OPTIMIZATION — The conversation is destroyed when the sheet is dismissed

**Files:** `RootView.swift` lines 205–207; `Features/AskCavnar/AskCavnarView.swift` line 9

```swift
.sheet(isPresented: $showingAskCavnar) {
    AskCavnarView()          // owns @State private var viewModel = AskCavnarViewModel()
}
```

The view model is `@State` **inside the sheet**, so every dismissal deallocates the entire conversation. An owner who swipes down to check a figure on the Reviews tab and comes back finds an empty chat and no way to recover what Cavnar just told them. Given the product framing — "your restaurant intelligence consultant" — a consultant with total amnesia every time you look away is a notable UX gap.

**After** — hoist ownership to `RootView`, which already does exactly this for `homeViewModel` and the navigation paths for the same reason (`RootView.swift:30–37`):
```swift
// RootView.swift
// Owned here, not inside the sheet — a sheet-owned @State view model is
// destroyed on every dismissal, wiping the conversation. Same rationale as
// homeViewModel/homePath above.
@State private var askCavnarViewModel = AskCavnarViewModel()

.sheet(isPresented: $showingAskCavnar) {
    AskCavnarView(viewModel: askCavnarViewModel)
}
```
```swift
// AskCavnarView.swift
let viewModel: AskCavnarViewModel      // was: @State private var viewModel = AskCavnarViewModel()
```
and clear it on sign-out alongside the other per-session state in `SessionStore.clearLocalSession()`.

---

## ✅ What this codebase already gets right

- **Server-side context management is genuinely thorough.** `_sanitize_history` (`ask_cavnar.py:381–407`) does not trust the client payload: it validates roles, drops empty turns, caps per-turn length at 800 chars, collapses same-role repeats to satisfy the Messages API's strict-alternation requirement, caps to the last 12 messages, **and** re-checks the "must start with user" rule *after* the cap rather than before — a subtle ordering bug most implementations get wrong. That is careful work.
- **Multi-turn context was a real bug, found and fixed properly.** The doc comment at `AskCavnarViewModel.swift:10–15` records that follow-ups like "yes" previously arrived with no context; history is now captured *before* the new question is appended, matching the backend's documented contract exactly.
- **Prompt is tuned to the actual render target.** The system prompt constrains answers to 2–3 sentences specifically because the output "renders as a narrow mobile chat bubble, not a report" — the AI output and the UI it lands in were designed against each other, which is rarer than it should be.
- **The reveal width is stable.** `TypewriterText.resolvedWidth` deliberately measures the **full final text**, so the bubble does not jitter wider and narrower word-by-word during the reveal — only height grows. The intent is right (the per-frame cost of achieving it is the Pass 3 §3.2 finding).
- **Loading state is on-brand and informative.** `LoadingBubble` uses `CavnarComposingLines` (an ember caret writing lines) rather than a generic spinner, matching the app's wider loading language.
