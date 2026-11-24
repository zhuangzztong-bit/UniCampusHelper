import QtQuick

Rectangle {
    id: track
    property real value: 0
    property color fillColor: value >= 0.999 ? Theme.success : Theme.accent
    implicitHeight: 6
    radius: height / 2
    color: Theme.track
    clip: true
    Accessible.role: Accessible.ProgressBar
    Accessible.name: "完成进度 " + Math.round(value * 100) + "%"

    Rectangle {
        height: track.height
        width: track.width * Math.max(0, Math.min(1, track.value))
        radius: track.radius
        color: track.fillColor
        Behavior on width {
            NumberAnimation { duration: Theme.durProgress; easing.type: Easing.OutCubic }
        }
        Behavior on color {
            ColorAnimation { duration: Theme.durFast }
        }
    }
}
