import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Button {
    id: control
    property bool primary: false
    property bool quiet: false
    property bool destructive: false
    property bool accented: false
    property string iconName: ""
    property string hint: ""
    implicitHeight: 36
    implicitWidth: innerRow.implicitWidth + leftPadding + rightPadding
    leftPadding: 14
    rightPadding: 14
    hoverEnabled: true
    opacity: enabled ? 1 : 0.42
    scale: (control.down && control.enabled) ? 0.98 : 1.0
    font.pixelSize: 13

    Behavior on scale { NumberAnimation { duration: Theme.durInstant; easing.type: Easing.OutCubic } }

    background: Rectangle {
        radius: 8
        color: control.primary
             ? (control.down ? Theme.accentPressed : control.hovered ? Theme.accentHover : Theme.accentFill)
             : control.destructive && !control.quiet
             ? (control.down ? Theme.errorPressed : control.hovered ? Theme.errorHover : Theme.errorSurface)
             : control.down ? Theme.hover
             : control.hovered ? Theme.raised
             : control.quiet ? "transparent" : Theme.surface
        border.width: control.primary || control.quiet ? 0 : 1
        border.color: control.activeFocus ? Theme.accent
                    : control.destructive ? Theme.errorLine
                    : control.hovered ? Theme.selectionLine : Theme.line

        Rectangle {
            anchors.fill: parent
            anchors.margins: -2
            color: "transparent"
            radius: 10
            border.width: 1
            border.color: Theme.accent
            visible: control.visualFocus
        }
    }
    ToolTip.visible: hovered && hint !== ""
    ToolTip.text: hint
    ToolTip.delay: 650
    HoverHandler { enabled: control.enabled; cursorShape: Qt.PointingHandCursor }
    contentItem: Item {
        implicitWidth: innerRow.implicitWidth
        implicitHeight: innerRow.implicitHeight
        Row {
            id: innerRow
            anchors.centerIn: parent
            spacing: control.iconName !== "" && control.text !== "" ? 7 : 0
            Icon {
                anchors.verticalCenter: parent.verticalCenter
                name: control.iconName || "list"
                visible: control.iconName !== ""
                white: control.primary
                width: 15
                height: 15
            }
            UiText {
                anchors.verticalCenter: parent.verticalCenter
                text: control.text
                visible: control.text !== ""
                font.pixelSize: 13
                font.weight: control.primary ? Font.DemiBold : Font.Medium
                color: control.primary ? "#ffffff"
                     : control.destructive ? Theme.error
                     : control.accented ? Theme.accent : Theme.ink
            }
        }
    }
}
