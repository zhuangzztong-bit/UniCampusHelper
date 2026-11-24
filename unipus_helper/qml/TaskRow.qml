import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: row
    required property int index
    required property string taskId
    required property string title
    required property string status
    required property string statusName
    required property bool chosen
    required property bool isRequired
    required property var scored
    required property string sectionHeader
    required property bool selectable
    required property string scoreText
    required property bool scoreAbnormal
    property bool editable: true
    readonly property string selectionHelp: statusName === "需手动对话"
        ? "在 U 校园完成实时语音对话"
        : "当前已排除不计分任务"
    height: sectionHeader ? 82 : 48

    function toggleSelection(keyboard) {
        if (!row.editable || !row.selectable) return
        row.ListView.view.currentIndex = row.index
        if (keyboard || !taskControl.activeFocus)
            taskControl.forceActiveFocus(keyboard ? Qt.TabFocusReason : Qt.MouseFocusReason)
        workbench.selectTask(row.taskId, !row.chosen)
    }

    function focusTask() { taskControl.forceActiveFocus(Qt.TabFocusReason) }
    function focusIndex(targetIndex) {
        const view = row.ListView.view
        const next = Math.max(0, Math.min(view.count - 1, targetIndex))
        view.currentIndex = next
        view.positionViewAtIndex(next, ListView.Contain)
        Qt.callLater(function() {
            const target = view.itemAtIndex(next)
            if (target) target.focusTask()
        })
    }

    function moveFocus(step) { focusIndex(row.index + step) }

    Rectangle {
        width: parent.width; height: 34; visible: row.sectionHeader !== ""
        color: Theme.canvas
        Rectangle { anchors.top: parent.top; width: parent.width; height: 1; color: Theme.subtleLine }
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.subtleLine }
        UiText {
            id: sectionTitle
            x: 24; height: parent.height; width: parent.width - 48
            text: row.sectionHeader
            color: Theme.muted
            font.pixelSize: 12
            font.weight: Font.Medium
        }
        HoverHandler { id: sectionHover }
        ToolTip.visible: sectionHover.hovered && sectionTitle.truncated
        ToolTip.text: row.sectionHeader
        ToolTip.delay: 650
    }

    ItemDelegate {
        id: taskControl
        objectName: "task_" + row.taskId
        anchors.fill: parent; anchors.topMargin: row.sectionHeader ? 34 : 0
        enabled: row.editable; hoverEnabled: true
        checkable: false; checked: row.chosen
        focusPolicy: Qt.StrongFocus
        leftPadding: 24; rightPadding: 24; topPadding: 0; bottomPadding: 0
        Accessible.role: Accessible.CheckBox
        Accessible.name: row.title + "，" + (row.isRequired ? "必修" : "选修") + "，" + row.statusName
        Accessible.checkable: true
        Accessible.checked: row.chosen
        Accessible.onPressAction: row.toggleSelection(false)
        Accessible.onToggleAction: row.toggleSelection(false)
        Accessible.description: row.selectable ? "点击整行或按空格勾选任务" : row.selectionHelp
        onClicked: row.toggleSelection(false)
        Keys.onSpacePressed: row.toggleSelection(true)
        Keys.onReturnPressed: row.toggleSelection(true)
        Keys.onEnterPressed: row.toggleSelection(true)
        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Home || event.key === Qt.Key_End) {
                row.focusIndex(event.key === Qt.Key_Home ? 0 : row.ListView.view.count - 1)
                event.accepted = true
            } else if (event.key === Qt.Key_A && (event.modifiers & Qt.ControlModifier)) {
                workbench.selectVisible("all")
                event.accepted = true
            }
        }
        Keys.onUpPressed: row.moveFocus(-1)
        Keys.onDownPressed: row.moveFocus(1)

        background: Rectangle {
            color: taskControl.down && row.selectable ? Theme.selectionHover
                 : row.chosen ? (taskControl.hovered ? Theme.selectionHover : Theme.selection)
                 : taskControl.hovered ? Theme.raised : "transparent"
            Behavior on color { ColorAnimation { duration: Theme.durFast } }

            // Left 3px selection rail
            Rectangle {
                anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom
                width: 3
                color: taskControl.visualFocus ? Theme.accentBright : Theme.accent
                visible: row.chosen || taskControl.visualFocus
            }
            // Subtle bottom hairline
            Rectangle {
                anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
                anchors.leftMargin: 20; anchors.rightMargin: 20
                height: 1
                color: Theme.subtleLine
                opacity: row.chosen ? 0.35 : 0.7
            }
        }

        HoverHandler { enabled: row.editable; cursorShape: row.selectable ? Qt.PointingHandCursor : Qt.ForbiddenCursor }

        contentItem: RowLayout {
            spacing: 16
            CheckIndicator {
                objectName: "taskCheck_" + row.taskId
                Layout.preferredWidth: 18; Layout.preferredHeight: 18; Layout.rightMargin: 2
                checkState: row.chosen ? Qt.Checked : Qt.Unchecked
                opacity: row.selectable ? 1 : 0.4
                highlighted: row.selectable && (taskControl.hovered || taskControl.visualFocus)
            }
            UiText {
                id: taskTitle
                text: row.title
                Layout.fillWidth: true; Layout.minimumWidth: 0
                font.pixelSize: 14
                font.weight: row.status === "doing" || row.chosen ? Font.DemiBold : Font.Normal
                color: row.selectable ? Theme.ink : Theme.muted
            }
            Rectangle {
                objectName: "requirement_" + row.taskId
                Layout.preferredWidth: 48; Layout.preferredHeight: 24; radius: 6
                color: row.isRequired ? Theme.compulsorySurface : Theme.raised
                border.width: 1
                border.color: row.isRequired ? Theme.compulsoryLine : Theme.line
                UiText {
                    anchors.centerIn: parent
                    text: row.isRequired ? "必修" : "选修"
                    color: row.isRequired ? Theme.requiredInk : Theme.muted
                    font.pixelSize: 12
                    font.weight: row.isRequired ? Font.Medium : Font.Normal
                }
            }
            UiText {
                text: row.scored === false ? "不计分" : row.scored === null ? "待确认" : "计分"
                color: row.scored === false ? Theme.muted : Theme.secondary
                font.pixelSize: 12
                Layout.preferredWidth: 64
                horizontalAlignment: Text.AlignHCenter
            }
            Item {
                Layout.preferredWidth: 68; Layout.preferredHeight: 24
                Rectangle {
                    objectName: "score_" + row.taskId
                    anchors.fill: parent
                    visible: row.scoreText !== ""
                    radius: 6
                    color: row.scoreAbnormal ? Theme.errorSurface : Theme.successSurface
                    border.width: 1
                    border.color: row.scoreAbnormal ? Theme.errorLine : Theme.successLine
                    UiText {
                        anchors.centerIn: parent
                        text: row.scoreText
                        mono: true
                        font.features: { "tnum": 1 }
                        font.pixelSize: 12
                        font.weight: Font.Medium
                        color: row.scoreAbnormal ? Theme.error : Theme.successInk
                    }
                }
            }
            Rectangle {
                Layout.preferredWidth: 74; Layout.preferredHeight: 24; radius: 6
                color: row.status === "done" ? Theme.successSurface
                     : row.status === "doing" ? Theme.navigation : Theme.raised
                border.width: 1
                border.color: row.status === "done" ? Theme.successLine
                            : row.status === "doing" ? Theme.selectionLine : Theme.line
                UiText {
                    anchors.centerIn: parent
                    text: row.statusName
                    font.pixelSize: 12
                    font.weight: row.status === "done" || row.status === "doing" ? Font.Medium : Font.Normal
                    color: row.status === "done" ? Theme.successInk
                         : row.status === "doing" ? Theme.accentBright : Theme.secondary
                }
            }
        }
        ToolTip.visible: hovered && (!row.selectable || taskTitle.truncated)
        ToolTip.text: row.selectable ? row.title : row.selectionHelp
        ToolTip.delay: 650
    }
}
