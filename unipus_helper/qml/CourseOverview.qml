import QtQuick
import QtQuick.Layouts

Item {
    id: overview
    property var counts: ({ all: 0, done: 0, pending: 0 })
    readonly property real completion: counts.all > 0 ? counts.done / counts.all : 0
    property bool compact: false
    implicitHeight: compact ? 70 : 80

    RowLayout {
        anchors.fill: parent
        spacing: overview.compact ? 10 : 14

        Repeater {
            model: [
                { key: "all", title: "总任务数", icon: "list", tone: Theme.ink, rail: Theme.line },
                { key: "done", title: "已完成", icon: "completed", tone: Theme.success, rail: Theme.success },
                { key: "pending", title: "待完成", icon: "pending", tone: Theme.compulsory, rail: Theme.warning }
            ]
            delegate: Rectangle {
                id: card
                required property var modelData
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: overview.compact ? 140 : 185
                Layout.minimumWidth: 120
                radius: 10
                color: Theme.surface
                border.width: 1
                border.color: Theme.line
                clip: true

                Rectangle {
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                    height: 2
                    color: card.modelData.rail
                    opacity: card.modelData.key === "all" ? 0.55 : 0.85
                }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: overview.compact ? 14 : 18
                    anchors.rightMargin: overview.compact ? 14 : 18
                    spacing: overview.compact ? 10 : 14

                    Rectangle {
                        width: overview.compact ? 32 : 36
                        height: width
                        radius: 8
                        color: Theme.raised
                        border.width: 1
                        border.color: Theme.subtleLine
                        Icon {
                            name: card.modelData.icon
                            anchors.centerIn: parent
                            width: 18; height: 18
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true; spacing: 2
                        UiText {
                            text: card.modelData.title
                            color: Theme.muted
                            font.pixelSize: 12
                            Layout.fillWidth: true
                        }
                        UiText {
                            objectName: "overview_" + card.modelData.key
                            text: String(overview.counts[card.modelData.key] || 0)
                            mono: true
                            color: card.modelData.tone
                            font.pixelSize: overview.compact ? 22 : 26
                            font.weight: Font.DemiBold
                            font.features: { "tnum": 1 }
                        }
                    }
                }
            }
        }

        // Progress card (wider proportion for readable progress track)
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: overview.compact ? 220 : 300
            Layout.minimumWidth: overview.compact ? 180 : 220
            radius: 10
            color: Theme.surface
            border.width: 1
            border.color: Theme.line
            clip: true

            Rectangle {
                anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                height: 2
                color: overview.completion >= 0.999 ? Theme.success : Theme.accent
                opacity: 0.85
            }

            ColumnLayout {
                anchors.fill: parent
                anchors.leftMargin: overview.compact ? 14 : 18
                anchors.rightMargin: overview.compact ? 14 : 18
                anchors.topMargin: 12; anchors.bottomMargin: 12
                spacing: 10

                RowLayout {
                    Layout.fillWidth: true
                    UiText {
                        text: "整体完成进度"
                        color: Theme.muted
                        font.pixelSize: 12
                        Layout.fillWidth: true
                    }
                    UiText {
                        objectName: "overview_progress"
                        text: (overview.completion * 100).toFixed(1) + "%"
                        mono: true
                        font.pixelSize: overview.compact ? 16 : 18
                        font.weight: Font.DemiBold
                        font.features: { "tnum": 1 }
                        color: overview.completion >= 0.999 ? Theme.success : Theme.ink
                    }
                }

                ProgressTrack {
                    objectName: "courseProgress"
                    Layout.fillWidth: true
                    value: overview.completion
                    implicitHeight: 8
                }
            }
        }
    }
}
