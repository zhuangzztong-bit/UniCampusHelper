import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: studio
    objectName: "voiceStudioPage"
    readonly property var vs: voiceStudio.state
    property int activeScriptIndex: 0
    property bool cloudExpanded: vs.mode === "cloud"

    function formatTimer(sec) {
        const s = Math.max(0, Number(sec) || 0)
        const mm = Math.floor(s / 60)
        const ss = Math.floor(s % 60)
        const tenths = Math.floor((s * 10) % 10)
        return (mm < 10 ? "0" : "") + mm + ":" + (ss < 10 ? "0" : "") + ss + "." + tenths
    }

    component SegButton: Button {
        id: seg
        property bool active: false
        implicitHeight: 30
        leftPadding: 12; rightPadding: 12
        hoverEnabled: true
        HoverHandler { cursorShape: Qt.PointingHandCursor }
        background: Rectangle {
            radius: 6
            color: seg.active ? Theme.navigation : seg.hovered ? Theme.hover : Theme.canvas
            border.width: 1
            border.color: seg.active ? Theme.selectionLine : Theme.line
            Behavior on color { ColorAnimation { duration: Theme.durFast } }
        }
        contentItem: UiText {
            text: seg.text
            font.pixelSize: 12
            font.weight: seg.active ? Font.DemiBold : Font.Normal
            color: seg.active ? Theme.accentBright : Theme.secondary
            horizontalAlignment: Text.AlignHCenter
        }
    }

    component ToggleSwitch: Item {
        id: sw
        property bool checked: false
        signal toggled(bool value)
        width: 46; height: 26
        implicitWidth: 46; implicitHeight: 26
        Layout.preferredWidth: 46; Layout.preferredHeight: 26
        opacity: enabled ? 1 : 0.45

        Rectangle {
            anchors.fill: parent
            radius: 13
            color: sw.checked ? Theme.accentFill : Theme.canvas
            border.width: 1
            border.color: sw.checked ? Theme.accent : swArea.containsMouse ? Theme.selectionLine : Theme.line

            Rectangle {
                width: 18; height: 18; radius: 9
                y: 4
                x: sw.checked ? 24 : 4
                color: sw.checked ? "#ffffff" : Theme.secondary
                Behavior on x { NumberAnimation { duration: Theme.durFast; easing.type: Easing.OutCubic } }
            }
        }

        MouseArea {
            id: swArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: sw.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: if (sw.enabled) sw.toggled(!sw.checked)
        }
    }

    ScrollView {
        id: scroll
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            width: scroll.availableWidth
            spacing: 14

            // 1. Top Control Strip: Master Enable Switch + Engine Status
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: topBarCol.implicitHeight + 32
                radius: 10
                color: Theme.surface
                border.width: 1
                border.color: vs.readyToUse ? Theme.successLine : Theme.line

                Rectangle {
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                    height: 2
                    color: vs.readyToUse ? Theme.success : Theme.accent
                    opacity: 0.75
                }

                ColumnLayout {
                    id: topBarCol
                    anchors.fill: parent
                    anchors.margins: 16
                    spacing: 12

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 14

                        Rectangle {
                            width: 40; height: 40; radius: 9
                            color: vs.readyToUse ? Theme.successSurface : Theme.navigation
                            border.width: 1
                            border.color: vs.readyToUse ? Theme.successLine : Theme.selectionLine
                            Icon { name: "waveform"; anchors.centerIn: parent; width: 20; height: 20 }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true; spacing: 3
                            RowLayout {
                                spacing: 10
                                UiText { text: "使用我的音色完成语音题"; font.pixelSize: 15; font.weight: Font.DemiBold }
                                Rectangle {
                                    implicitWidth: badgeTxt.implicitWidth + 14; implicitHeight: 22; radius: 11
                                    color: vs.readyToUse ? Theme.successSurface
                                         : vs.hasProfile ? Theme.warningSurface : Theme.raised
                                    border.width: 1
                                    border.color: vs.readyToUse ? Theme.successLine
                                                : vs.hasProfile ? Theme.warningLine : Theme.line
                                    UiText {
                                        id: badgeTxt; anchors.centerIn: parent
                                        text: vs.statusBadge || "待录入音色"
                                        font.pixelSize: 11; font.weight: Font.Medium
                                        color: vs.readyToUse ? Theme.successInk
                                             : vs.hasProfile ? Theme.requiredInk : Theme.muted
                                    }
                                }
                            }
                            UiText {
                                text: "通过本地 OpenVoice V2 神经音色迁移 + 广播级母带润色，将流利英语基音转为你的真实嗓音，零干涩卡顿。"
                                color: Theme.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true
                            }
                        }

                        ToggleSwitch {
                            objectName: "voiceCloneToggle"
                            checked: !!vs.enabled
                            enabled: !vs.recording && !vs.busy
                            onToggled: function(val) { voiceStudio.updateSetting("voice_clone_enabled", val) }
                        }
                    }

                    // Local Model Status / Download Bar
                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: modelRow.implicitHeight + 20
                        radius: 8
                        color: Theme.canvas
                        border.width: 1
                        border.color: Theme.subtleLine

                        ColumnLayout {
                            id: modelRow
                            anchors.fill: parent; anchors.margins: 10
                            spacing: 8

                            RowLayout {
                                Layout.fillWidth: true; spacing: 10
                                Rectangle {
                                    width: 7; height: 7; radius: 4
                                    color: vs.modelReady ? Theme.success : vs.downloading ? Theme.accent : Theme.warning
                                }
                                UiText {
                                    text: vs.downloading
                                        ? (vs.downloadStatus || "正在下载本地音色模型…")
                                        : vs.modelReady
                                        ? ("本地音色引擎已就绪 · OpenVoice V2 (" + vs.modelSizeMb + " MB · 存储于 D 盘)")
                                        : "尚未下载本地音色模型（约 125 MB，免费离线运行，存储于 D 盘项目目录）"
                                    font.pixelSize: 12
                                    color: vs.modelReady ? Theme.secondary : Theme.ink
                                    Layout.fillWidth: true
                                }
                                ActionButton {
                                    visible: !vs.modelReady && !vs.downloading
                                    text: "下载本地模型"
                                    iconName: "download"
                                    primary: true
                                    implicitHeight: 30
                                    onClicked: voiceStudio.downloadModel()
                                }
                                UiText {
                                    visible: vs.downloading
                                    text: Math.round((vs.downloadProgress || 0) * 100) + "%"
                                    mono: true; font.features: { "tnum": 1 }
                                    font.pixelSize: 12; color: Theme.accent
                                }
                            }

                            ProgressTrack {
                                visible: vs.downloading
                                Layout.fillWidth: true
                                implicitHeight: 6
                                value: vs.downloadProgress || 0
                            }
                        }
                    }
                }
            }

            // Inline feedback banner
            Rectangle {
                visible: (vs.notice || "") !== ""
                Layout.fillWidth: true
                implicitHeight: vsNoticeText.implicitHeight + 20
                radius: 8
                color: vs.noticeOk ? Theme.successSurface : Theme.errorSurface
                border.width: 1
                border.color: vs.noticeOk ? Theme.successLine : Theme.errorLine
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 14; anchors.rightMargin: 10; spacing: 10
                    UiText {
                        id: vsNoticeText
                        text: vs.notice || ""
                        color: vs.noticeOk ? Theme.successInk : Theme.error
                        font.pixelSize: 13
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                    ActionButton {
                        iconName: "close"; quiet: true
                        implicitHeight: 28; leftPadding: 6; rightPadding: 6
                        onClicked: voiceStudio.dismissNotice()
                    }
                }
            }

            // 2. Two-Column Main Workbench
            GridLayout {
                Layout.fillWidth: true
                columns: studio.width < 880 ? 1 : 2
                columnSpacing: 14
                rowSpacing: 14

                // LEFT COLUMN: Sample Capture & Profile Telemetry
                Rectangle {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    implicitHeight: captureCol.implicitHeight + 36
                    radius: 10
                    color: Theme.surface
                    border.width: 1
                    border.color: vs.recording ? Theme.recordRed : Theme.line
                    Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

                    ColumnLayout {
                        id: captureCol
                        anchors.fill: parent
                        anchors.margins: 18
                        spacing: 14

                        RowLayout {
                            Layout.fillWidth: true
                            ColumnLayout {
                                Layout.fillWidth: true; spacing: 2
                                UiText { text: "第一步 · 录入我的音色样本"; font.pixelSize: 15; font.weight: Font.DemiBold }
                                UiText { text: "朗读 8–18 秒清晰人声，或直接导入手机/电脑录好的语音文件"; color: Theme.muted; font.pixelSize: 12 }
                            }
                        }

                        // Microphone Selector Row
                        RowLayout {
                            Layout.fillWidth: true; spacing: 8
                            UiText { text: "麦克风"; color: Theme.secondary; font.pixelSize: 12 }
                            ComboBox {
                                id: micCombo
                                Layout.fillWidth: true
                                implicitHeight: 34
                                enabled: !vs.recording && !vs.busy
                                model: voiceStudio.microphones
                                textRole: "name"
                                currentIndex: {
                                    const list = voiceStudio.microphones || []
                                    for (let i = 0; i < list.length; ++i) {
                                        if (list[i].index === vs.selectedMic) return i
                                    }
                                    return list.length > 0 ? 0 : -1
                                }
                                onActivated: function(idx) {
                                    const list = voiceStudio.microphones || []
                                    if (idx >= 0 && idx < list.length)
                                        voiceStudio.selectMicrophone(list[idx].index)
                                }
                                background: Rectangle {
                                    radius: 7; color: Theme.canvas
                                    border.width: 1; border.color: micCombo.activeFocus ? Theme.accent : Theme.line
                                }
                                contentItem: UiText {
                                    leftPadding: 10; rightPadding: 24
                                    text: micCombo.displayText || "未检测到麦克风（可直接导入本地音频）"
                                    color: micCombo.displayText ? Theme.ink : Theme.muted
                                    font.pixelSize: 12
                                }
                            }
                            ActionButton {
                                iconName: "refresh"; quiet: true
                                implicitHeight: 34; leftPadding: 9; rightPadding: 9
                                hint: "重新扫描麦克风设备"
                                enabled: !vs.recording && !vs.busy
                                onClicked: voiceStudio.refreshMicrophones()
                            }
                        }

                        // Live 28-bar Waveform Meter Stage
                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: 96
                            radius: 8
                            color: vs.recording ? Theme.recordSurface : Theme.canvas
                            border.width: 1
                            border.color: vs.recording ? Theme.errorLine : Theme.subtleLine
                            Behavior on color { ColorAnimation { duration: Theme.durFast } }

                            ColumnLayout {
                                anchors.fill: parent; anchors.margins: 12; spacing: 8

                                RowLayout {
                                    Layout.fillWidth: true
                                    Rectangle {
                                        width: 8; height: 8; radius: 4
                                        color: vs.recording ? Theme.recordRed : vs.hasProfile ? Theme.success : Theme.muted
                                        SequentialAnimation on opacity {
                                            running: !!vs.recording
                                            loops: Animation.Infinite
                                            NumberAnimation { to: 0.25; duration: 480; easing.type: Easing.InOutSine }
                                            NumberAnimation { to: 1.0; duration: 480; easing.type: Easing.InOutSine }
                                        }
                                        onVisibleChanged: opacity = 1.0
                                    }
                                    UiText {
                                        text: vs.recording ? "正在收音 · 请保持自然语速朗读下方参考稿"
                                            : vs.busyTask === "processing" ? "正在降噪清洗并提取声纹特征…"
                                            : vs.hasProfile ? ("已保存：" + vs.profileName + "（" + vs.profileUpdatedAt + "）")
                                            : "等待录音或导入声音样本"
                                        font.pixelSize: 12
                                        color: vs.recording ? Theme.ink : Theme.secondary
                                        Layout.fillWidth: true
                                    }
                                    UiText {
                                        visible: !!vs.recording
                                        text: studio.formatTimer(vs.recordSeconds) + " / 00:45"
                                        mono: true; font.features: { "tnum": 1 }
                                        font.pixelSize: 12; font.weight: Font.DemiBold
                                        color: Theme.error
                                    }
                                }

                                // 28-bar centered symmetrical visualizer
                                Item {
                                    id: waveStage
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    Layout.minimumHeight: 42
                                    Row {
                                        anchors.fill: parent
                                        spacing: 4
                                        Repeater {
                                            model: voiceStudio.waveform
                                            delegate: Item {
                                                required property real modelData
                                                readonly property int barCount: Math.max(1, (voiceStudio.waveform || []).length)
                                                width: Math.max(4, Math.floor((waveStage.width - (barCount - 1) * 4) / barCount))
                                                height: waveStage.height
                                                Rectangle {
                                                    anchors.centerIn: parent
                                                    width: Math.min(8, parent.width)
                                                    height: Math.max(4, Math.round(waveStage.height * Math.min(1.0, Math.max(0.10, modelData))))
                                                    radius: Math.max(2, width / 2)
                                                    color: vs.recording ? Theme.recordRed
                                                         : vs.hasProfile ? Theme.accent : Theme.line
                                                    opacity: vs.recording ? 0.92 : vs.hasProfile ? 0.72 : 0.38
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        // Primary Capture Action Buttons
                        RowLayout {
                            Layout.fillWidth: true; spacing: 10
                            ActionButton {
                                objectName: "recordVoiceBtn"
                                Layout.fillWidth: true
                                implicitHeight: 40
                                text: vs.recording ? "停止录音并提取声纹" : "开始录制我的声音"
                                iconName: vs.recording ? "stop" : "mic"
                                primary: !vs.recording
                                destructive: !!vs.recording
                                enabled: !vs.busy && (vs.recording || (voiceStudio.microphones || []).length > 0)
                                onClicked: vs.recording ? voiceStudio.stopRecording() : voiceStudio.startRecording()
                            }
                            ActionButton {
                                objectName: "importVoiceBtn"
                                implicitHeight: 40
                                text: "导入音频"
                                iconName: "upload"
                                hint: "导入 WAV / MP3 / M4A / FLAC / OGG 清晰人声（推荐 6–30 秒）"
                                enabled: !vs.recording && !vs.busy
                                onClicked: voiceStudio.importAudioFile()
                            }
                        }

                        // Guided Reading Scripts
                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: scriptCol.implicitHeight + 24
                            radius: 8
                            color: Theme.canvas
                            border.width: 1
                            border.color: Theme.subtleLine

                            ColumnLayout {
                                id: scriptCol
                                anchors.fill: parent; anchors.margins: 12; spacing: 8

                                RowLayout {
                                    Layout.fillWidth: true; spacing: 6
                                    UiText { text: "参考朗读稿"; font.pixelSize: 12; color: Theme.muted; Layout.fillWidth: true }
                                    Repeater {
                                        model: ["中英自然", "校园口语", "中文样本"]
                                        delegate: SegButton {
                                            required property int index
                                            required property string modelData
                                            text: modelData
                                            implicitHeight: 24
                                            leftPadding: 8; rightPadding: 8
                                            active: studio.activeScriptIndex === index
                                            onClicked: studio.activeScriptIndex = index
                                        }
                                    }
                                }

                                UiText {
                                    readonly property var scriptObj: (voiceStudio.guidedScripts || [])[studio.activeScriptIndex] || ({ text: "", hint: "" })
                                    text: scriptObj.text
                                    color: Theme.ink
                                    font.pixelSize: 13
                                    lineHeight: 1.55
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }

                                UiText {
                                    readonly property var scriptObj: (voiceStudio.guidedScripts || [])[studio.activeScriptIndex] || ({ text: "", hint: "" })
                                    text: scriptObj.hint
                                    color: Theme.muted
                                    font.pixelSize: 11
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }
                            }
                        }

                        // Saved Voice Profile Diagnostics Card
                        Rectangle {
                            visible: !!vs.hasProfile
                            Layout.fillWidth: true
                            implicitHeight: diagCol.implicitHeight + 24
                            radius: 8
                            color: Theme.raised
                            border.width: 1
                            border.color: Theme.line

                            ColumnLayout {
                                id: diagCol
                                anchors.fill: parent; anchors.margins: 12; spacing: 10

                                RowLayout {
                                    Layout.fillWidth: true
                                    UiText { text: "当前声纹体检报告"; font.pixelSize: 12; font.weight: Font.DemiBold; Layout.fillWidth: true }
                                    ActionButton {
                                        text: vs.playing ? "停止播放" : "听原声"
                                        iconName: vs.playing ? "stop" : "volume"
                                        quiet: true; implicitHeight: 28
                                        enabled: !vs.recording
                                        onClicked: voiceStudio.playOriginalSample()
                                    }
                                    ActionButton {
                                        text: "清除"
                                        iconName: "trash"
                                        quiet: true; destructive: true; implicitHeight: 28
                                        enabled: !vs.recording && !vs.busy
                                        onClicked: voiceStudio.deleteProfile()
                                    }
                                }

                                RowLayout {
                                    Layout.fillWidth: true; spacing: 8
                                    Repeater {
                                        model: [
                                            { label: "有效人声", val: (vs.profileActiveDuration || 0).toFixed(1) + " s", tone: Theme.ink },
                                            { label: "基频音域", val: Math.round(vs.profileF0 || 0) + " Hz", tone: Theme.accent },
                                            { label: "清晰度", val: (vs.profileQualityLabel || "-") + " · " + (vs.profileQualityScore || 0), tone: (vs.profileQualityScore || 0) >= 68 ? Theme.success : Theme.warning },
                                            { label: "信噪比", val: (vs.profileSnrDb || 0).toFixed(0) + " dB", tone: Theme.secondary }
                                        ]
                                        delegate: Rectangle {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            implicitHeight: 46
                                            radius: 6
                                            color: Theme.canvas
                                            border.width: 1; border.color: Theme.subtleLine
                                            ColumnLayout {
                                                anchors.fill: parent; anchors.margins: 6; spacing: 2
                                                UiText { text: modelData.label; font.pixelSize: 11; color: Theme.muted; Layout.fillWidth: true }
                                                UiText {
                                                    text: modelData.val; mono: true; font.features: { "tnum": 1 }
                                                    font.pixelSize: 12; font.weight: Font.DemiBold; color: modelData.tone
                                                    Layout.fillWidth: true
                                                }
                                            }
                                        }
                                    }
                                }

                                UiText {
                                    visible: (vs.profileWarning || "") !== ""
                                    text: "建议：" + vs.profileWarning
                                    color: Theme.warning; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true
                                }
                            }
                        }
                    }
                }

                // RIGHT COLUMN: Live Audition + Fluency & Timbre Tuning
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    spacing: 14

                    // Audition Card
                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: auditionCol.implicitHeight + 36
                        radius: 10
                        color: Theme.surface
                        border.width: 1
                        border.color: Theme.line

                        ColumnLayout {
                            id: auditionCol
                            anchors.fill: parent; anchors.margins: 18; spacing: 12

                            RowLayout {
                                Layout.fillWidth: true
                                ColumnLayout {
                                    Layout.fillWidth: true; spacing: 2
                                    UiText { text: "第二步 · 生成虚拟语音试听"; font.pixelSize: 15; font.weight: Font.DemiBold }
                                    UiText { text: "输入任意英文句子，实时检验模拟音色的流畅度与自然度"; color: Theme.muted; font.pixelSize: 12 }
                                }
                                UiText {
                                    visible: (vs.previewLatency || 0) > 0
                                    text: "耗时 " + (vs.previewLatency || 0).toFixed(1) + " s"
                                    mono: true; font.features: { "tnum": 1 }
                                    font.pixelSize: 12; color: Theme.success
                                }
                            }

                            ScrollView {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 88
                                clip: true
                                background: Rectangle {
                                    radius: 8; color: Theme.canvas
                                    border.width: 1
                                    border.color: previewInput.activeFocus ? Theme.accent : Theme.line
                                }
                                TextArea {
                                    id: previewInput
                                    objectName: "voicePreviewInput"
                                    text: vs.defaultPreviewText || ""
                                    placeholderText: "输入任意英文口语内容用于试听…"
                                    placeholderTextColor: Theme.muted
                                    color: Theme.ink; selectionColor: Theme.accentFill; selectedTextColor: "#ffffff"
                                    font.family: Theme.fontFamily; font.pixelSize: 13
                                    wrapMode: TextEdit.Wrap
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true; spacing: 10
                                ActionButton {
                                    objectName: "synthPreviewBtn"
                                    Layout.fillWidth: true
                                    implicitHeight: 38
                                    text: vs.busyTask === "synthesizing" ? "正在合成我的音色…" : "用我的音色生成试听"
                                    iconName: "play"
                                    primary: true
                                    enabled: !!vs.hasProfile && !vs.recording && !vs.busy && (vs.mode === "cloud" || !!vs.modelReady)
                                    onClicked: voiceStudio.synthesizeAndPlay(previewInput.text)
                                }
                                ActionButton {
                                    visible: !!vs.previewReady
                                    implicitHeight: 38
                                    text: vs.playing ? "停止" : "重播"
                                    iconName: vs.playing ? "stop" : "volume"
                                    enabled: !vs.recording && !vs.busy
                                    onClicked: voiceStudio.replayLastPreview()
                                }
                            }
                        }
                    }

                    // Fluency & Naturalness Tuning Card
                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: tuningCol.implicitHeight + 36
                        radius: 10
                        color: Theme.surface
                        border.width: 1
                        border.color: Theme.line

                        ColumnLayout {
                            id: tuningCol
                            anchors.fill: parent; anchors.margins: 18; spacing: 14

                            ColumnLayout {
                                Layout.fillWidth: true; spacing: 2
                                UiText { text: "自然度与答题范围调优"; font.pixelSize: 15; font.weight: Font.DemiBold }
                                UiText { text: "所有调节自动保存并实时生效，无需重启程序"; color: Theme.muted; font.pixelSize: 12 }
                            }

                            Rectangle { Layout.fillWidth: true; height: 1; color: Theme.subtleLine }

                            // Tau slider
                            ColumnLayout {
                                Layout.fillWidth: true; spacing: 6
                                RowLayout {
                                    Layout.fillWidth: true
                                    UiText { text: "音色还原细腻度（tau）"; color: Theme.secondary; font.pixelSize: 13; Layout.fillWidth: true }
                                    UiText {
                                        text: Number(tauSlider.value).toFixed(2)
                                        mono: true; font.features: { "tnum": 1 }
                                        font.pixelSize: 13; font.weight: Font.DemiBold; color: Theme.accent
                                    }
                                }
                                Slider {
                                    id: tauSlider
                                    Layout.fillWidth: true
                                    from: 0.15; to: 0.65; stepSize: 0.05
                                    value: Number(vs.tau || 0.35)
                                    enabled: !vs.busy
                                    onMoved: voiceStudio.updateSetting("voice_clone_tau", value)
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    UiText { text: "0.15 更顺滑平稳"; font.pixelSize: 11; color: Theme.muted }
                                    Item { Layout.fillWidth: true }
                                    UiText { text: "0.35 平衡推荐"; font.pixelSize: 11; color: Theme.secondary }
                                    Item { Layout.fillWidth: true }
                                    UiText { text: "0.65 更多人声细节"; font.pixelSize: 11; color: Theme.muted }
                                }
                            }

                            // Base voice register match
                            RowLayout {
                                Layout.fillWidth: true; spacing: 8
                                UiText { text: "基音音域"; color: Theme.secondary; font.pixelSize: 13; Layout.preferredWidth: 72 }
                                Repeater {
                                    model: [
                                        { key: "auto", label: "自动匹配（推荐）" },
                                        { key: "male", label: "男声基音" },
                                        { key: "female", label: "女声基音" }
                                    ]
                                    delegate: SegButton {
                                        required property var modelData
                                        Layout.fillWidth: true
                                        text: modelData.label
                                        active: (vs.baseGender || "auto") === modelData.key
                                        enabled: !vs.busy
                                        onClicked: voiceStudio.updateSetting("voice_clone_base_gender", modelData.key)
                                    }
                                }
                            }

                            // Speech rate
                            RowLayout {
                                Layout.fillWidth: true; spacing: 8
                                UiText { text: "自然语速"; color: Theme.secondary; font.pixelSize: 13; Layout.preferredWidth: 72 }
                                Repeater {
                                    model: [
                                        { key: "-10%", label: "稍慢 -10%" },
                                        { key: "+0%", label: "标准自然" },
                                        { key: "+8%", label: "轻快 +8%" }
                                    ]
                                    delegate: SegButton {
                                        required property var modelData
                                        Layout.fillWidth: true
                                        text: modelData.label
                                        active: (vs.speechRate || "+0%") === modelData.key
                                        enabled: !vs.busy
                                        onClicked: voiceStudio.updateSetting("voice_clone_rate", modelData.key)
                                    }
                                }
                            }

                            // Scope checkbox
                            UiCheck {
                                Layout.fillWidth: true
                                text: "跟读题 / 词汇朗读 / 角色扮演原声也转为我的音色"
                                hint: "开启后保留教材原声的纯正节奏与停顿，但音色转换为你自己的声音；关闭则仅对自由口语/段落朗读题启用音色克隆。"
                                checked: !!vs.convertReference
                                enabled: !vs.busy
                                onClicked: voiceStudio.updateSetting("voice_clone_convert_reference", checked)
                            }

                            Rectangle { Layout.fillWidth: true; height: 1; color: Theme.subtleLine }

                            // Collapsible Advanced / Cloud Mode
                            RowLayout {
                                Layout.fillWidth: true
                                UiText {
                                    text: "进阶：运行模式（当前：" + (vs.mode === "cloud" ? "云端克隆 API" : "本地 OpenVoice V2 免费离线") + "）"
                                    font.pixelSize: 12; color: Theme.muted; Layout.fillWidth: true
                                }
                                ActionButton {
                                    text: studio.cloudExpanded ? "收起" : "切换引擎"
                                    quiet: true; implicitHeight: 28
                                    onClicked: studio.cloudExpanded = !studio.cloudExpanded
                                }
                            }

                            ColumnLayout {
                                visible: studio.cloudExpanded
                                Layout.fillWidth: true
                                spacing: 10

                                RowLayout {
                                    Layout.fillWidth: true; spacing: 8
                                    SegButton {
                                        Layout.fillWidth: true; text: "本地 OpenVoice V2（推荐 · 免费离线）"
                                        active: (vs.mode || "local") === "local"
                                        onClicked: voiceStudio.updateSetting("voice_clone_mode", "local")
                                    }
                                    SegButton {
                                        Layout.fillWidth: true; text: "云端零样本 API（可选）"
                                        active: vs.mode === "cloud"
                                        onClicked: voiceStudio.updateSetting("voice_clone_mode", "cloud")
                                    }
                                }

                                ColumnLayout {
                                    visible: vs.mode === "cloud"
                                    Layout.fillWidth: true; spacing: 8
                                    TextField {
                                        id: cloudUrlInput
                                        Layout.fillWidth: true; implicitHeight: 34
                                        text: vs.apiUrl || ""
                                        placeholderText: "云端克隆接口地址（如 https://api.siliconflow.cn/v1/audio/speech）"
                                        placeholderTextColor: Theme.muted; color: Theme.ink
                                        font.family: Theme.monoFamily; font.pixelSize: 12
                                        leftPadding: 10; rightPadding: 10
                                        onEditingFinished: voiceStudio.updateSetting("voice_clone_api_url", text)
                                        background: Rectangle { radius: 6; color: Theme.canvas; border.width: 1; border.color: cloudUrlInput.activeFocus ? Theme.accent : Theme.line }
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true; spacing: 8
                                        TextField {
                                            id: cloudKeyInput
                                            Layout.fillWidth: true; implicitHeight: 34
                                            text: vs.apiKey || ""
                                            echoMode: TextInput.Password
                                            placeholderText: "云端 API Key"
                                            placeholderTextColor: Theme.muted; color: Theme.ink
                                            font.family: Theme.monoFamily; font.pixelSize: 12
                                            leftPadding: 10; rightPadding: 10
                                            onEditingFinished: voiceStudio.updateSetting("voice_clone_api_key", text)
                                            background: Rectangle { radius: 6; color: Theme.canvas; border.width: 1; border.color: cloudKeyInput.activeFocus ? Theme.accent : Theme.line }
                                        }
                                        TextField {
                                            id: cloudModelInput
                                            Layout.preferredWidth: 210; implicitHeight: 34
                                            text: vs.apiModel || "FunAudioLLM/CosyVoice2-0.5B"
                                            placeholderText: "模型名"
                                            placeholderTextColor: Theme.muted; color: Theme.ink
                                            font.family: Theme.monoFamily; font.pixelSize: 12
                                            leftPadding: 10; rightPadding: 10
                                            onEditingFinished: voiceStudio.updateSetting("voice_clone_api_model", text)
                                            background: Rectangle { radius: 6; color: Theme.canvas; border.width: 1; border.color: cloudModelInput.activeFocus ? Theme.accent : Theme.line }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

            Item { Layout.preferredHeight: 12 }
        }
    }
}
