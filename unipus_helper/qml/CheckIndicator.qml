import QtQuick

Rectangle {
    property int checkState: Qt.Unchecked
    property bool highlighted: false
    implicitWidth: 18; implicitHeight: 18
    radius: 5
    color: checkState !== Qt.Unchecked ? Theme.accentFill : Theme.canvas
    border.width: 1
    border.color: highlighted ? Theme.accent : checkState !== Qt.Unchecked ? Theme.accentFill : Theme.line
    Behavior on color { ColorAnimation { duration: Theme.durInstant } }
    Behavior on border.color { ColorAnimation { duration: Theme.durInstant } }

    Icon {
        anchors.centerIn: parent
        name: "check"; white: true; width: 13; height: 13
        visible: parent.checkState === Qt.Checked
    }
    Rectangle {
        anchors.centerIn: parent
        width: 8; height: 2; radius: 1; color: "white"
        visible: parent.checkState === Qt.PartiallyChecked
    }
}
