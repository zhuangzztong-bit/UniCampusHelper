import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Button {
    id: control
    property bool selected: false
    property string iconName: "list"
    property string count: ""
    property string badgeText: ""
    property color badgeColor: Theme.success
    property bool showDot: false
    property color dotColor: Theme.success
    implicitHeight: 40
    leftPadding: 14; rightPadding: 12
    hoverEnabled: true
    HoverHandler { cursorShape: Qt.PointingHandCursor }

    background: Rectangle {
        radius: 8
        color: control.selected ? Theme.navigation : control.hovered ? Theme.hover : Theme.sidebar
        border.width: control.activeFocus || control.selected ? 1 : 0
        border.color: control.activeFocus ? Theme.accent : Theme.selectionLine

        // Crisp 3px left active rail (instant precision switching)
        Rectangle {
            anchors.left: parent.left
            anchors.leftMargin: 2
            anchors.verticalCenter: parent.verticalCenter
            width: 3
            height: 18
            radius: 2
            color: Theme.accent
            visible: control.selected
        }
    }

    contentItem: RowLayout {
        spacing: 11
        Icon { name: control.iconName; Layout.preferredWidth: 17; Layout.preferredHeight: 17 }
        UiText {
            text: control.text
            Layout.fillWidth: true
            color: control.selected ? Theme.ink : Theme.secondary
            font.pixelSize: 14
            font.weight: control.selected ? Font.DemiBold : Font.Medium
        }
        Rectangle {
            visible: control.showDot && control.badgeText === ""
            width: 7; height: 7; radius: 4
            color: control.dotColor
        }
        Rectangle {
            visible: control.badgeText !== ""
            implicitWidth: badgeLabel.implicitWidth + 12
            implicitHeight: 20
            radius: 10
            color: Qt.rgba(control.badgeColor.r, control.badgeColor.g, control.badgeColor.b, 0.16)
            border.width: 1
            border.color: Qt.rgba(control.badgeColor.r, control.badgeColor.g, control.badgeColor.b, 0.36)
            UiText {
                id: badgeLabel
                anchors.centerIn: parent
                text: control.badgeText
                color: control.badgeColor
                font.pixelSize: 11
                font.weight: Font.Medium
            }
        }
        UiText {
            visible: control.count !== ""
            text: control.count
            mono: true
            font.features: { "tnum": 1 }
            color: control.selected ? Theme.accentBright : Theme.muted
            font.pixelSize: 12
        }
    }
}
