import QtQuick
import QtQuick.Controls

CheckBox {
    id: control
    property string hint: ""
    spacing: 10
    padding: 2
    implicitHeight: 32
    hoverEnabled: true
    opacity: enabled ? 1 : 0.45
    Behavior on opacity { NumberAnimation { duration: Theme.durFast } }
    indicator: CheckIndicator {
        x: control.leftPadding
        y: (control.height - height) / 2
        width: 18
        height: 18
        checkState: control.checkState
        highlighted: control.activeFocus || control.hovered
    }
    ToolTip.visible: hovered && hint !== ""
    ToolTip.text: hint
    ToolTip.delay: 650
    HoverHandler { enabled: control.enabled; cursorShape: Qt.PointingHandCursor }
    contentItem: UiText {
        text: control.text
        leftPadding: control.indicator.width + control.spacing
        color: control.hovered ? Theme.ink : Theme.secondary
        font.pixelSize: 13
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }
}
