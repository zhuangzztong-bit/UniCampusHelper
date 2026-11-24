import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ListView {
    id: view
    property var entries: []
    property string levelFilter: "all" // all | issue | success
    property bool followTail: true

    readonly property var filteredEntries: {
        const list = entries || []
        if (levelFilter === "all") return list
        if (levelFilter === "issue") return list.filter(e => e.level === "error" || e.level === "warning")
        if (levelFilter === "success") return list.filter(e => e.level === "success")
        return list
    }

    function followLatest() { if (followTail && count > 0) Qt.callLater(function() { view.positionViewAtEnd() }) }
    model: filteredEntries
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    spacing: 0
    onEntriesChanged: followLatest()
    onLevelFilterChanged: followLatest()
    onCountChanged: followLatest()
    onHeightChanged: followLatest()
    onWidthChanged: followLatest()
    onMovementStarted: followTail = false
    onMovementEnded: followTail = atYEnd
    onAtYEndChanged: if (atYEnd && !moving) followTail = true

    header: Rectangle {
        width: view.width; height: 34; color: Theme.raised
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.line }
        RowLayout {
            anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 16; spacing: 16
            UiText { text: "时间"; font.pixelSize: 12; color: Theme.muted; Layout.preferredWidth: 74 }
            UiText { text: "级别"; font.pixelSize: 12; color: Theme.muted; Layout.preferredWidth: 44 }
            UiText { text: "执行事件"; font.pixelSize: 12; color: Theme.muted; Layout.fillWidth: true }
        }
    }

    delegate: Rectangle {
        id: logRow
        required property var modelData
        readonly property color tone: modelData.level === "error" ? Theme.error
                                    : modelData.level === "warning" ? Theme.warning
                                    : modelData.level === "success" ? Theme.success : Theme.secondary
        readonly property string levelName: modelData.level === "error" ? "错误"
                                          : modelData.level === "warning" ? "提醒"
                                          : modelData.level === "success" ? "成功" : "信息"
        objectName: "log_" + modelData.id
        width: view.width
        height: Math.max(40, logContent.implicitHeight + 20)
        color: modelData.level === "error" ? Qt.rgba(0.97, 0.44, 0.44, 0.07)
             : modelData.level === "warning" ? Qt.rgba(0.96, 0.62, 0.04, 0.05) : "transparent"

        Rectangle {
            anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom
            width: 3; color: logRow.tone
            visible: logRow.modelData.level !== "info"
        }
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.subtleLine }

        RowLayout {
            anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 16
            anchors.topMargin: 10; anchors.bottomMargin: 10; spacing: 16
            UiText {
                text: logRow.modelData.time
                mono: true
                color: Theme.muted
                font.features: { "tnum": 1 }
                font.pixelSize: 12
                Layout.alignment: Qt.AlignTop; Layout.preferredWidth: 74; Layout.topMargin: 2
            }
            Rectangle {
                Layout.alignment: Qt.AlignTop
                Layout.preferredWidth: 44; Layout.preferredHeight: 20
                radius: 4
                color: Qt.rgba(logRow.tone.r, logRow.tone.g, logRow.tone.b, 0.14)
                UiText {
                    anchors.centerIn: parent
                    text: logRow.levelName
                    color: logRow.tone
                    font.pixelSize: 11
                    font.weight: Font.Medium
                }
            }
            TextEdit {
                id: logContent
                objectName: "logMessage_" + logRow.modelData.id
                text: logRow.modelData.message
                color: logRow.modelData.level === "info" ? Theme.ink : logRow.tone
                font.family: Theme.fontFamily
                font.weight: Font.Normal
                font.pixelSize: 13
                font.hintingPreference: Font.PreferFullHinting
                renderType: TextEdit.NativeRendering
                textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                selectedTextColor: "white"; selectionColor: Theme.accentFill
                Layout.fillWidth: true; Layout.minimumWidth: 0; Layout.alignment: Qt.AlignTop
                Accessible.name: logRow.levelName + "，" + logRow.modelData.message
            }
        }
    }

    UiText {
        visible: view.count === 0
        anchors.centerIn: parent
        text: view.levelFilter === "all" ? "暂无运行日志" : "当前筛选条件下没有匹配日志"
        font.pixelSize: 14; color: Theme.muted
    }
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; onPressedChanged: { if (pressed) view.followTail = false; else view.followTail = view.atYEnd } }
}
