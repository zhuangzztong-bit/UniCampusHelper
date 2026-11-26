# UniCampusHelper

> **v4.0.2** · 面向大学生的外语在线学习与练习辅助桌面工作台（支持新版 `ipublish / course-v3` 与旧版 `ucontent` 双播放器架构）

**Windows 11 或 Windows 10 1809 及以上的 x64 用户**下载并完整解压 `UniCampusHelper-Windows-x64.zip`，双击 `启动-UniCampusHelper.cmd`。包内包含 Python 和基础依赖。[下载 Windows 免安装包](https://github.com/zhuangzztong-bit/UniCampusHelper/releases/latest/download/UniCampusHelper-Windows-x64.zip)

- **项目主页**：[https://github.com/zhuangzztong-bit/UniCampusHelper](https://github.com/zhuangzztong-bit/UniCampusHelper)
- **桌面界面**：PySide6 / Qt Quick 纸墨浅色系学习工作台（护眼低疲劳、任务状态一目了然）
- **核心能力**：课程目录与平台学习记录精准同步、音视频自愈播放与断点续播、本地 Whisper 听力转写、Edge-TTS / 本地 OpenVoice V2 音色克隆、多题型自动作答与严格词库防错校验

---

## 核心特性

### 1. 平台真实记录驱动（拒绝“假完成”）
- 自动读取 U 校园课程目录与子题组级学习记录，精确识别每个任务的计分标记、已有分数与缺失子项（阅读/视频/作答）。
- 仅在平台已真实登记完播记录时跳过音视频，避免本地缓存失效导致整单元重播。
- 区分页面历史「重新作答」按钮与本次提交回执，未入库计分题自动重开并复核。

### 2. 双播放器全题型稳定适配
- **新版 `ipublish (course-v3)`**：
  - 支持单选、多选、判断、下拉选词、严格词库选词填空（内置词形/唯一剩余词三级对齐 + 带词库 AI 纠错）、连线匹配、简答、多媒体附件上传（自动生成音频/文档并在耗时合成后实时重定位上传控件防 `stale`）、长写作与单元测试。
  - 内置可滚动祖先容器驱动 + 离场重进 + 自动刷新唤醒机制，解决懒加载题组卡在空占位态的问题。
  - 音视频起播同步播放器 UI 控件防定时拉回暂停，内置动态顺延超时窗与元数据懒加载唤醒。
- **旧版 `ucontent.unipus.cn`**：
  - 支持 AntD Portal 可见下拉过滤与回读校验、多选题内层复选框真实状态校验、听力/视频自动转写与上下文隔离。
  - 支持「返回修改」自动重做：重做前自动停并发预取、清空错答对话历史并强制禁止复读已失败组合。

### 3. 纸墨浅色系学习工作台 UI
- 专为大学生长时间学习场景设计的浅色系界面（云纸浅底 `#f5f7fb` + 洁净白卡片 + 深墨蓝字 `#182230` + 学研蓝主色 `#2563eb`），搭配课程任务、我的音色、运行日志、系统配置四大工作区。

---

## 快速开始

### 免安装包首次使用

1. 使用 Windows 的「全部解压缩」，把整个文件夹放到可写的位置。保留包内的 `.runtime` 文件夹。
2. 确认电脑装有 Microsoft Edge 或 Google Chrome，双击 `启动-UniCampusHelper.cmd`。第一次连接课程浏览器时，程序可能联网下载匹配的浏览器驱动。
3. 在初始配置填写自己的 U 校园账号和模型服务的 API 地址、API Key、模型名称。点击「测试连接」，通过后保存。需要更换浏览器或模型时，可单独双击 `配置-UniCampusHelper.cmd`。

例如使用 DeepSeek 官方接口时，可填写 `https://api.deepseek.com` 和服务商控制台显示的模型名 `deepseek-flash`。第三方网关按该网关公布的地址和模型名配置。

### 从源码安装

通过仓库的 **Code → Download ZIP** 下载源码时，先完整解压，再双击 `安装-UniCampusHelper.cmd`。安装入口使用电脑上已有的 64 位 Python 3.11 或更新版本创建项目内的 `.venv`，推荐 3.12 或 3.13。失败时可查看 `logs/install.log`，并重新运行安装入口。[官方下载 Python](https://www.python.org/downloads/windows/)

### 听力与语音

需要 Whisper 听力转写、录音或本地音色时，在装好 64 位 Python 3.12 或 3.13 后双击 `安装语音支持.cmd`。安装会下载语音依赖，Whisper 模型在首次转写时下载。使用「我的音色」时，可在界面下载 OpenVoice V2 模型。

听力转写还需要 FFmpeg。下载 Windows 构建后，可将 `ffmpeg.exe`、`ffprobe.exe` 放进程序目录的 `tools/ffmpeg/bin`，或使用电脑上已经加入 `PATH` 的 FFmpeg。[FFmpeg 下载页](https://ffmpeg.org/download.html#build-windows)

### 升级与排错

- 升级前关闭旧窗口，再把新版解压到新目录。有需要时复制自己的 `config.json` 与 `voice_profiles` 到新目录。
- 启动失败时看弹窗及 `logs/startup.log`。若驱动下载失败，可检查浏览器是否更新完成以及网络能否访问对应浏览器的驱动服务，然后启动重试。
- 启动后可在「运行日志」复制本次运行日志。平台确认数来自课程记录，停止后会继续核对已开始任务的记录。

---

## 项目架构

```text
UniCampusHelper/
├── UnipusAI_Helper.py              # 核心引擎（双播放器解析、作答、音视频播放与重做容错）
├── 启动-UniCampusHelper.cmd         # Windows 一键启动脚本
├── 安装-UniCampusHelper.cmd         # Windows 源码安装入口
├── 配置-UniCampusHelper.cmd         # 独立打开设置
├── 安装语音支持.cmd                 # 按需安装语音依赖
├── config.example.json             # 脱敏配置模板（请勿提交个人 config.json）
├── requirements-core.txt           # 基础工作台依赖
├── requirements.txt                # 包含语音功能的依赖
├── scripts/
│   ├── launch_workbench.py         # Windows 启动诊断入口
│   ├── build_windows_portable.py    # 从提交构建 Windows x64 免安装包
│   └── verify_release.py           # 发布前编译、QML 与核心回归检查
└── unipus_helper/
    ├── meta.py                     # 统一元数据（应用名、版本号、GitHub 仓库地址）
    ├── native_ui.py                # PySide6 / QML 桌面窗口宿主
    ├── workbench_controller.py     # 工作台状态机与平台记录同步
    ├── course_catalog.py           # 课程目录与学习记录抓取匹配
    ├── task_grading.py             # 计分与达标判定规则
    ├── audio_recognizer.py         # 本地 Whisper 音视频转写
    ├── upload_builder.py           # 多媒体附件（MP3/DOCX/PDF/PNG/MP4）生成
    ├── voice_cloner.py             # 本地 OpenVoice V2 音色克隆
    ├── browser.py                  # Chrome / Edge 驱动与防节流参数
    ├── environment_checker.py      # 运行环境与 FFmpeg 检测
    └── qml/                        # 纸墨浅色系 QML 界面组件与图标
```

---

## 安全与开源说明

- `config.json`、`config.backup-*.json`、`logs/`、`voice_profiles/` 均已写入 `.gitignore`，发布或推送代码前请再次确认未包含个人账号、密码、API Key 或录音样本。
- 日志会隐藏已知配置凭据和常见认证参数。`config.json` 含明文凭据，请保存在本机。
- 本项目仅供学习交流与前端自动化技术研究使用，请遵守所在院校及平台的相关规定。
