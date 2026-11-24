pragma Singleton
import QtQuick

QtObject {
    // Paper & Ink Academic Light Palette (4-step warm cloud-paper elevation, <= 5 semantic hues)
    // Tailored for university students' daily study workbench: calm, legible, low eye-strain, zero harsh #000000
    readonly property bool dark: false
    readonly property string fontFamily: Qt.application.font.family
    property string monoFamily: Qt.platform.os === "windows" ? "Consolas" : "Menlo"

    // 4-step cloud-paper surface hierarchy (clear section alternation)
    readonly property color canvas: "#f5f7fb"
    readonly property color sidebar: "#edf1f7"
    readonly property color surface: "#ffffff"
    readonly property color raised: "#f1f5fa"
    readonly property color hover: "#e6edf6"

    // Crisp 1px hairlines
    readonly property color line: "#d6dfec"
    readonly property color subtleLine: "#e5ebf4"

    // Ink hierarchy (WCAG AA/AAA calibrated deep slate-ink, no pure #000000)
    readonly property color ink: "#182230"
    readonly property color secondary: "#475569"
    readonly property color muted: "#64748b"

    // Primary interaction academic cobalt blue
    readonly property color accent: "#2563eb"
    readonly property color accentBright: "#1d4ed8"
    readonly property color accentFill: "#2563eb"
    readonly property color accentHover: "#1d4ed8"
    readonly property color accentPressed: "#1e40af"
    readonly property color navigation: "#e0ebff"
    readonly property color selection: "#edf4ff"
    readonly property color selectionHover: "#e1ecff"
    readonly property color selectionLine: "#bcd3fe"
    readonly property color track: "#dde5f0"

    // Semantic: Success / Calm Pine Green
    readonly property color success: "#15803d"
    readonly property color successInk: "#166534"
    readonly property color successSurface: "#ecfdf3"
    readonly property color successLine: "#bbf7d0"

    // Semantic: Warning & Compulsory / Warm Amber (high contrast on light paper)
    readonly property color warning: "#b45309"
    readonly property color warningSurface: "#fffbeb"
    readonly property color warningLine: "#fde68a"
    readonly property color compulsory: "#b45309"
    readonly property color compulsorySurface: "#fef3c7"
    readonly property color compulsoryLine: "#fcd34d"
    readonly property color requiredInk: "#92400e"

    // Semantic: Error & Recording Crimson
    readonly property color error: "#dc2626"
    readonly property color errorSurface: "#fef2f2"
    readonly property color errorHover: "#fee2e2"
    readonly property color errorPressed: "#fecaca"
    readonly property color errorLine: "#fecaca"
    readonly property color recordRed: "#dc2626"
    readonly property color recordSurface: "#fef2f2"

    // Motion tokens (data-motion="gentle": crisp OutCubic, exit = 0.7x enter)
    readonly property int durInstant: 90
    readonly property int durFast: 160
    readonly property int durBase: 220
    readonly property int durExit: 150
    readonly property int durProgress: 320
}
