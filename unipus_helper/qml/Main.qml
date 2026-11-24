import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Precision Workbench redesign guided by frontend-design-taste (Linear / Mercury paradigm).
ApplicationWindow {
    id: window
    objectName: "workbenchWindow"
    minimumWidth: 980; minimumHeight: 660
    width: Math.max(minimumWidth, Math.min(1320, Screen.desktopAvailableWidth - 56))
    height: Math.max(minimumHeight, Math.min(860, Screen.desktopAvailableHeight - 56))
    visible: true
    title: (window.appState.appName || "UniCampusHelper")
    color: Theme.canvas
    palette.window: Theme.surface
    palette.windowText: Theme.ink
    palette.base: Theme.canvas
    palette.text: Theme.ink
    palette.button: Theme.raised
    palette.buttonText: Theme.ink
    palette.highlight: Theme.accentFill
    palette.highlightedText: "white"

    // 0: 课程任务 | 1: 我的音色 | 2: 运行日志 | 3: 系统配置
    property int currentPage: 0
    property string logLevelFilter: "all"
    readonly property int pageMargin: width < 1120 ? 20 : 28
    readonly property bool compactHeight: height < 730
    readonly property var appState: workbench.state
    readonly property var summary: workbench.summary
    readonly property var voiceState: voiceStudio.state
    readonly property bool busy: !!appState.busy

    property bool returnToTasksWhenIdle: false

    Component.onCompleted: {
        if (typeof uiMonoFont !== "undefined" && uiMonoFont)
            Theme.monoFamily = uiMonoFont
    }

    function finishProcessingNavigation() {
        if (!returnToTasksWhenIdle || !appState || appState.busy) return
        returnToTasksWhenIdle = false
        // Leave stopped or failed runs available for inspection in the log.
        if (appState.status === "就绪" && !appState.error) currentPage = 0
    }
    onAppStateChanged: finishProcessingNavigation()

    function startProcessing(action) {
        if (!workbench.start(action)) return
        returnToTasksWhenIdle = true
        currentPage = 2
        finishProcessingNavigation()
    }

    function resetTaskPosition() { if (taskList) { taskList.currentIndex = -1; taskList.positionViewAtBeginning() } }
    function focusFirstTask() {
        if (taskList.count === 0) return
        taskList.currentIndex = 0
        taskList.positionViewAtBeginning()
        Qt.callLater(function() {
            const first = taskList.itemAtIndex(0)
            if (first) first.focusTask()
        })
    }
    function clearTaskFilters() {
        searchInput.clear()
        workbench.resetFilters()
        resetTaskPosition()
        searchInput.forceActiveFocus()
    }

    Shortcut {
        sequences: [StandardKey.Find]
        onActivated: { window.currentPage = 0; searchInput.forceActiveFocus(); searchInput.selectAll() }
    }
    Shortcut {
        sequences: [StandardKey.Save]
        onActivated: { if (window.currentPage === 3) embeddedConfig.saveSettings() }
    }
    Shortcut { sequence: StandardKey.Quit; onActivated: window.close() }

    RowLayout {
        anchors.fill: parent; spacing: 0

        // LEFT SIDEBAR
        Rectangle {
            Layout.preferredWidth: window.width < 1120 ? 228 : 250
            Layout.minimumWidth: Layout.preferredWidth; Layout.maximumWidth: Layout.preferredWidth
            Layout.fillHeight: true; Layout.minimumHeight: 0
            color: Theme.sidebar

            Rectangle { anchors.right: parent.right; width: 1; height: parent.height; color: Theme.line }

            ColumnLayout {
                anchors.fill: parent; anchors.margins: 16; spacing: 4

                // Brand Header
                RowLayout {
                    Layout.fillWidth: true
                    Layout.leftMargin: 4
                    Layout.topMargin: window.compactHeight ? 8 : 16
                    Layout.bottomMargin: window.compactHeight ? 18 : 24
                    spacing: 10

                    Rectangle {
                        width: 34; height: 34; radius: 9
                        color: Theme.navigation
                        border.width: 1; border.color: Theme.selectionLine
                        Image {
                            anchors.centerIn: parent
                            source: "icons/app.svg"
                            sourceSize.width: 64; sourceSize.height: 64
                            width: 24; height: 24
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true; spacing: 1
                        UiText {
                            objectName: "projectName"
                            text: window.appState.appName || "UniCampusHelper"
                            font.pixelSize: window.width < 1120 ? 15 : 16
                            font.weight: Font.DemiBold
                            Layout.fillWidth: true
                        }
                        UiText {
                            text: "v" + (window.appState.version || "4.0.0") + " · 学习工作台"
                            mono: true
                            font.pixelSize: 11
                            color: Theme.muted
                        }
                    }
                }

                // Primary Navigation (4 integrated workspaces)
                NavItem {
                    objectName: "tasksPage"
                    Layout.fillWidth: true
                    text: "课程任务"
                    iconName: "list"
                    selected: window.currentPage === 0
                    onClicked: window.currentPage = 0
                }
                NavItem {
                    objectName: "voicePage"
                    Layout.fillWidth: true
                    text: "我的音色"
                    iconName: "waveform"
                    selected: window.currentPage === 1
                    badgeText: voiceState.recording ? "录音中" : voiceState.readyToUse ? "已启用" : ""
                    badgeColor: voiceState.recording ? Theme.recordRed : Theme.success
                    showDot: !voiceState.readyToUse && !!voiceState.hasProfile
                    dotColor: Theme.warning
                    onClicked: window.currentPage = 1
                }
                NavItem {
                    objectName: "logsPage"
                    Layout.fillWidth: true
                    text: "运行日志"
                    iconName: "logs"
                    selected: window.currentPage === 2
                    count: (window.appState.logs || []).length > 0 ? String((window.appState.logs || []).length) : ""
                    onClicked: window.currentPage = 2
                }
                NavItem {
                    objectName: "settingsPage"
                    Layout.fillWidth: true
                    text: "系统配置"
                    iconName: "settings"
                    selected: window.currentPage === 3
                    onClicked: window.currentPage = 3
                }

                Rectangle { Layout.fillWidth: true; height: 1; color: Theme.subtleLine; Layout.topMargin: 12; Layout.bottomMargin: 10 }

                RowLayout {
                    Layout.fillWidth: true; Layout.leftMargin: 8; Layout.rightMargin: 8; Layout.bottomMargin: 6
                    UiText { text: "课程目录"; color: Theme.muted; font.pixelSize: 12; font.weight: Font.Medium; Layout.fillWidth: true }
                    UiText {
                        text: workbench.units.length ? workbench.units.length + " 个单元" : "待扫描"
                        mono: true; font.features: { "tnum": 1 }
                        color: Theme.muted; font.pixelSize: 11
                    }
                }

                NavItem {
                    objectName: "allUnits"
                    Layout.fillWidth: true
                    text: "全部单元"
                    iconName: "layers"
                    selected: window.currentPage === 0 && workbench.activeUnit === "all"
                    count: summary.total > 0 ? String(summary.total) : ""
                    onClicked: { workbench.chooseUnit("all"); window.currentPage = 0; window.resetTaskPosition() }
                }

                ListView {
                    id: unitList; objectName: "unitList"
                    Layout.fillHeight: true; Layout.fillWidth: true; Layout.minimumHeight: 0
                    Layout.topMargin: 6; clip: true; spacing: 4; model: workbench.units
                    delegate: Button {
                        id: unitButton
                        required property var modelData
                        readonly property bool selected: workbench.activeUnit === modelData.name && window.currentPage === 0
                        readonly property int remaining: Math.max(0, modelData.total - modelData.done)
                        objectName: "unit_" + modelData.name
                        width: unitList.width; height: 58
                        leftPadding: 12; rightPadding: 12; hoverEnabled: true
                        Accessible.name: modelData.name + "，已完成 " + modelData.done + " / " + modelData.total
                        onClicked: { workbench.chooseUnit(modelData.name); window.currentPage = 0; window.resetTaskPosition() }
                        HoverHandler { cursorShape: Qt.PointingHandCursor }

                        background: Rectangle {
                            radius: 8
                            color: unitButton.selected ? Theme.navigation : unitButton.hovered ? Theme.hover : "transparent"
                            border.width: unitButton.activeFocus || unitButton.selected ? 1 : 0
                            border.color: unitButton.activeFocus ? Theme.accent : Theme.selectionLine
                            Behavior on color { ColorAnimation { duration: Theme.durFast } }

                            Rectangle {
                                anchors.left: parent.left; anchors.leftMargin: 2; anchors.verticalCenter: parent.verticalCenter
                                width: 3; height: unitButton.selected ? 26 : 0; radius: 2
                                color: Theme.accent
                                Behavior on height { NumberAnimation { duration: Theme.durFast; easing.type: Easing.OutCubic } }
                            }
                        }

                        contentItem: ColumnLayout {
                            spacing: 6
                            RowLayout {
                                Layout.fillWidth: true; spacing: 8
                                UiText {
                                    text: unitButton.modelData.name
                                    font.pixelSize: 13
                                    font.weight: unitButton.selected ? Font.DemiBold : Font.Medium
                                    color: unitButton.selected ? Theme.ink : Theme.secondary
                                    Layout.fillWidth: true
                                }
                                UiText {
                                    text: unitButton.modelData.done + "/" + unitButton.modelData.total
                                    mono: true; font.pixelSize: 12
                                    color: unitButton.remaining === 0 ? Theme.success : Theme.secondary
                                    font.features: { "tnum": 1 }
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true; spacing: 8
                                UiText {
                                    text: unitButton.remaining === 0 ? "全部完成" : unitButton.remaining + " 项待完成"
                                    font.pixelSize: 11
                                    color: unitButton.remaining === 0 ? Theme.success : Theme.muted
                                    Layout.fillWidth: true
                                }
                                ProgressTrack {
                                    Layout.preferredWidth: 68; implicitHeight: 4
                                    value: unitButton.modelData.done / Math.max(1, unitButton.modelData.total)
                                    Accessible.ignored: true
                                }
                            }
                        }
                        ToolTip.visible: hovered && modelData.title !== ""
                        ToolTip.text: modelData.title
                        ToolTip.delay: 650
                    }
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                }

                Rectangle {
                    Layout.fillWidth: true; Layout.preferredHeight: 1
                    Layout.topMargin: 10; Layout.bottomMargin: 8; color: Theme.subtleLine
                }

                ColumnLayout {
                    objectName: "githubInfo"
                    Layout.fillWidth: true; Layout.minimumWidth: 0
                    Layout.leftMargin: 6; Layout.rightMargin: 6; spacing: 4
                    RowLayout {
                        Layout.fillWidth: true; spacing: 6
                        Icon { name: "github"; Layout.preferredWidth: 15; Layout.preferredHeight: 15 }
                        UiText { objectName: "githubLabel"; text: "GitHub 项目主页"; color: Theme.muted; font.pixelSize: 12; Layout.fillWidth: true }
                    }
                    TextEdit {
                        objectName: "githubAddress"
                        Layout.fillWidth: true; Layout.minimumWidth: 0
                        text: window.appState.githubDisplay || "github.com/zhuangzztong-bit/UniCampusHelper"
                        readOnly: true; selectByMouse: true; textFormat: TextEdit.PlainText
                        wrapMode: TextEdit.Wrap
                        font.family: Theme.monoFamily; font.pixelSize: 11; font.weight: Font.Normal
                        font.hintingPreference: Font.PreferFullHinting; renderType: TextEdit.NativeRendering
                        color: Theme.muted; selectedTextColor: "white"; selectionColor: Theme.accentFill
                        Accessible.name: "GitHub 地址：" + text
                    }
                }
            }
        }

        // MAIN CONTENT STAGE
        ColumnLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumWidth: 0; Layout.minimumHeight: 0
            Layout.leftMargin: window.pageMargin; Layout.rightMargin: window.pageMargin; spacing: 0

            // Top Workspace Header
            RowLayout {
                Layout.fillWidth: true; Layout.preferredHeight: window.compactHeight ? 68 : 78
                Layout.topMargin: window.compactHeight ? 14 : 20; Layout.bottomMargin: window.compactHeight ? 12 : 16; spacing: 20

                ColumnLayout {
                    Layout.fillWidth: true; Layout.minimumWidth: 0; spacing: 3
                    UiText {
                        text: window.currentPage === 1 ? "我的音色工作室"
                            : window.currentPage === 2 ? "运行日志"
                            : window.currentPage === 3 ? "系统配置"
                            : workbench.activeUnit === "all" ? "全部课程任务" : workbench.activeUnit
                        font.pixelSize: window.compactHeight ? 24 : 27
                        font.weight: Font.DemiBold
                        Layout.fillWidth: true
                    }
                    UiText {
                        visible: text !== ""
                        text: window.currentPage === 0 ? (summary.unitTitle || "集中管理课程扫描、勾选筛选与自动答题")
                            : window.currentPage === 1 ? "录入个人声音样本，本地神经音色克隆流畅完成口语及配音题"
                            : window.currentPage === 2 ? "实时追踪扫描、语音克隆、答题与服务器进度核验事件"
                            : "管理 U 校园账号、大模型 API、语音及浏览器运行参数"
                        color: Theme.muted; font.pixelSize: 12; Layout.fillWidth: true
                    }
                }

                AccountSummary {
                    objectName: "accountDetails"
                    Layout.fillWidth: false
                    Layout.preferredWidth: window.width < 1120 ? 320 : 386
                    Layout.minimumWidth: 312; Layout.maximumWidth: Layout.preferredWidth
                    account: window.appState.account || ""
                    model: window.appState.model || ""
                    status: window.appState.status || ""
                    apiStatus: window.appState.apiStatus || ""
                    apiOk: !!window.appState.apiOk
                    apiFail: !!window.appState.apiFail
                    busy: window.busy
                    ready: !!window.appState.ready
                    hasError: !!window.appState.error
                    voiceActive: !!voiceState.readyToUse
                    voiceLabel: voiceState.readyToUse ? "我的音色" : voiceState.hasProfile ? "音色未开" : ""
                    onVoiceClicked: window.currentPage = 1
                }
            }

            // PAGE 0: Course Overview Metrics
            CourseOverview {
                objectName: "courseOverview"
                visible: window.currentPage === 0
                Layout.fillWidth: true
                Layout.bottomMargin: window.compactHeight ? 12 : 14
                counts: summary.counts
                compact: window.width < 1120 || window.compactHeight
            }

            // Action Toolbar (Page 0 & Page 2)
            RowLayout {
                visible: window.currentPage === 0 || window.currentPage === 2
                Layout.fillWidth: true
                Layout.bottomMargin: window.compactHeight ? 10 : 14
                spacing: 10

                // Page 0 actions
                ActionButton {
                    objectName: "scanAll"; visible: window.currentPage === 0
                    text: "扫描全部单元"; iconName: "refresh"; implicitHeight: window.compactHeight ? 36 : 38
                    primary: true; leftPadding: 18; rightPadding: 18
                    hint: "读取当前教材全部单元的任务"
                    enabled: window.appState.ready && !window.busy
                    onClicked: workbench.start("scan")
                }
                ActionButton {
                    objectName: "scanUnit"; visible: window.currentPage === 0
                    text: "扫描当前单元"; iconName: "layers"; implicitHeight: window.compactHeight ? 36 : 38
                    hint: "读取课程浏览器当前所在的单元"
                    enabled: window.appState.ready && !window.busy
                    onClicked: workbench.start("scan_unit")
                }
                ActionButton {
                    objectName: "processPage"; visible: window.currentPage === 0
                    text: "处理当前页"; iconName: "page"; implicitHeight: window.compactHeight ? 36 : 38
                    hint: "直接处理课程浏览器中已打开的题目"
                    enabled: window.appState.ready && !window.busy
                    onClicked: quickDialog.open()
                }

                // Page 2 log filter pills
                RowLayout {
                    visible: window.currentPage === 2
                    spacing: 8
                    Repeater {
                        model: [
                            { key: "all", label: "全部日志" },
                            { key: "issue", label: "提醒与错误" },
                            { key: "success", label: "成功节点" }
                        ]
                        delegate: Button {
                            id: logChip
                            required property var modelData
                            readonly property bool selected: window.logLevelFilter === modelData.key
                            implicitHeight: 32; leftPadding: 12; rightPadding: 12; hoverEnabled: true
                            HoverHandler { cursorShape: Qt.PointingHandCursor }
                            onClicked: window.logLevelFilter = modelData.key
                            background: Rectangle {
                                radius: 7
                                color: logChip.selected ? Theme.navigation : logChip.hovered ? Theme.hover : Theme.surface
                                border.width: 1
                                border.color: logChip.selected ? Theme.selectionLine : Theme.line
                                Behavior on color { ColorAnimation { duration: Theme.durFast } }
                            }
                            contentItem: UiText {
                                text: logChip.modelData.label
                                font.pixelSize: 12
                                font.weight: logChip.selected ? Font.DemiBold : Font.Normal
                                color: logChip.selected ? Theme.accentBright : Theme.secondary
                            }
                        }
                    }
                    UiText {
                        text: activity.count + " 条"
                        mono: true; font.features: { "tnum": 1 }
                        font.pixelSize: 12; color: Theme.muted; Layout.leftMargin: 4
                    }
                }

                Item { Layout.fillWidth: true }

                ActionButton {
                    objectName: "restartBrowser"; visible: window.currentPage === 0
                    text: "重启浏览器"; iconName: "refresh"; quiet: true
                    implicitHeight: window.compactHeight ? 36 : 38
                    enabled: !window.busy; onClicked: workbench.restartBrowser()
                }
                ActionButton {
                    objectName: "copyLogs"; visible: window.currentPage === 2
                    enabled: (window.appState.logs || []).length > 0
                    text: "复制全部日志"; iconName: "copy"; implicitHeight: 36
                    onClicked: workbench.copyLogs()
                }
            }

            // Global Error / Notice Strip
            Rectangle {
                visible: !!window.appState.error || workbench.notice !== ""
                Layout.fillWidth: true; implicitHeight: noticeText.implicitHeight + 20; Layout.bottomMargin: 12
                color: window.appState.error ? Theme.errorSurface : Theme.surface
                radius: 8
                border.width: 1
                border.color: window.appState.error ? Theme.errorLine : Theme.line
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 14; anchors.rightMargin: 10; spacing: 12
                    UiText {
                        id: noticeText
                        text: workbench.notice || window.appState.error || ""
                        color: window.appState.error ? Theme.error : Theme.secondary
                        font.pixelSize: 13; wrapMode: Text.Wrap; Layout.fillWidth: true
                    }
                    ActionButton {
                        visible: workbench.notice !== ""
                        iconName: "close"; quiet: true; implicitHeight: 28; leftPadding: 6; rightPadding: 6
                        Accessible.name: "关闭提示"
                        onClicked: workbench.dismissNotice()
                    }
                }
            }

            // PAGE 0: Task Table Surface
            Rectangle {
                objectName: "taskSurface"
                visible: window.currentPage === 0
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 0; Layout.minimumWidth: 0
                color: Theme.surface; radius: 12; border.width: 1; border.color: Theme.line
                clip: true

                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 1; spacing: 0

                    // Filter & Search Bar
                    Item {
                        Layout.fillWidth: true; Layout.preferredHeight: window.compactHeight ? 48 : 54
                        RowLayout {
                            anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 16; spacing: 10
                            RowLayout {
                                spacing: 6
                                Repeater {
                                    model: [
                                        { key: "all", title: "全部" },
                                        { key: "pending", title: "未完成" },
                                        { key: "required", title: "必修" },
                                        { key: "done", title: "已完成" }
                                    ]
                                    Button {
                                        id: filterButton
                                        required property var modelData
                                        readonly property bool selected: workbench.activeFilter === modelData.key
                                        objectName: "filter_" + modelData.key
                                        implicitWidth: filterRow.implicitWidth + 20; implicitHeight: 32; hoverEnabled: true
                                        onClicked: { workbench.chooseFilter(modelData.key); window.resetTaskPosition() }
                                        HoverHandler { cursorShape: Qt.PointingHandCursor }
                                        Accessible.name: modelData.title
                                        background: Rectangle {
                                            radius: 7
                                            color: filterButton.selected ? Theme.navigation : filterButton.hovered ? Theme.hover : "transparent"
                                            border.width: filterButton.selected || filterButton.activeFocus ? 1 : 0
                                            border.color: filterButton.activeFocus ? Theme.accent : Theme.selectionLine
                                            Behavior on color { ColorAnimation { duration: Theme.durFast } }
                                        }
                                        contentItem: RowLayout {
                                            id: filterRow; spacing: 6
                                            UiText {
                                                text: filterButton.modelData.title
                                                font.pixelSize: 13
                                                font.weight: filterButton.selected ? Font.DemiBold : Font.Medium
                                                color: filterButton.modelData.key === "required" ? Theme.requiredInk
                                                     : filterButton.selected ? Theme.ink : Theme.secondary
                                            }
                                            UiText {
                                                text: String(summary.counts[filterButton.modelData.key] || 0)
                                                mono: true; font.features: { "tnum": 1 }
                                                font.pixelSize: 12
                                                color: filterButton.modelData.key === "required" ? Theme.compulsory
                                                     : filterButton.selected ? Theme.accentBright : Theme.muted
                                            }
                                        }
                                    }
                                }
                            }
                            Item { Layout.fillWidth: true }
                            TextField {
                                id: searchInput; objectName: "taskSearch"
                                Layout.preferredWidth: window.width < 1120 ? 168 : 248; Layout.minimumWidth: 130
                                implicitHeight: 32; placeholderText: "搜索任务名称或章节…"; color: Theme.ink; placeholderTextColor: Theme.muted
                                font.family: Theme.fontFamily; font.weight: Font.Normal; font.pixelSize: 13
                                font.hintingPreference: Font.PreferFullHinting; renderType: Text.NativeRendering
                                leftPadding: 32; rightPadding: 28; selectByMouse: true
                                Accessible.name: "搜索任务"; onTextChanged: { workbench.search(text); window.resetTaskPosition() }
                                Keys.onEscapePressed: { if (text !== "") clear(); else window.focusFirstTask() }
                                Keys.onReturnPressed: window.focusFirstTask()
                                Keys.onEnterPressed: window.focusFirstTask()
                                Keys.onDownPressed: window.focusFirstTask()
                                background: Rectangle {
                                    radius: 7; color: Theme.canvas
                                    border.width: 1; border.color: searchInput.activeFocus ? Theme.accent : Theme.line
                                    Behavior on border.color { ColorAnimation { duration: Theme.durFast } }
                                }
                                Icon { x: 9; anchors.verticalCenter: parent.verticalCenter; name: "search"; width: 15; height: 15 }
                                ActionButton {
                                    objectName: "clearSearch"
                                    anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                                    width: 28; height: 28; leftPadding: 6; rightPadding: 6
                                    visible: searchInput.text !== ""; quiet: true; iconName: "close"
                                    Accessible.name: "清空搜索"; hint: "清空搜索（Esc）"
                                    onClicked: { searchInput.clear(); searchInput.forceActiveFocus() }
                                }
                            }
                        }
                    }

                    // Bulk Selection Bar
                    Rectangle {
                        Layout.fillWidth: true; Layout.preferredHeight: 36; color: Theme.raised
                        Rectangle { anchors.top: parent.top; width: parent.width; height: 1; color: Theme.subtleLine }
                        RowLayout {
                            anchors.fill: parent; anchors.leftMargin: 24; anchors.rightMargin: 16; spacing: 12
                            UiCheck {
                                objectName: "selectAllTasks"; text: "全选当前列表 · " + summary.selectableVisible + " 项"
                                hint: "仅选择当前列表中的可选任务，不影响其他单元或被筛选隐藏的选择"
                                checkState: summary.allChecked ? Qt.Checked : summary.someChecked ? Qt.PartiallyChecked : Qt.Unchecked
                                enabled: !window.busy && summary.selectableVisible > 0; onClicked: workbench.toggleVisible()
                            }
                            Item { Layout.fillWidth: true }
                            ActionButton {
                                objectName: "selectPending"; text: "勾选未完成必修"; accented: true; quiet: true; implicitHeight: 30
                                hint: "只调整当前列表，不影响隐藏任务的选择"
                                enabled: !window.busy && summary.selectableVisible > 0
                                onClicked: workbench.selectVisible("pending")
                            }
                            Rectangle { width: 1; height: 14; color: Theme.line }
                            ActionButton {
                                objectName: "clearSelection"; text: "清空全部选择"
                                hint: "清空所有单元的选择，包括筛选隐藏的任务"
                                quiet: true; implicitHeight: 30
                                enabled: !window.busy && summary.selected > 0
                                onClicked: workbench.clearSelection()
                            }
                        }
                    }

                    // Column Headers
                    Rectangle {
                        Layout.fillWidth: true; Layout.preferredHeight: 32; color: Theme.surface
                        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.line }
                        RowLayout {
                            anchors.fill: parent; anchors.leftMargin: 60; anchors.rightMargin: 24; spacing: 16
                            UiText { text: "任务名称"; color: Theme.muted; font.pixelSize: 12; Layout.fillWidth: true }
                            UiText { text: "要求"; color: Theme.muted; font.pixelSize: 12; Layout.preferredWidth: 48; horizontalAlignment: Text.AlignHCenter }
                            UiText { text: "计分"; color: Theme.muted; font.pixelSize: 12; Layout.preferredWidth: 64; horizontalAlignment: Text.AlignHCenter }
                            UiText { text: "分数"; color: Theme.muted; font.pixelSize: 12; Layout.preferredWidth: 68; horizontalAlignment: Text.AlignHCenter }
                            UiText { text: "状态"; color: Theme.muted; font.pixelSize: 12; Layout.preferredWidth: 74; horizontalAlignment: Text.AlignHCenter }
                        }
                    }

                    ListView {
                        id: taskList; objectName: "taskList"
                        Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 0; Layout.bottomMargin: 6
                        clip: true; model: taskModel; currentIndex: -1
                        boundsBehavior: Flickable.StopAtBounds
                        delegate: TaskRow { width: taskList.width; editable: !window.busy }
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        ColumnLayout {
                            anchors.centerIn: parent; width: Math.min(400, parent.width - 64); spacing: 12
                            visible: taskList.count === 0
                            UiText {
                                text: summary.total ? "没有匹配的任务" : window.appState.ready ? "还没有课程任务" : "等待 U 校园登录"
                                font.pixelSize: 17; font.weight: Font.Medium
                                Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter
                            }
                            UiText {
                                text: summary.total ? "试试其他关键词，或清除搜索与筛选"
                                    : window.appState.ready ? "在课程浏览器中打开教材目录后，点击「扫描全部单元」"
                                    : "请在打开的课程浏览器中完成登录"
                                color: Theme.muted; font.pixelSize: 13
                                Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
                            }
                            ActionButton {
                                objectName: "resetTaskFilters"; visible: summary.total > 0
                                Layout.alignment: Qt.AlignHCenter; text: "清除搜索与筛选"
                                hint: "保留当前单元和已选任务"
                                onClicked: window.clearTaskFilters()
                            }
                        }
                    }
                }
            }

            // PAGE 1: Voice Cloning Studio
            VoiceStudioPage {
                visible: window.currentPage === 1
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 0
                Layout.bottomMargin: window.busy ? 0 : 12
            }

            // PAGE 2: Runtime Logs
            Rectangle {
                visible: window.currentPage === 2
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 0
                Layout.bottomMargin: window.busy ? 0 : 16
                color: Theme.surface; radius: 12; border.width: 1; border.color: Theme.line
                clip: true
                LogView {
                    id: activity; objectName: "allActivity"
                    anchors.fill: parent; anchors.margins: 1
                    entries: window.appState.logs || []
                    levelFilter: window.logLevelFilter
                }
                ActionButton {
                    objectName: "latestLogs"
                    visible: !activity.followTail && !activity.atYEnd && activity.count > 0
                    anchors.right: parent.right; anchors.bottom: parent.bottom; anchors.margins: 20
                    text: "回到底部最新"
                    primary: true
                    onClicked: { activity.followTail = true; activity.followLatest() }
                }
            }

            // PAGE 3: Embedded Settings Workspace
            ConfigForm {
                id: embeddedConfig
                visible: window.currentPage === 3
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 0
                Layout.bottomMargin: window.busy ? 0 : 16
            }

            // Bottom Execution Dock (Tasks Page OR anytime a batch run is active)
            Rectangle {
                objectName: "taskActions"
                visible: window.currentPage === 0 || window.busy
                Layout.fillWidth: true; Layout.preferredHeight: window.compactHeight ? 64 : 74
                Layout.topMargin: window.compactHeight ? 10 : 14; Layout.bottomMargin: 16
                radius: 12; color: Theme.surface
                border.width: 1; border.color: window.busy ? Theme.selectionLine : Theme.line

                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 18; anchors.rightMargin: 16; spacing: window.width < 1120 ? 14 : 18

                    Rectangle {
                        visible: window.width >= 1060
                        width: 30; height: 30; radius: 8
                        color: window.busy ? Theme.navigation : summary.selectedVisible > 0 ? Theme.accentFill : Theme.raised
                        border.width: 1; border.color: window.busy ? Theme.selectionLine : Theme.subtleLine
                        Icon {
                            anchors.centerIn: parent
                            name: window.busy ? "play" : "check"
                            width: 16; height: 16; white: true
                            visible: window.busy || summary.selectedVisible > 0
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true; Layout.minimumWidth: 0; spacing: 4
                        UiText {
                            text: window.busy
                                ? (window.appState.progress.total > 0
                                    ? "本轮已处理 " + window.appState.progress.completed + " / " + window.appState.progress.total + " 项"
                                    : window.appState.status)
                                : summary.selectedVisible
                                    ? "已选择 " + summary.selectedVisible + " 项任务" + (voiceState.readyToUse ? " · 语音题将使用我的音色" : "")
                                    : "请在上方列表勾选要处理的任务"
                            font.pixelSize: 15; font.weight: Font.DemiBold; Layout.fillWidth: true
                        }
                        UiText {
                            objectName: "selectionDetails"
                            visible: !window.busy
                            text: summary.selectedHidden > 0 ? "另有 " + summary.selectedHidden + " 项在其他筛选/单元中保留勾选"
                                : summary.selectedUnits > 1 ? "跨越 " + summary.selectedUnits + " 个单元 · 点击整行或按空格切换"
                                : "点击整行勾选 · ↑↓ 移动 · 空格切换 · Ctrl+A 全选当前列表"
                            font.pixelSize: 12; color: summary.selectedHidden > 0 ? Theme.secondary : Theme.muted; Layout.fillWidth: true
                            HoverHandler { id: selectionHint }
                            ToolTip.visible: selectionHint.hovered
                            ToolTip.text: summary.selectedHidden > 0
                                ? "本次仅处理当前列表的勾选任务，隐藏项可切换列表后处理"
                                : "点击整行勾选；↑↓ 移动；空格或回车切换；Ctrl+A 全选当前列表"
                            ToolTip.delay: 650
                        }
                        ProgressTrack {
                            visible: window.busy && window.appState.progress.total > 0
                            Layout.fillWidth: true; Layout.maximumWidth: 260; implicitHeight: 5
                            value: window.appState.progress.completed / Math.max(1, window.appState.progress.total)
                        }
                    }

                    UiCheck {
                        objectName: "excludeUngraded"
                        visible: window.currentPage === 0
                        text: "排除不计分"
                        hint: "开启后取消并禁止选择明确不计分的任务，计分待确认的任务仍可选择"
                        checked: !!window.appState.excludeUngraded
                        enabled: !window.busy
                        onClicked: workbench.excludeUngraded(checked)
                    }

                    Rectangle { visible: window.currentPage === 0; width: 1; height: 22; color: Theme.line }

                    ActionButton {
                        objectName: "startTasks"
                        Layout.preferredWidth: window.width < 1120 ? 134 : 158
                        implicitHeight: window.compactHeight ? 38 : 42
                        text: window.appState.status === "正在停止" ? "正在停止…" : window.busy ? "停止处理" : "开始处理"
                        iconName: window.busy ? "stop" : "play"
                        primary: !window.busy
                        destructive: window.busy
                        enabled: window.appState.status !== "正在停止" && (window.busy || (window.appState.ready && summary.selectedVisible > 0))
                        onClicked: window.busy ? workbench.stop() : window.startProcessing("run")
                    }
                }
            }
        }
    }

    Dialog {
        id: quickDialog; objectName: "quickDialog"; modal: true
        closePolicy: Popup.CloseOnEscape
        onOpened: cancelQuick.forceActiveFocus()
        anchors.centerIn: parent; width: 456; padding: 24
        header: UiText { text: "处理当前页"; font.pixelSize: 19; font.weight: Font.DemiBold; padding: 24; bottomPadding: 8 }
        onAccepted: window.startProcessing("quick")
        background: Rectangle { color: Theme.surface; radius: 14; border.width: 1; border.color: Theme.line }
        ColumnLayout {
            width: parent.width; spacing: 22
            UiText {
                Layout.fillWidth: true
                text: "确认直接处理浏览器当前打开的题目页？" + (voiceState.readyToUse ? "\n（语音录入题将自动使用「我的音色」模拟合成）" : "")
                wrapMode: Text.Wrap; lineHeight: 1.5; color: Theme.secondary
            }
            RowLayout {
                Layout.fillWidth: true; spacing: 10
                Item { Layout.fillWidth: true }
                ActionButton { id: cancelQuick; objectName: "cancelQuick"; text: "取消"; onClicked: quickDialog.reject() }
                ActionButton {
                    objectName: "confirmQuick"; text: "开始处理当前页"; primary: true
                    enabled: window.appState.ready && !window.busy
                    onClicked: quickDialog.accept()
                }
            }
        }
    }
}
