# UniCampusHelper

> **v4.0.0** · 面向大学生的外语在线学习与练习辅助桌面工作台（支持新版 `ipublish / course-v3` 与旧版 `ucontent` 双播放器架构）

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

### 环境要求
- Windows 10 / 11（64 位）
- Python 3.11+（推荐 3.13）
- Chrome 或 Edge 浏览器
- FFmpeg（程序启动时自动检测，缺失可按提示安装）

### 安装与运行
```powershell
git clone https://github.com/zhuangzztong-bit/UniCampusHelper.git
cd UniCampusHelper

python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 复制示例配置并填入你的账号与大模型 API Key（config.json 已被 .gitignore 忽略）
Copy-Item config.example.json config.json

# 启动桌面工作台
.\启动-UniCampusHelper.cmd
```

---

## 项目架构

```text
UniCampusHelper/
├── UnipusAI_Helper.py              # 核心引擎（双播放器解析、作答、音视频播放与重做容错）
├── 启动-UniCampusHelper.cmd         # Windows 一键启动脚本
├── config.example.json             # 脱敏配置模板（请勿提交个人 config.json）
├── requirements.txt                # Python 依赖清单
├── scripts/
│   └── verify_release.py           # 发布前编译、轻色 QML 与核心防回归校验脚本
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
- 本项目仅供学习交流与前端自动化技术研究使用，请遵守所在院校及平台的相关规定。
