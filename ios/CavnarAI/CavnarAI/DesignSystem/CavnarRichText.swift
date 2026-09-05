import SwiftUI

/// A small, constrained Markdown-like renderer for Ask Cavnar's answers.
///
/// The system prompt used to forbid all formatting outright ("no markdown,
/// no bullet points... plain conversational text only") specifically
/// because the client had no way to render it — a stray `**bold**` or `- `
/// leaking through showed up as literal asterisks and dashes. Rather than
/// keep suppressing formatting, this gives the model a small, well-defined
/// syntax it's told to use (see ask_cavnar.py's ASK_CAVNAR_SYSTEM_PROMPT)
/// and renders exactly that: numbered lists, bullet lists, short section
/// headings ("Option 1"), and inline emphasis. Ember is reserved for the
/// STRUCTURAL markers (bullet dots, numbers, heading labels) — inline
/// **bold** stays plain bold in ink, the same "one point of color" rule
/// the rest of this app's motion and typography already follow.
///
/// Anything that doesn't match the syntax is just a paragraph — the common
/// case for a short, direct answer, which renders exactly as plain text
/// always did.
enum CavnarMarkdown {
    struct InlineToken: Equatable {
        let text: String
        let bold: Bool
    }

    struct Block: Identifiable, Equatable {
        enum Kind: Equatable {
            case paragraph
            case bullet
            case numbered(Int)
            case heading
        }
        /// Position in the parsed sequence — stable across a partial
        /// (in-progress reveal) truncation of the SAME block, so
        /// `ForEach(blocks)` never treats a block that's still growing as a
        /// new one from one animation tick to the next.
        let index: Int
        let kind: Kind
        let tokens: [InlineToken]

        var id: Int { index }
        var tokenCount: Int { tokens.count }

        func truncated(to count: Int) -> Block {
            Block(index: index, kind: kind, tokens: Array(tokens.prefix(count)))
        }
    }

    /// Parses `raw` into blocks. Blank lines separate blocks; `## `/`### `
    /// starts a heading, `- `/`• ` a bullet, `1. ` a numbered item — each
    /// on its own line, matching what the system prompt is told to do.
    /// Everything else accumulates into the current paragraph until a
    /// blank line or a new block type starts.
    static func parse(_ raw: String) -> [Block] {
        let lines = raw.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        var blocks: [Block] = []
        var paragraphLines: [String] = []
        var nextIndex = 0

        func flushParagraph() {
            guard !paragraphLines.isEmpty else { return }
            let tokens = tokenize(paragraphLines.joined(separator: " "))
            paragraphLines = []
            guard !tokens.isEmpty else { return }
            blocks.append(Block(index: nextIndex, kind: .paragraph, tokens: tokens))
            nextIndex += 1
        }

        for rawLine in lines {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty else { flushParagraph(); continue }

            if let m = line.range(of: #"^#{1,6}\s+"#, options: .regularExpression) {
                flushParagraph()
                append(.heading, content: String(line[m.upperBound...]), to: &blocks, nextIndex: &nextIndex)
                continue
            }
            if let m = line.range(of: #"^[-•]\s+"#, options: .regularExpression) {
                flushParagraph()
                append(.bullet, content: String(line[m.upperBound...]), to: &blocks, nextIndex: &nextIndex)
                continue
            }
            if let m = line.range(of: #"^\d+\.\s+"#, options: .regularExpression) {
                flushParagraph()
                let digits = line.prefix(while: { $0.isNumber })
                let number = Int(digits) ?? (nextIndex + 1)
                append(.numbered(number), content: String(line[m.upperBound...]), to: &blocks, nextIndex: &nextIndex)
                continue
            }
            paragraphLines.append(line)
        }
        flushParagraph()

        // Should only trip on genuinely empty input, but a raw answer
        // rendered as nothing at all is worse than one rendered as a
        // single plain block.
        if blocks.isEmpty {
            let tokens = tokenize(raw.trimmingCharacters(in: .whitespacesAndNewlines))
            if !tokens.isEmpty { blocks = [Block(index: 0, kind: .paragraph, tokens: tokens)] }
        }
        return blocks
    }

    private static func append(_ kind: Block.Kind, content: String, to blocks: inout [Block], nextIndex: inout Int) {
        let tokens = tokenize(content)
        guard !tokens.isEmpty else { return }
        blocks.append(Block(index: nextIndex, kind: kind, tokens: tokens))
        nextIndex += 1
    }

    /// Splits inline text on whitespace into word tokens, resolving
    /// `**bold**` spans into a per-token flag. An unterminated trailing
    /// `**` (a truncated answer cut off mid-emphasis) just renders as
    /// plain text rather than leaving the rest of the message stuck bold.
    private static func tokenize(_ text: String) -> [InlineToken] {
        var tokens: [InlineToken] = []
        var rest = Substring(text)
        var bold = false
        while let range = rest.range(of: "**") {
            appendWords(rest[rest.startIndex..<range.lowerBound], bold: bold, to: &tokens)
            bold.toggle()
            rest = rest[range.upperBound...]
        }
        appendWords(rest, bold: false, to: &tokens)
        return tokens
    }

    private static func appendWords(_ sub: Substring, bold: Bool, to tokens: inout [InlineToken]) {
        for word in sub.split(separator: " ") where !word.isEmpty {
            tokens.append(InlineToken(text: String(word), bold: bold))
        }
    }

    // MARK: - Rendering

    /// One `Text` for a run of tokens — bold spans get real weight, and
    /// every numeric run (a price, a percent, a count) renders in Space
    /// Grotesk per the app-wide numbers rule, same as HomeMixedText.
    static func text(for tokens: [InlineToken], size: CGFloat, weight baseWeight: CGFloat, color: Color) -> Text {
        var result = Text(verbatim: "")
        for (i, token) in tokens.enumerated() {
            if i > 0 { result = result + Text(verbatim: " ") }
            let weight = token.bold ? max(baseWeight, 700) : baseWeight
            for run in HomeMixedText.runs(token.text) {
                if run.isNumber {
                    result = result + Text(verbatim: run.text)
                        .font(.cavnarNumber(size, weight: max(weight, 600)))
                        .foregroundStyle(color)
                } else {
                    result = result + Text(verbatim: run.text)
                        .font(.cavnarBody(size, weight: weight))
                        .foregroundStyle(color)
                }
            }
        }
        return result
    }

    /// Renders a full (or partially-revealed) block sequence.
    @ViewBuilder
    static func render(_ blocks: [Block], size: CGFloat, color: Color, lineSpacing: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(blocks) { block in
                blockView(block, size: size, color: color, lineSpacing: lineSpacing)
            }
        }
    }

    @ViewBuilder
    private static func blockView(_ block: Block, size: CGFloat, color: Color, lineSpacing: CGFloat) -> some View {
        switch block.kind {
        case .paragraph:
            text(for: block.tokens, size: size, weight: 400, color: color)
                .lineSpacing(lineSpacing)
                .fixedSize(horizontal: false, vertical: true)
        case .bullet:
            HStack(alignment: .firstTextBaseline, spacing: 9) {
                Circle().fill(Color.cavnarEmber2).frame(width: 5, height: 5)
                    .alignmentGuide(.firstTextBaseline) { d in d[VerticalAlignment.center] + size * 0.36 }
                text(for: block.tokens, size: size, weight: 400, color: color)
                    .lineSpacing(lineSpacing)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case .numbered(let n):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(verbatim: "\(n).")
                    .font(.cavnarNumber(size, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                text(for: block.tokens, size: size, weight: 400, color: color)
                    .lineSpacing(lineSpacing)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.leading, 2)
        case .heading:
            text(for: block.tokens, size: size - 1.5, weight: 700, color: Color.cavnarEmber2)
                .tracking(0.4)
                .textCase(.uppercase)
                .padding(.top, block.index == 0 ? 0 : 4)
        }
    }
}
