"""Qt Quick workbench. No web server or browser engine is used by this UI."""
from __future__ import annotations

import logging
import json
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QObject, Property,
                            QTimer, Qt, QUrl, Signal, Slot, QtMsgType, qInstallMessageHandler)
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from shiboken6 import delete as delete_qt_object

from .model_api import model_settings_error, probe_model
from .paths import PROJECT_ROOT, QML_DIR
from .task_grading import score_text
from .workbench_controller import WorkbenchController


DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "url": "https://uai.unipus.cn/sso/index.html?service=https%3A%2F%2Fucloud.unipus.cn%2Fhome",
    "token_full": "",
    "api_key": "",
    "base_url": "",
    "model": "",
    "max_tokens": 32768,
    "temperature": 0.3,
    "debug_mode": False,
    # 录音题（Exercise 2 这类主观题）没有原声，需要 TTS 生成语音
    "tts_engine": "auto",
    "tts_voice": "",
    "tts_proxy": "",
    "browser": "edge",
    # 新版视频播放器原生倍速（仅在播放器界面本身开放倍速控件时生效）：1.0 / 1.25 / 1.5 / 2.0
    "video_playback_rate": 1.0,
    # 题目截图一并发给支持识图的模型（qwen-vl、gpt-4o 等），默认关闭
    "screenshot": False,
    # 用户自定义音色克隆（本地 OpenVoice V2 或可选云端 API）
    "voice_clone_enabled": False,
    "voice_clone_mode": "local",
    "voice_clone_profile": "my_voice",
    "voice_clone_tau": 0.35,
    "voice_clone_base_gender": "auto",
    "voice_clone_rate": "+0%",
    "voice_clone_convert_reference": True,
    "voice_clone_api_url": "",
    "voice_clone_api_key": "",
    "voice_clone_api_model": "FunAudioLLM/CosyVoice2-0.5B",
}

_BOOL_KEYS = frozenset({"debug_mode", "screenshot", "voice_clone_enabled", "voice_clone_convert_reference"})
_FLOAT_KEYS = frozenset({"temperature", "voice_clone_tau", "video_playback_rate"})
_INT_KEYS = frozenset({"max_tokens"})


def load_config(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        return merged
    return dict(DEFAULT_CONFIG)


def save_config(path, data: dict):
    out = dict(data)
    token = out.get("token_full", "")
    if token and isinstance(token, str):
        token = token.strip()
        if (token.startswith("'") and token.endswith("'")) or (
            token.startswith('"') and token.endswith('"')
        ):
            token = token[1:-1].strip()
        out["token_full"] = token

    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return True


class ConfigEditor(QObject):
    saved = Signal(str, bool)
    valuesChanged = Signal()
    apiTestingChanged = Signal()
    apiTestFinished = Signal(str, bool)
    _apiTestResult = Signal(str, bool)

    def __init__(self, path, first_run=False, on_saved=None):
        super().__init__()
        self.path = path
        self.first_run = first_run
        self.completed = False
        self._on_saved = on_saved
        self.cfg = load_config(path)
        self._api_testing = False
        self._apiTestResult.connect(self._finish_api_test, Qt.ConnectionType.QueuedConnection)

    @Property("QVariantMap", notify=valuesChanged)
    def values(self):
        return self.cfg

    @Property(bool, constant=True)
    def firstRun(self):
        return self.first_run

    @Property(bool, notify=apiTestingChanged)
    def apiTesting(self):
        return self._api_testing

    @Slot("QVariantMap")
    def testApi(self, values):
        if self._api_testing:
            return
        settings = tuple(str(values.get(name) or "").strip() for name in ("base_url", "api_key", "model"))
        self._api_testing = True
        self.apiTestingChanged.emit()

        def run_test():
            result = probe_model(*settings)
            self._apiTestResult.emit(result.message, result.ok)

        threading.Thread(target=run_test, name="settings-api", daemon=True).start()

    @Slot(str, bool)
    def _finish_api_test(self, message, ok):
        self._api_testing = False
        self.apiTestingChanged.emit()
        self.apiTestFinished.emit(message, ok)

    def refresh_from_disk(self):
        self.cfg = load_config(self.path)
        self.valuesChanged.emit()

    @Slot("QVariantMap")
    def save(self, values):
        if self._api_testing:
            return
        # Always merge on top of latest disk config so voice settings edited in VoiceStudio are preserved
        data = load_config(self.path)
        data.update(values)
        for key in DEFAULT_CONFIG:
            if key in _BOOL_KEYS or key in _FLOAT_KEYS or key in _INT_KEYS:
                continue
            if key in data and data[key] is not None:
                data[key] = str(data[key]).strip()
        missing = [
            label for key, label in (
                ("username", "账号"), ("password", "密码"),
                ("api_key", "API Key"), ("base_url", "API 地址"), ("model", "模型名称"),
            ) if not data.get(key)
        ]
        if missing:
            self.saved.emit("请填写必填项：" + "、".join(missing), False)
            return
        error = model_settings_error(data["base_url"], data["api_key"], data["model"])
        if error:
            self.saved.emit(error, False)
            return
        for key in _BOOL_KEYS:
            data[key] = bool(data.get(key, DEFAULT_CONFIG[key]))
        try:
            data["max_tokens"] = int(data["max_tokens"])
            if data["max_tokens"] < 1:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            self.saved.emit("最大 Token 数必须是正整数", False)
            return
        try:
            data["temperature"] = float(data["temperature"])
            if not 0 <= data["temperature"] <= 2:
                raise ValueError
        except (ValueError, TypeError):
            self.saved.emit("温度必须在 0 到 2 之间", False)
            return
        try:
            data["voice_clone_tau"] = max(0.10, min(0.85, float(data.get("voice_clone_tau", 0.35))))
        except (ValueError, TypeError):
            data["voice_clone_tau"] = 0.35
        try:
            rate_val = float(data.get("video_playback_rate", 1.0))
            data["video_playback_rate"] = rate_val if rate_val in (1.0, 1.25, 1.5, 2.0) else 1.0
        except (ValueError, TypeError):
            data["video_playback_rate"] = 1.0
        try:
            save_config(self.path, data)
        except Exception as exc:
            self.saved.emit(f"保存失败：{exc}", False)
            return
        self.cfg = data
        self.completed = True
        self.valuesChanged.emit()
        if callable(self._on_saved):
            try:
                self._on_saved(data)
            except Exception:
                pass
        self.saved.emit("配置已保存" if self.first_run else "配置已保存。语音设置实时生效，模型、账号和浏览器设置重启后生效。", True)


def setup_first_run():
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    _, mono_family = configure_ui_font(app)
    editor = ConfigEditor(PROJECT_ROOT / "config.json", first_run=True)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("uiMonoFont", mono_family)
    engine.rootContext().setContextProperty("configEditor", editor)
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "ConfigEditor.qml")))
    if not engine.rootObjects():
        raise RuntimeError("配置界面加载失败")
    app.exec()
    delete_qt_object(engine)
    return app, editor.completed


def configure_ui_font(app):
    families = set(QFontDatabase.families())
    # Prefer the platform's UI face rather than a document font with light
    # named subfamilies. Keep explicit fallbacks for macOS and Linux.
    preferred = (("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "Source Han Sans SC")
                 if sys.platform == "win32" else
                 ("PingFang SC", "Noto Sans SC", "Source Han Sans SC", "Microsoft YaHei UI"))
    family = next((name for name in preferred if name in families), app.font().family())
    mono_preferred = (("Cascadia Mono", "JetBrains Mono", "Consolas", "Courier New")
                      if sys.platform == "win32" else
                      ("SF Mono", "JetBrains Mono", "Menlo", "DejaVu Sans Mono"))
    mono_family = next((name for name in mono_preferred if name in families), "Consolas")
    font = QFont(family, 10, QFont.Weight.Medium)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app.setFont(font)
    return family, mono_family


class VoiceStudioBridge(QObject):
    """Qt Quick bridge for User Voice Cloning Studio (recording, import, preview, live sync)."""

    stateChanged = Signal()
    waveformChanged = Signal()
    microphonesChanged = Signal()
    # Internal signals to safely marshal background worker updates back to Qt GUI thread
    _workerEvent = Signal(str, object)

    DEFAULT_PREVIEW_TEXT = (
        "Hello everyone! Learning English with my own natural voice feels smooth, clear, and genuinely confident."
    )

    def __init__(self, config_path: Path, solver=None, config_editor: ConfigEditor | None = None, parent=None):
        super().__init__(parent)
        self.config_path = Path(config_path)
        self.solver = solver
        self.config_editor = config_editor
        self._cfg = load_config(self.config_path)
        self._notice = ""
        self._notice_ok = True
        self._busy_task = ""        # "", "processing", "downloading", "synthesizing"
        self._download_progress = 0.0
        self._download_status = ""
        self._record_seconds = 0.0
        self._waveform = [0.08] * 28
        self._microphones = []
        self._selected_mic = -1
        self._last_preview_wav: bytes = b""
        self._last_preview_latency = 0.0
        self._last_preview_text = self.DEFAULT_PREVIEW_TEXT
        self._preview_has_audio = False
        self._stop_download = threading.Event()
        self._cached_wave_key = ""

        from .voice_cloner import (
            AudioPlayer,
            GUIDED_SCRIPTS,
            MicRecorder,
            VoiceCloneEngine,
            VoiceProfileManager,
            list_input_microphones,
        )

        self._guided_scripts = list(GUIDED_SCRIPTS)
        self._list_mics_fn = list_input_microphones
        self._recorder = MicRecorder(sample_rate=22050)
        self._player = AudioPlayer()
        self._profiles = VoiceProfileManager()

        # Share solver's VoiceCloneEngine if available so weights aren't loaded twice
        existing_cloner = getattr(solver, "voice_cloner", None) if solver is not None else None
        if existing_cloner is not None:
            self._engine = existing_cloner
        else:
            self._engine = VoiceCloneEngine(
                enabled=bool(self._cfg.get("voice_clone_enabled", False)),
                mode=str(self._cfg.get("voice_clone_mode") or "local"),
                profile_id=str(self._cfg.get("voice_clone_profile") or "my_voice"),
                tau=float(self._cfg.get("voice_clone_tau", 0.35)),
                base_gender=str(self._cfg.get("voice_clone_base_gender") or "auto"),
                rate=str(self._cfg.get("voice_clone_rate") or "+0%"),
                convert_reference=bool(self._cfg.get("voice_clone_convert_reference", True)),
                proxy=str(self._cfg.get("tts_proxy") or ""),
                api_url=str(self._cfg.get("voice_clone_api_url") or ""),
                api_key=str(self._cfg.get("voice_clone_api_key") or ""),
                api_model=str(self._cfg.get("voice_clone_api_model") or "FunAudioLLM/CosyVoice2-0.5B"),
            )

        self._workerEvent.connect(self._handle_worker_event)

        self._meter_timer = QTimer(self)
        self._meter_timer.setInterval(75)
        self._meter_timer.timeout.connect(self._poll_meter)

        self._state: dict = {}
        self.refreshMicrophones()
        self._rebuild_state()

    @Property("QVariantMap", notify=stateChanged)
    def state(self):
        return self._state

    @Property("QVariantList", notify=waveformChanged)
    def waveform(self):
        return self._waveform

    @Property("QVariantList", notify=microphonesChanged)
    def microphones(self):
        return self._microphones

    @Property("QVariantList", constant=True)
    def guidedScripts(self):
        return self._guided_scripts

    def _rebuild_state(self):
        profile_id = str(self._cfg.get("voice_clone_profile") or "my_voice")
        profile = self._profiles.get_profile(profile_id)
        model_ready = self._engine.converter.is_downloaded()
        enabled = bool(self._cfg.get("voice_clone_enabled", False))
        mode = str(self._cfg.get("voice_clone_mode") or "local").lower()
        has_profile = bool(profile and profile.get("has_wav"))
        ready_to_use = bool(
            enabled and has_profile and (
                (mode == "cloud" and self._cfg.get("voice_clone_api_url") and self._cfg.get("voice_clone_api_key"))
                or (mode != "cloud" and model_ready)
            )
        )

        gender_map = {"male": "偏低沉 / 男声音域", "female": "偏清亮 / 女声音域"}
        gender_guess = (profile or {}).get("gender_guess", "")

        # Populate a real static 28-bar waveform preview from saved reference WAV when idle
        if not self._recorder.is_recording:
            wave_key = f"{(profile or {}).get('wav_path', '')}|{(profile or {}).get('updated_at', '')}"
            if wave_key != self._cached_wave_key:
                self._cached_wave_key = wave_key
                self._waveform = self._compute_static_waveform((profile or {}).get("wav_path", ""))
                self.waveformChanged.emit()

        self._state = {
            "enabled": enabled,
            "mode": mode,
            "tau": round(float(self._cfg.get("voice_clone_tau", 0.35)), 2),
            "baseGender": str(self._cfg.get("voice_clone_base_gender") or "auto"),
            "speechRate": str(self._cfg.get("voice_clone_rate") or "+0%"),
            "convertReference": bool(self._cfg.get("voice_clone_convert_reference", True)),
            "apiUrl": str(self._cfg.get("voice_clone_api_url") or ""),
            "apiKey": str(self._cfg.get("voice_clone_api_key") or ""),
            "apiModel": str(self._cfg.get("voice_clone_api_model") or "FunAudioLLM/CosyVoice2-0.5B"),
            "modelReady": model_ready,
            "modelSizeMb": self._engine.converter.model_size_mb(),
            "downloading": self._busy_task == "downloading",
            "downloadProgress": round(float(self._download_progress), 3),
            "downloadStatus": self._download_status,
            "busyTask": self._busy_task,
            "busy": bool(self._busy_task),
            "recording": self._recorder.is_recording,
            "recordSeconds": round(float(self._record_seconds), 1),
            "playing": self._player.is_playing,
            "hasProfile": has_profile,
            "profileName": (profile or {}).get("name", "未录入音色"),
            "profileDuration": (profile or {}).get("raw_duration") or (profile or {}).get("duration", 0.0),
            "profileActiveDuration": (profile or {}).get("active_duration", 0.0),
            "profileF0": (profile or {}).get("f0_hz", 0.0),
            "profileGenderGuess": gender_guess,
            "profileGenderLabel": gender_map.get(gender_guess, "待识别"),
            "profileSnrDb": (profile or {}).get("snr_db", 0.0),
            "profileQualityLabel": (profile or {}).get("quality_label", ""),
            "profileQualityScore": int((profile or {}).get("quality_score", 0)),
            "profileWarning": (profile or {}).get("warning", ""),
            "profileUpdatedAt": (profile or {}).get("updated_at", ""),
            "profileSeReady": bool((profile or {}).get("has_se", False)),
            "readyToUse": ready_to_use,
            "statusBadge": (
                "已启用 · 就绪" if ready_to_use else
                "已录入 · 未启用" if has_profile and not enabled else
                "待下载模型" if has_profile and mode == "local" and not model_ready else
                "待录入音色"
            ),
            "selectedMic": self._selected_mic,
            "notice": self._notice,
            "noticeOk": self._notice_ok,
            "previewReady": self._preview_has_audio,
            "previewLatency": round(float(self._last_preview_latency), 2),
            "defaultPreviewText": self.DEFAULT_PREVIEW_TEXT,
        }
        self.stateChanged.emit()

    @staticmethod
    def _compute_static_waveform(wav_path: str, bars: int = 28) -> list:
        if not wav_path or not os.path.isfile(wav_path):
            return [0.10] * bars
        try:
            import numpy as np
            from .voice_cloner import decode_audio_to_float32
            arr = decode_audio_to_float32(wav_path, sample_rate=16000)
            if arr.size < bars:
                return [0.12] * bars
            chunks = np.array_split(np.abs(arr), bars)
            peaks = np.array([float(np.percentile(c, 92)) for c in chunks], dtype=np.float32)
            hi = max(0.05, float(np.max(peaks)))
            return [round(max(0.12, min(0.95, float((p / hi) ** 0.72))), 3) for p in peaks]
        except Exception:
            return [0.12] * bars

    def _set_notice(self, message: str, ok: bool = True):
        self._notice = message
        self._notice_ok = ok
        self._rebuild_state()

    def _persist_and_sync(self, updates: dict):
        data = load_config(self.config_path)
        data.update(updates)
        save_config(self.config_path, data)
        self._cfg = data
        self._engine.configure(
            enabled=bool(data.get("voice_clone_enabled", False)),
            mode=str(data.get("voice_clone_mode") or "local"),
            profile_id=str(data.get("voice_clone_profile") or "my_voice"),
            tau=float(data.get("voice_clone_tau", 0.35)),
            base_gender=str(data.get("voice_clone_base_gender") or "auto"),
            rate=str(data.get("voice_clone_rate") or "+0%"),
            convert_reference=bool(data.get("voice_clone_convert_reference", True)),
            proxy=str(data.get("tts_proxy") or ""),
            api_url=str(data.get("voice_clone_api_url") or ""),
            api_key=str(data.get("voice_clone_api_key") or ""),
            api_model=str(data.get("voice_clone_api_model") or "FunAudioLLM/CosyVoice2-0.5B"),
        )
        if self.solver is not None and hasattr(self.solver, "reload_voice_config"):
            try:
                self.solver.reload_voice_config(data)
            except Exception:
                pass
        if self.config_editor is not None:
            self.config_editor.refresh_from_disk()
        self._rebuild_state()

    def sync_from_config(self, data: dict):
        self._cfg = dict(data)
        self._engine.configure(
            enabled=bool(data.get("voice_clone_enabled", False)),
            mode=str(data.get("voice_clone_mode") or "local"),
            profile_id=str(data.get("voice_clone_profile") or "my_voice"),
            tau=float(data.get("voice_clone_tau", 0.35)),
            base_gender=str(data.get("voice_clone_base_gender") or "auto"),
            rate=str(data.get("voice_clone_rate") or "+0%"),
            convert_reference=bool(data.get("voice_clone_convert_reference", True)),
            proxy=str(data.get("tts_proxy") or ""),
            api_url=str(data.get("voice_clone_api_url") or ""),
            api_key=str(data.get("voice_clone_api_key") or ""),
            api_model=str(data.get("voice_clone_api_model") or "FunAudioLLM/CosyVoice2-0.5B"),
        )
        self._rebuild_state()

    def _poll_meter(self):
        if self._recorder.is_recording:
            self._record_seconds = self._recorder.elapsed_seconds
            self._waveform = self._recorder.get_levels()
            self.waveformChanged.emit()
            self._rebuild_state()
            # Auto-stop safety cap at 45 seconds
            if self._record_seconds >= 45.0:
                self.stopRecording()
        elif not self._player.is_playing:
            self._meter_timer.stop()

    @Slot(str, object)
    def _handle_worker_event(self, kind: str, payload):
        if kind == "download_progress":
            pct, msg = payload
            self._download_progress = float(pct)
            self._download_status = str(msg)
            self._rebuild_state()
        elif kind == "download_done":
            ok, msg = payload
            self._busy_task = ""
            self._download_progress = 1.0 if ok else 0.0
            self._download_status = msg
            self._set_notice(msg, ok)
            # If user already had a sample saved before downloading model, extract SE now
            if ok:
                prof = self._profiles.get_profile(str(self._cfg.get("voice_clone_profile") or "my_voice"))
                if prof and prof.get("has_wav") and not prof.get("has_se"):
                    self._extract_se_async()
        elif kind == "profile_done":
            ok, msg, auto_enable = payload
            self._busy_task = ""
            self._engine.invalidate_profile_cache()
            self._last_preview_wav = b""
            self._preview_has_audio = False
            if ok and auto_enable and not self._cfg.get("voice_clone_enabled"):
                self._persist_and_sync({"voice_clone_enabled": True})
            elif ok and self.solver is not None and hasattr(self.solver, "reload_voice_config"):
                self.solver.reload_voice_config(self._cfg)
            self._set_notice(msg, ok)
        elif kind == "synth_done":
            ok, msg, wav_bytes, latency = payload
            self._busy_task = ""
            if ok and wav_bytes:
                self._last_preview_wav = wav_bytes
                self._last_preview_latency = latency
                self._preview_has_audio = True
                self._set_notice(msg, True)
                self._player.play_audio(wav_bytes, on_finished=lambda: self._workerEvent.emit("play_ended", None))
                self._rebuild_state()
            else:
                self._set_notice(msg, False)
        elif kind == "play_ended":
            self._rebuild_state()

    def _extract_se_async(self):
        if self._busy_task:
            return
        self._busy_task = "processing"
        self._rebuild_state()
        profile_id = str(self._cfg.get("voice_clone_profile") or "my_voice")

        def worker():
            try:
                self._profiles.load_or_extract_se(profile_id, self._engine.converter)
                self._workerEvent.emit("profile_done", (True, "已自动完成声纹特征提取，可以试听或开始答题！", False))
            except Exception as exc:
                self._workerEvent.emit("profile_done", (False, f"声纹提取失败：{exc}", False))

        threading.Thread(target=worker, daemon=True, name="voice-se").start()

    @Slot()
    def dismissNotice(self):
        self._notice = ""
        self._rebuild_state()

    @Slot()
    def refreshMicrophones(self):
        mics = self._list_mics_fn()
        self._microphones = mics
        if mics:
            valid = {m["index"] for m in mics}
            if self._selected_mic not in valid:
                self._selected_mic = mics[0]["index"]
        else:
            self._selected_mic = -1
        self.microphonesChanged.emit()
        self._rebuild_state()

    @Slot(int)
    def selectMicrophone(self, device_index: int):
        self._selected_mic = int(device_index)
        self._rebuild_state()

    @Slot()
    def startRecording(self):
        if self._busy_task or self._recorder.is_recording:
            return
        self._player.stop()
        try:
            mic_idx = self._selected_mic if self._selected_mic >= 0 else None
            self._recorder.start(device_index=mic_idx)
            self._record_seconds = 0.0
            self._notice = ""
            self._meter_timer.start()
            self._rebuild_state()
        except Exception as exc:
            self._set_notice(f"无法打开麦克风：{exc}", False)

    @Slot()
    def stopRecording(self):
        if not self._recorder.is_recording:
            return
        self._meter_timer.stop()
        waveform, sr = self._recorder.stop()
        self._waveform = [0.08] * 28
        self.waveformChanged.emit()
        dur = waveform.size / max(1, sr)
        if dur < 1.5:
            self._set_notice("录音不足 1.5 秒，请至少朗读 6–15 秒清晰人声", False)
            return

        self._busy_task = "processing"
        self._set_notice("正在降噪清洗并提取你的专属声纹特征…", True)
        profile_id = str(self._cfg.get("voice_clone_profile") or "my_voice")

        def worker():
            try:
                meta = self._profiles.save_from_waveform(
                    waveform, sr,
                    profile_id=profile_id,
                    name="我的麦克风音色",
                    converter=self._engine.converter if self._engine.converter.is_downloaded() else None,
                )
                msg = (
                    f"音色录入完成（有效人声 {meta['active_duration']:.1f} 秒 · {meta['quality_label']}）"
                    + ("，已自动启用「我的音色」" if meta.get("has_se") else "，下载本地模型后即可试听")
                )
                self._workerEvent.emit("profile_done", (True, msg, bool(meta.get("has_se"))))
            except Exception as exc:
                self._workerEvent.emit("profile_done", (False, f"录音处理失败：{exc}", False))

        threading.Thread(target=worker, daemon=True, name="voice-record-save").start()

    @Slot()
    def importAudioFile(self):
        if self._busy_task or self._recorder.is_recording:
            return
        from .voice_cloner import pick_audio_file_native
        self._player.stop()
        path = pick_audio_file_native(str(PROJECT_ROOT))
        if not path:
            return

        self._busy_task = "processing"
        self._set_notice("正在导入音频、智能降噪并提取你的声纹特征…", True)
        profile_id = str(self._cfg.get("voice_clone_profile") or "my_voice")
        stem = Path(path).stem[:24] or "导入音色"

        def worker():
            try:
                meta = self._profiles.save_from_audio_file(
                    path,
                    profile_id=profile_id,
                    name=f"我的音色 · {stem}",
                    source_type="import",
                    converter=self._engine.converter if self._engine.converter.is_downloaded() else None,
                )
                msg = (
                    f"音频导入完成（有效人声 {meta['active_duration']:.1f} 秒 · {meta['quality_label']}）"
                    + ("，已自动启用「我的音色」" if meta.get("has_se") else "，下载本地模型后即可试听")
                )
                self._workerEvent.emit("profile_done", (True, msg, bool(meta.get("has_se"))))
            except Exception as exc:
                self._workerEvent.emit("profile_done", (False, f"导入音频失败：{exc}", False))

        threading.Thread(target=worker, daemon=True, name="voice-import").start()

    @Slot()
    def downloadModel(self):
        if self._busy_task:
            return
        if self._engine.converter.is_downloaded():
            self._set_notice("本地音色模型（OpenVoice V2）已就绪", True)
            return
        self._stop_download.clear()
        self._busy_task = "downloading"
        self._download_progress = 0.02
        self._download_status = "正在连接镜像站下载本地音色模型（约 125 MB）…"
        self._rebuild_state()

        def worker():
            try:
                self._engine.converter.ensure_downloaded(
                    progress_cb=lambda pct, msg: self._workerEvent.emit("download_progress", (pct, msg)),
                    stop_event=self._stop_download,
                )
                self._workerEvent.emit("download_done", (True, "本地音色模型下载完成，已存入 D 盘"))
            except Exception as exc:
                self._workerEvent.emit("download_done", (False, f"模型下载失败：{exc}"))

        threading.Thread(target=worker, daemon=True, name="voice-model-dl").start()

    @Slot()
    def playOriginalSample(self):
        if self._recorder.is_recording:
            return
        if self._player.is_playing:
            self._player.stop()
            self._rebuild_state()
            return
        profile_id = str(self._cfg.get("voice_clone_profile") or "my_voice")
        prof = self._profiles.get_profile(profile_id)
        if not prof or not prof.get("has_wav"):
            self._set_notice("还没有录入声音样本，请先录音或导入音频", False)
            return
        self._player.play_audio(prof["wav_path"], on_finished=lambda: self._workerEvent.emit("play_ended", None))
        self._rebuild_state()

    @Slot(str)
    def synthesizeAndPlay(self, text: str):
        if self._busy_task or self._recorder.is_recording:
            return
        sample_text = (text or "").strip() or self.DEFAULT_PREVIEW_TEXT
        profile_id = str(self._cfg.get("voice_clone_profile") or "my_voice")
        prof = self._profiles.get_profile(profile_id)
        if not prof or not prof.get("has_wav"):
            self._set_notice("请先录制或导入一段你的声音样本，再生成试听", False)
            return
        if self._engine.mode == "local" and not self._engine.converter.is_downloaded():
            self._set_notice("本地音色模型尚未下载，请先点击「下载本地模型（125 MB）」", False)
            return

        self._player.stop()
        self._busy_task = "synthesizing"
        self._set_notice("正在模拟你的音色生成流畅英文试听音频…", True)

        def worker():
            t0 = time.perf_counter()
            try:
                fmt, data = self._engine.synthesize(sample_text, out_format="wav", force=True)
                dt = time.perf_counter() - t0
                if data:
                    self._workerEvent.emit(
                        "synth_done",
                        (True, f"试听生成成功（耗时 {dt:.1f} 秒 · 广播级润色已应用），正在播放…", data, dt),
                    )
                else:
                    self._workerEvent.emit(
                        "synth_done",
                        (False, self._engine.last_error or "试听生成失败，请检查网络或样本质量", b"", dt),
                    )
            except Exception as exc:
                self._workerEvent.emit("synth_done", (False, f"试听生成异常：{exc}", b"", 0.0))

        threading.Thread(target=worker, daemon=True, name="voice-preview-synth").start()

    @Slot()
    def replayLastPreview(self):
        if self._player.is_playing:
            self._player.stop()
            self._rebuild_state()
            return
        if not self._last_preview_wav:
            return
        self._player.play_audio(self._last_preview_wav, on_finished=lambda: self._workerEvent.emit("play_ended", None))
        self._rebuild_state()

    @Slot()
    def stopPlayback(self):
        self._player.stop()
        self._rebuild_state()

    @Slot()
    def deleteProfile(self):
        if self._busy_task or self._recorder.is_recording:
            return
        self._player.stop()
        profile_id = str(self._cfg.get("voice_clone_profile") or "my_voice")
        self._profiles.delete_profile(profile_id)
        self._engine.invalidate_profile_cache()
        self._last_preview_wav = b""
        self._preview_has_audio = False
        self._persist_and_sync({"voice_clone_enabled": False})
        self._set_notice("已清除已保存的音色档案，已切回标准语音", True)

    @Slot(str, "QVariant")
    def updateSetting(self, key: str, value):
        allowed = {
            "voice_clone_enabled", "voice_clone_mode", "voice_clone_tau",
            "voice_clone_base_gender", "voice_clone_rate", "voice_clone_convert_reference",
            "voice_clone_api_url", "voice_clone_api_key", "voice_clone_api_model",
        }
        if key not in allowed:
            return
        if key in _BOOL_KEYS:
            value = bool(value)
        elif key == "voice_clone_tau":
            try:
                value = round(max(0.10, min(0.85, float(value))), 2)
            except (TypeError, ValueError):
                value = 0.35
        else:
            value = str(value or "").strip()
        self._persist_and_sync({key: value})

    def cleanup(self):
        self._stop_download.set()
        self._meter_timer.stop()
        try:
            if self._recorder.is_recording:
                self._recorder.stop()
        except Exception:
            pass
        try:
            self._player.stop()
        except Exception:
            pass


class TaskListModel(QAbstractListModel):
    """Keep delegates and scroll position when only selection/status changes."""
    fields = ("taskId", "title", "isRequired", "status", "chosen", "scored", "statusName",
              "sectionHeader", "selectable", "scoreText", "scoreAbnormal")
    roles = {Qt.UserRole + i + 1: name.encode() for i, name in enumerate(fields)}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def roleNames(self):
        return self.roles

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        name = self.roles.get(role)
        return self.rows[index.row()].get(name.decode()) if name else None

    def update(self, rows):
        # Notify insertions/removals explicitly for empty search transitions.
        if self.rows and not rows:
            self.beginRemoveRows(QModelIndex(), 0, len(self.rows) - 1)
            self.rows = []
            self.endRemoveRows()
            return
        if rows and not self.rows:
            self.beginInsertRows(QModelIndex(), 0, len(rows) - 1)
            self.rows = rows
            self.endInsertRows()
            return
        if [r["taskId"] for r in rows] != [r["taskId"] for r in self.rows]:
            self.beginResetModel()
            self.rows = rows
            self.endResetModel()
            return
        for i, row in enumerate(rows):
            previous = self.rows[i]
            roles = [role for role, name in self.roles.items()
                     if previous.get(name.decode()) != row.get(name.decode())]
            if roles:
                self.rows[i] = row
                self.dataChanged.emit(self.index(i), self.index(i), roles)


def task_section_label(section, title):
    """Keep task names in rows rather than repeating them in section headings."""
    parts = []
    for part in section.split("/"):
        part = part.strip()
        if part and (not parts or part.casefold() != parts[-1].casefold()):
            parts.append(part)
    if parts and parts[-1].casefold() == title.strip().casefold():
        parts.pop()
    return " / ".join(parts)


class NativeBridge(QObject):
    changed = Signal()
    unitsChanged = Signal()
    noticeChanged = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._state = {}
        self._units = []
        self._summary = {}
        self._unit = "all"
        self._filter = "all"
        self._query = ""
        self._notice = ""
        self.task_model = TaskListModel(self)
        self.refresh()
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    @Property("QVariantMap", notify=changed)
    def state(self):
        return self._state

    @Property("QVariantList", notify=unitsChanged)
    def units(self):
        return self._units

    @Property("QVariantMap", notify=changed)
    def summary(self):
        return self._summary

    @Property(str, notify=changed)
    def activeUnit(self):
        return self._unit

    @Property(str, notify=changed)
    def activeFilter(self):
        return self._filter

    @Property(str, notify=noticeChanged)
    def notice(self):
        return self._notice

    def _result(self, result):
        if not result.get("ok"):
            self._notice = result.get("error", "操作未完成，请重试")
            self.noticeChanged.emit()
        self.refresh(force=True)

    @Slot()
    def dismissNotice(self):
        self._notice = ""
        self.noticeChanged.emit()

    @Slot()
    def resetFilters(self):
        self._filter = "all"
        self._query = ""
        self.refresh(force=True)

    @Slot(str)
    def chooseUnit(self, unit):
        self._unit = unit
        self.refresh(force=True)

    @Slot(str)
    def chooseFilter(self, value):
        self._filter = value
        self.refresh(force=True)

    @Slot(str)
    def search(self, query):
        self._query = query.casefold().strip()
        self.refresh(force=True)

    @Slot(str, bool)
    def selectTask(self, task_id, chosen):
        self._result(self.controller.set_selection([task_id], chosen))

    @Slot(str)
    def selectVisible(self, mode):
        self._result(self.controller.select_tasks(mode, [r["taskId"] for r in self.task_model.rows]))

    @Slot()
    def toggleVisible(self):
        rows = [r for r in self.task_model.rows if r["selectable"]]
        self._result(self.controller.set_selection(
            [r["taskId"] for r in rows], not (rows and all(r["chosen"] for r in rows))))

    @Slot()
    def clearSelection(self):
        self._result(self.controller.select_tasks("none", [t["id"] for t in self._state["tasks"]]))

    @Slot(bool)
    def excludeUngraded(self, value):
        self._result(self.controller.set_exclude_ungraded(value))

    @Slot(str, result=bool)
    def start(self, action):
        task_ids = [row["taskId"] for row in self.task_model.rows] if action == "run" else None
        result = self.controller.start_action(action, task_ids=task_ids)
        self._result(result)
        return bool(result.get("ok"))

    @Slot()
    def stop(self):
        self._result(self.controller.stop())

    @Slot()
    def restartBrowser(self):
        self._result(self.controller.restart_browser())

    @Slot()
    def copyLogs(self):
        QGuiApplication.clipboard().setText("\n".join(
            f'{row["time"]}  {row["message"]}' for row in self._state["logs"]))
        self._notice = "运行日志已复制"
        self.noticeChanged.emit()

    @Slot()
    def refresh(self, force=False):
        state = self.controller.snapshot()
        if not force and state == self._state:
            return
        tasks = state["tasks"]
        units = []
        for unit in dict.fromkeys(t["unit"] for t in tasks):
            group = [t for t in tasks if t["unit"] == unit]
            units.append({"name": unit, "title": group[0]["unitName"], "total": len(group),
                          "done": sum(t["status"] == "done" for t in group)})
        if units != self._units:
            self._units = units
            self.unitsChanged.emit()
        if self._unit != "all" and not any(u["name"] == self._unit for u in self._units):
            self._unit = "all"
        scoped = [t for t in tasks if self._unit == "all" or t["unit"] == self._unit]
        counts = {"all": len(scoped), "pending": sum(t["status"] != "done" for t in scoped),
                  "done": sum(t["status"] == "done" for t in scoped),
                  "required": sum(t["required"] for t in scoped)}
        visible = [t for t in scoped if (
            self._query in (f'{t["title"]} {t["section"]} {t["unit"]} {score_text(t.get("score"))} ' + (
                "不计分" if t["scored"] is False else "计分未知" if t["scored"] is None else "计分")).casefold()) and (
            self._filter == "all" or self._filter == "pending" and t["status"] != "done"
            or self._filter == "done" and t["status"] == "done"
            or self._filter == "required" and t["required"])]
        statuses = {"done": "已完成", "doing": "进行中", "todo": "未开始", "unknown": "待确认"}
        rows = []
        for task in visible:
            status = task["status"]
            status_name = statuses.get(status, "待确认")
            if task.get("manual_conversation") and status != "done":
                status_name = "需手动对话"
            elif status == "done" and task.get("score_pending"):
                status_name = "待出分"
            score = score_text(task.get("score"))
            below_target = bool(task.get("score_abnormal"))
            rows.append(dict(
                task, taskId=task["id"], chosen=task["selected"], isRequired=task["required"],
                statusName=status_name, scoreText=f"{score} 分" if score else ("有低分" if below_target else ""),
                scoreAbnormal=below_target,
                selectable=task.get("selectable", not (state["excludeUngraded"] and task["scored"] is False)),
            ))
        previous_group = None
        for row in rows:
            section = task_section_label(row["section"], row["title"])
            group = (row["unit"], section)
            if self._unit == "all":
                if section and section.casefold().startswith(row["unit"].casefold()):
                    label = section
                else:
                    label = " / ".join(part for part in (row["unit"], section) if part)
            else:
                label = section
            row["sectionHeader"] = label if group != previous_group else ""
            previous_group = group
        self.task_model.update(rows)
        selected = [t for t in tasks if t["selected"]]
        selectable = [t for t in visible if not (state["excludeUngraded"] and t["scored"] is False)]
        selected_visible = sum(t["selected"] for t in visible)
        self._summary = {
            "counts": counts, "visible": len(visible), "selected": len(selected),
            "selectedUnits": len(set(t["unit"] for t in selected)),
            "selectableVisible": len(selectable),
            "selectedVisible": selected_visible, "selectedHidden": len(selected) - selected_visible,
            "allChecked": bool(selectable) and all(t["selected"] for t in selectable),
            "someChecked": any(t["selected"] for t in selectable),
            "total": len(tasks),
            "unitTitle": scoped[0]["unitName"] if self._unit != "all" and scoped else "",
        }
        self._state = state
        self.changed.emit()


class NativeModernGUI(WorkbenchController):
    def mainloop(self, *, screenshot=None, close_after=None):
        from .meta import APP_NAME
        QQuickStyle.setStyle("Basic")
        app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
        app.setApplicationName(APP_NAME)
        app.setOrganizationName(APP_NAME)
        app.setWindowIcon(QIcon(str(QML_DIR / "icons" / "app.svg")))
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
        _, mono_family = configure_ui_font(app)
        log_path = PROJECT_ROOT / "logs" / "native-ui.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        qt_logger = logging.getLogger("UniCampusHelper.Native")
        qt_logger.setLevel(logging.WARNING)
        qt_logger.propagate = False
        log_file = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
        log_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        qt_logger.addHandler(log_file)

        def qt_message(kind, context, message):
            if kind in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                qt_logger.warning("%s:%s %s", context.file or "Qt", context.line, message)
                print(f"Qt: {message}", file=sys.stderr)

        previous_handler = qInstallMessageHandler(qt_message)
        engine = QQmlApplicationEngine()
        bridge = NativeBridge(self)
        config_path = PROJECT_ROOT / "config.json"

        voice_bridge_ref = [None]

        def on_config_saved(data):
            if self.solver is not None and hasattr(self.solver, "reload_voice_config"):
                self.solver.reload_voice_config(data)
            if voice_bridge_ref[0] is not None:
                voice_bridge_ref[0].sync_from_config(data)

        config_editor = ConfigEditor(config_path, on_saved=on_config_saved)
        voice_bridge = VoiceStudioBridge(config_path, solver=self.solver, config_editor=config_editor)
        voice_bridge_ref[0] = voice_bridge

        engine.rootContext().setContextProperty("uiMonoFont", mono_family)
        engine.rootContext().setContextProperty("configEditor", config_editor)
        engine.rootContext().setContextProperty("voiceStudio", voice_bridge)
        engine.rootContext().setContextProperty("workbench", bridge)
        engine.rootContext().setContextProperty("taskModel", bridge.task_model)
        # Python signal handlers run whenever Qt returns to this timer's callback.
        pulse = QTimer()
        pulse.timeout.connect(lambda: None)
        pulse.start(100)
        signals = [signal.SIGINT]
        if hasattr(signal, "SIGBREAK"):
            signals.append(signal.SIGBREAK)
        previous_signals = {sig: signal.getsignal(sig) for sig in signals}
        for sig in signals:
            signal.signal(sig, lambda *_: app.quit())
        app.aboutToQuit.connect(bridge.timer.stop)
        app.aboutToQuit.connect(voice_bridge.cleanup)
        app.aboutToQuit.connect(self.close)
        deferred = []

        def schedule(milliseconds, callback):
            timer = QTimer(bridge)
            timer.setSingleShot(True)
            timer.timeout.connect(callback)
            timer.start(milliseconds)
            deferred.append(timer)

        try:
            engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
            if not engine.rootObjects():
                raise RuntimeError(f"QML 界面加载失败，诊断日志：{log_path}")
            # Visible rows are lightweight. Finish their creation synchronously
            # so rapid filtering cannot cancel delegates with obsolete indices.
            engine.setIncubationController(None)
            window = engine.rootObjects()[0]
            if screenshot:
                def capture():
                    destination = Path(screenshot).resolve()
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not window.grabWindow().save(str(destination)):
                        qt_logger.warning("截图保存失败：%s", destination)
                schedule(1000, capture)
            if close_after:
                schedule(close_after, app.quit)
            result = app.exec()
        finally:
            pulse.stop()
            bridge.timer.stop()
            voice_bridge.cleanup()
            for timer in deferred:
                timer.stop()
            app.aboutToQuit.disconnect(bridge.timer.stop)
            app.aboutToQuit.disconnect(voice_bridge.cleanup)
            app.aboutToQuit.disconnect(self.close)
            for sig, previous in previous_signals.items():
                signal.signal(sig, previous)
            self.close()
            # Destroy QML bindings while their Python objects still exist.
            delete_qt_object(engine)
            delete_qt_object(voice_bridge)
            delete_qt_object(config_editor)
            delete_qt_object(bridge)
            qInstallMessageHandler(previous_handler)
            qt_logger.removeHandler(log_file)
            log_file.close()
        return result
