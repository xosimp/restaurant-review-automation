import Foundation

/// Strips markdown syntax from AI output before it is rendered.
///
/// The Ask Cavnar system prompt ends with "No markdown, no bullet points, no
/// headers — plain conversational text only," and the client renders with a
/// String-initialised `Text`, which performs no markdown parsing. Those two
/// halves agree only as long as the model complies — and formatting
/// instructions are among the most commonly violated, especially on
/// list-shaped questions ("what should I focus on?"). When it does deviate,
/// the owner sees literal `**asterisks**` and `- ` markers in the chat bubble
/// with no fallback at all (audit 5.5).
///
/// This is the defense-in-depth half: a prompt deviation degrades to clean
/// text instead of visible syntax.
func cavnarPlainText(_ raw: String) -> String {
    var text = raw
    let replacements: [(pattern: String, template: String)] = [
        (#"\*\*(.+?)\*\*"#, "$1"),                 // **bold**
        (#"__(.+?)__"#, "$1"),                     // __bold__
        (#"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)"#, "$1"),  // *italic*
        (#"(?m)^\s*[-•*]\s+"#, ""),                // leading bullets
        (#"(?m)^\s*#{1,6}\s+"#, ""),               // ATX headings
        (#"`([^`]+)`"#, "$1"),                     // `code`
    ]
    for (pattern, template) in replacements {
        text = text.replacingOccurrences(
            of: pattern, with: template, options: .regularExpression
        )
    }
    return text.trimmingCharacters(in: .whitespacesAndNewlines)
}
