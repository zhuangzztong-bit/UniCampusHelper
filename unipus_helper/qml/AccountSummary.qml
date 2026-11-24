import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: identity
    property string account: ""
    property string model: ""
    property string status: ""
    property string apiStatus: ""
    property bool apiOk: false
    property bool apiFail: false
    property bool busy: false
    property bool ready: false
    property bool hasError: false
    property bool voiceActive: false
    property string voiceLabel: ""
    signal voiceClicked()

    implicitHeight: 72
    radius: 10
    color: Theme.surface
    border.width: 1
    border.color: identity.hasError ? Theme.errorLine : Theme.line

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 12; anchors.rightMargin: 14; anchors.topMargin: 10; anchors.bottomMargin: 10
        spacing: 12

        Rectangle {
            width: 34; height: 34; radius: 8
            color: Theme.navigation
            border.width: 1
            border.color: Theme.selectionLine
            Icon { name: "account"; anchors.centerIn: parent; width: 18; height: 18 }
        }

        ColumnLayout {
            Layout.fillWidth: true; Layout.minimumWidth: 0; spacing: 5

            RowLayout {
                Layout.fillWidth: true; spacing: 8
                UiText { text: "账号"; font.pixelSize: 12; color: Theme.muted; Layout.preferredWidth: 26 }
                UiText {
                    objectName: "accountName"
                    text: identity.account
                    mono: true
                    font.pixelSize: 13
                    font.weight: Font.Medium
                    Layout.fillWidth: true; Layout.minimumWidth: 0
                }
                Rectangle {
                    width: 7; height: 7; radius: 4
                    color: identity.hasError ? Theme.error : identity.busy ? Theme.accent : identity.ready ? Theme.success : Theme.muted
                    SequentialAnimation on opacity {
                        running: identity.busy
                        loops: Animation.Infinite
                        NumberAnimation { to: 0.35; duration: 650; easing.type: Easing.InOutSine }
                        NumberAnimation { to: 1.0; duration: 650; easing.type: Easing.InOutSine }
                    }
                    onVisibleChanged: opacity = 1.0
                }
                UiText {
                    objectName: "runtimeStatus"
                    text: identity.status
                    font.pixelSize: 12
                    font.weight: Font.Medium
                    color: identity.hasError ? Theme.error : identity.busy ? Theme.accent : Theme.secondary
                    Layout.maximumWidth: 92
                }
            }

            RowLayout {
                Layout.fillWidth: true; spacing: 8
                UiText { text: "模型"; font.pixelSize: 12; color: Theme.muted; Layout.preferredWidth: 26 }
                UiText {
                    id: modelText; objectName: "modelName"
                    text: identity.model
                    mono: true
                    font.pixelSize: 12; font.weight: Font.Medium
                    color: Theme.accent
                    Layout.fillWidth: true; Layout.minimumWidth: 0
                    HoverHandler { id: modelHover }
                    ToolTip.visible: modelHover.hovered && modelText.truncated
                    ToolTip.text: modelText.text
                    ToolTip.delay: 650
                }
                Rectangle {
                    visible: identity.voiceLabel !== ""
                    implicitWidth: voiceRow.implicitWidth + 12
                    implicitHeight: 20
                    radius: 10
                    color: identity.voiceActive ? Theme.successSurface : Theme.raised
                    border.width: 1
                    border.color: identity.voiceActive ? Theme.successLine : Theme.line
                    RowLayout {
                        id: voiceRow
                        anchors.centerIn: parent
                        spacing: 4
                        Rectangle {
                            width: 5; height: 5; radius: 3
                            color: identity.voiceActive ? Theme.success : Theme.muted
                        }
                        UiText {
                            text: identity.voiceLabel
                            font.pixelSize: 11
                            color: identity.voiceActive ? Theme.successInk : Theme.muted
                        }
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: identity.voiceClicked()
                    }
                }
                UiText {
                    id: apiStatusText; objectName: "apiStatus"
                    text: identity.apiStatus
                    mono: true
                    font.features: { "tnum": 1 }
                    font.pixelSize: 11; Layout.maximumWidth: 128
                    color: identity.apiOk ? Theme.success : identity.apiFail ? Theme.error : Theme.muted
                    HoverHandler { id: apiHover }
                    ToolTip.visible: apiHover.hovered && apiStatusText.truncated
                    ToolTip.text: identity.apiStatus
                    ToolTip.delay: 650
                }
            }
        }
    }
}
