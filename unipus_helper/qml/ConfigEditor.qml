import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: window
    objectName: "configWindow"
    title: "UniCampusHelper 初始配置"
    width: 820; height: 820
    minimumWidth: 640; minimumHeight: 560
    visible: true
    color: Theme.canvas
    palette.window: Theme.surface
    palette.windowText: Theme.ink
    palette.base: Theme.canvas
    palette.text: Theme.ink
    palette.highlight: Theme.accentFill
    palette.highlightedText: "white"

    Component.onCompleted: {
        if (typeof uiMonoFont !== "undefined" && uiMonoFont)
            Theme.monoFamily = uiMonoFont
    }

    Shortcut { sequences: [StandardKey.Save]; onActivated: configForm.saveSettings() }
    Shortcut { sequence: "Escape"; onActivated: window.close() }
    Shortcut { sequences: [StandardKey.Close]; onActivated: window.close() }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 16

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 4
            UiText { text: "系统配置"; font.pixelSize: 24; font.weight: Font.DemiBold }
            UiText {
                text: configEditor.firstRun ? "请先填写 U 校园账号与大模型接口必填项，保存后自动进入工作台" : "管理账号、大模型接口与语音浏览器配置"
                color: Theme.muted; font.pixelSize: 13
            }
        }

        ConfigForm {
            id: configForm
            Layout.fillWidth: true
            Layout.fillHeight: true
            onSavedSuccess: {
                if (configEditor.firstRun) window.close()
            }
        }
    }
}
