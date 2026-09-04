import SwiftUI

/// Builds one `Text` from a sentence where every numeric run — "12/14",
/// "$1,840", "86%", "+$1,240", "3" — is set in Space Grotesk and everything
/// else in the body face. The typography rule for numbers inside mixed
/// copy, applied to Home's hero subline, pulse chips, action-deck titles
/// and the weekly receipt so a figure never renders in Apfel.
enum HomeMixedText {
    struct Run: Equatable {
        let text: String
        let isNumber: Bool
    }

    static func make(
        _ string: String,
        size: CGFloat,
        weight: CGFloat = 400,
        color: Color = .cavnarInk,
        numberWeight: CGFloat? = nil,
        numberColor: Color? = nil
    ) -> Text {
        var result = Text(verbatim: "")
        for run in runs(string) {
            if run.isNumber {
                result = result + Text(verbatim: run.text)
                    .font(.cavnarNumber(size, weight: numberWeight ?? max(weight, 600)))
                    .foregroundStyle(numberColor ?? color)
            } else {
                result = result + Text(verbatim: run.text)
                    .font(.cavnarBody(size, weight: weight))
                    .foregroundStyle(color)
            }
        }
        return result
    }

    /// Splits `string` into alternating prose / number runs. A number run
    /// starts at a digit (optionally led by "$", "+$" or "+") and keeps
    /// going through digits, a trailing "%", and any of `, . / :` that sit
    /// between two digits — so "1,840", "22.3%", "12/14" and "9:41" each
    /// stay one run, while "Sep 3." keeps its full stop in the prose.
    static func runs(_ string: String) -> [Run] {
        var out: [Run] = []
        var current = ""
        var currentIsNumber = false
        func flush() {
            guard !current.isEmpty else { return }
            out.append(Run(text: current, isNumber: currentIsNumber))
            current = ""
        }

        let chars = Array(string)
        var i = 0
        while i < chars.count {
            let c = chars[i]
            let next: Character? = i + 1 < chars.count ? chars[i + 1] : nil
            let afterNext: Character? = i + 2 < chars.count ? chars[i + 2] : nil
            let startsNumber = c.isNumber
                || (c == "$" && next?.isNumber == true)
                || (c == "+" && (next?.isNumber == true || (next == "$" && afterNext?.isNumber == true)))
            if startsNumber {
                if !currentIsNumber { flush(); currentIsNumber = true }
                current.append(c)
                i += 1
                while i < chars.count {
                    let n = chars[i]
                    let following: Character? = i + 1 < chars.count ? chars[i + 1] : nil
                    if n.isNumber || n == "$" && !current.contains("$") && current == "+" {
                        current.append(n); i += 1; continue
                    }
                    if n == "%" {
                        current.append(n); i += 1; continue
                    }
                    if ",./:".contains(n), following?.isNumber == true {
                        current.append(n); i += 1; continue
                    }
                    break
                }
                continue
            }
            if currentIsNumber { flush(); currentIsNumber = false }
            current.append(c)
            i += 1
        }
        flush()
        return out
    }
}
