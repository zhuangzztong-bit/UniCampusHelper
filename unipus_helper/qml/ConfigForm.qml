import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property string notice: ""
    property bool noticeOk: true
    signal savedSuccess()

    function collectValues() {
        let values = {
            "token_full": tokenInput.text,
            "debug_mode": debugCheck.checked,
            "screenshot": screenshotCheck.checked
        }
        accountGroup.collectInto(values)
        modelGroup.collectInto(values)
        runtimeGroup.collectInto(values)
        return values
    }

    function saveSettings() {
        configEditor.save(collectValues())
    }

    Connections {
        target: configEditor
        function onSaved(message, ok) {
            root.notice = message
            root.noticeOk = ok
            if (ok) root.savedSuccess()
        }
    }

    component FieldGroup: Rectangle {
        id: groupBox
        property string title: ""
        property string subtitle: ""
        property var model: []
        function collectInto(target) {
            for (let i = 0; i < rep.count; ++i) {
                const row = rep.itemAt(i)
                if (row) target[row.fieldKey] = row.value
            }
        }
        Layout.fillWidth: true
        implicitHeight: groupCol.implicitHeight + 36
        radius: 10
        color: Theme.surface
        border.width: 1
        border.color: Theme.line

        ColumnLayout {
            id: groupCol
            anchors.fill: parent
            anchors.margins: 18
            spacing: 12

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                UiText { text: groupBox.title; font.pixelSize: 15; font.weight: Font.DemiBold }
                UiText {
                    visible: groupBox.subtitle !== ""
                    text: groupBox.subtitle
                    color: Theme.muted
                    font.pixelSize: 12
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: Theme.subtleLine }

            Repeater {
                id: rep
                model: groupBox.model
                delegate: RowLayout {
                    required property var modelData
                    property string fieldKey: modelData.key
                    property alias value: input.text
                    Layout.fillWidth: true
                    spacing: 16

                    UiText {
                        Layout.preferredWidth: 138
                        text: modelData.label
                        color: Theme.secondary
                        font.pixelSize: 13
                    }

                    TextField {
                        id: input
                        objectName: "field_" + modelData.key
                        Layout.fillWidth: true
                        implicitHeight: 36
                        text: configEditor.values[modelData.key] == null ? "" : String(configEditor.values[modelData.key])
                        echoMode: modelData.key === "password" || modelData.key === "api_key" ? TextInput.Password : TextInput.Normal
                        placeholderText: modelData.hint
                        placeholderTextColor: Theme.muted
                        color: Theme.ink
                        selectionColor: Theme.accentFill
                        selectedTextColor: "#ffffff"
                        font.family: modelData.mono ? Theme.monoFamily : Theme.fontFamily
                        font.pixelSize: 13
                        leftPadding: 12; rightPadding: 12
                        Accessible.name: modelData.label
                        background: Rectangle {
                            radius: 7
                            color: Theme.canvas
                            border.width: 1
                            border.color: input.activeFocus ? Theme.accent : Theme.line
                            Behavior on border.color { ColorAnimation { duration: Theme.durFast } }
                        }
                    }
                }
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 14

        ScrollView {
            id: scroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

            ColumnLayout {
                width: scroll.availableWidth
                spacing: 14

                FieldGroup {
                    id: accountGroup
                    title: "U 校园账号与登录"
                    subtitle: "用于自动登录 U 校园课程工作台"
                    model: [
                        { key: "username", label: "账号 *", hint: "输入 U 校园 AI 版账号", mono: true },
                        { key: "password", label: "密码 *", hint: "输入登录密码", mono: false },
                        { key: "url", label: "登录地址", hint: "U 校园统一登录地址", mono: true }
                    ]
                }

                FieldGroup {
                    id: modelGroup
                    title: "大模型 API 配置"
                    subtitle: "兼容 OpenAI 接口协议（DeepSeek / Kimi / Qwen / OpenAI 等）"
                    model: [
                        { key: "api_key", label: "API Key *", hint: "输入接口密钥", mono: true },
                        { key: "base_url", label: "API 地址 *", hint: "例如 https://api.deepseek.com/v1", mono: true },
                        { key: "model", label: "模型名称 *", hint: "例如 deepseek-chat", mono: true },
                        { key: "max_tokens", label: "最大 Token 数", hint: "32768", mono: true },
                        { key: "temperature", label: "温度（0–2）", hint: "0.3", mono: true }
                    ]
                }

                FieldGroup {
                    id: runtimeGroup
                    title: "语音合成与浏览器"
                    subtitle: "「我的音色」已启用时会优先使用你的专属音色，此处作为标准回退语音配置"
                    model: [
                        { key: "tts_engine", label: "标准 TTS 引擎", hint: "auto / edge / sapi / off", mono: true },
                        { key: "tts_voice", label: "标准回退音色", hint: "留空使用默认自然英语音色", mono: true },
                        { key: "tts_proxy", label: "TTS 网络代理", hint: "可选，例如 http://127.0.0.1:7890", mono: true },
                        { key: "video_playback_rate", label: "视频原生倍速", hint: "1.0（默认）/ 1.25 / 1.5 / 2.0（仅播放器支持时生效）", mono: true },
                        { key: "browser", label: "浏览器内核", hint: "edge（默认）/ chrome", mono: true }
                    ]
                }

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: advCol.implicitHeight + 36
                    radius: 10
                    color: Theme.surface
                    border.width: 1
                    border.color: Theme.line

                    ColumnLayout {
                        id: advCol
                        anchors.fill: parent
                        anchors.margins: 18
                        spacing: 12

                        UiText { text: "高级与调试选项"; font.pixelSize: 15; font.weight: Font.DemiBold }
                        Rectangle { Layout.fillWidth: true; height: 1; color: Theme.subtleLine }

                        RowLayout {
                            Layout.fillWidth: true; Layout.alignment: Qt.AlignTop; spacing: 16
                            UiText {
                                Layout.preferredWidth: 138; Layout.alignment: Qt.AlignTop; Layout.topMargin: 8
                                text: "Token（可选）"; color: Theme.secondary; font.pixelSize: 13
                            }
                            ScrollView {
                                Layout.fillWidth: true; Layout.preferredHeight: 84
                                clip: true
                                background: Rectangle {
                                    radius: 7; color: Theme.canvas
                                    border.width: 1
                                    border.color: tokenInput.activeFocus ? Theme.accent : Theme.line
                                }
                                TextArea {
                                    id: tokenInput
                                    objectName: "field_token_full"
                                    text: configEditor.values.token_full || ""
                                    placeholderText: "留空自动使用浏览器登录态；也可粘贴 __token 或 5.0 Authorization"
                                    placeholderTextColor: Theme.muted
                                    color: Theme.ink; selectionColor: Theme.accentFill; selectedTextColor: "#ffffff"
                                    font.family: Theme.monoFamily; font.pixelSize: 12
                                    wrapMode: TextEdit.Wrap
                                    Accessible.name: "Token（可选）"
                                }
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true; spacing: 16
                            UiText { Layout.preferredWidth: 138; text: "调试日志"; color: Theme.secondary; font.pixelSize: 13 }
                            UiCheck {
                                id: debugCheck; objectName: "field_debug_mode"
                                text: "开启详细调试输出"
                                checked: !!configEditor.values.debug_mode
                                Layout.fillWidth: true
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true; spacing: 16
                            UiText { Layout.preferredWidth: 138; text: "视觉识图"; color: Theme.secondary; font.pixelSize: 13 }
                            ColumnLayout {
                                Layout.fillWidth: true; spacing: 4
                                UiCheck {
                                    id: screenshotCheck; objectName: "field_screenshot"
                                    text: "发送题目区域截图给支持多模态的模型"
                                    checked: !!configEditor.values.screenshot
                                    Layout.fillWidth: true
                                }
                                UiText {
                                    text: "开启后会消耗较多 Token，仅在使用 qwen-vl / gpt-4o 等识图模型且遇到看图口语题时建议打开。"
                                    color: Theme.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true
                                }
                            }
                        }
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 56
            radius: 10
            color: Theme.surface
            border.width: 1
            border.color: Theme.line

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 18; anchors.rightMargin: 16
                spacing: 12
                UiText {
                    text: root.notice !== "" ? root.notice : "修改完成后点击保存；「我的音色」设置实时生效"
                    color: root.notice === "" ? Theme.muted : root.noticeOk ? Theme.success : Theme.error
                    Layout.fillWidth: true
                    font.pixelSize: 13
                }
                ActionButton {
                    objectName: "saveButton"
                    text: "保存配置"
                    iconName: "check"
                    primary: true
                    implicitHeight: 38
                    leftPadding: 20; rightPadding: 20
                    onClicked: root.saveSettings()
                }
            }
        }
    }
}
