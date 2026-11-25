"""UniCampusHelper release verification suite.

Checks:
1. All Python modules compile cleanly (py_compile).
2. Centralized metadata (APP_NAME, APP_VERSION, GITHUB_DISPLAY) is consistent.
3. Light-mode QML theme loads with 0 warnings and zero legacy dark hardcodes.
4. Core Unit 4 (ipublish) & Unit 5 (ucontent) anti-regression checks pass.
"""

from __future__ import annotations

import os
import json
import logging
import py_compile
import re
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
venv_site = ROOT / ".venv" / "Lib" / "site-packages"
if venv_site.is_dir():
    sys.path.insert(0, str(venv_site))


def check_py_compile() -> None:
    files = ([ROOT / "UnipusAI_Helper.py"] + sorted((ROOT / "unipus_helper").glob("*.py"))
             + sorted((ROOT / "scripts").glob("*.py")))
    for path in files:
        py_compile.compile(str(path), doraise=True)
    print(f"[PASS] py_compile: {len(files)} Python modules compiled cleanly")


def check_metadata_and_branding() -> None:
    from unipus_helper import meta

    assert meta.APP_NAME == "UniCampusHelper"
    assert re.fullmatch(r"\d+\.\d+\.\d+", meta.APP_VERSION)
    assert f"**v{meta.APP_VERSION}**" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert meta.GITHUB_OWNER == "zhuangzztong-bit"
    assert meta.GITHUB_DISPLAY == "github.com/zhuangzztong-bit/UniCampusHelper"

    qml_dir = ROOT / "unipus_helper" / "qml"
    for qml in sorted(qml_dir.glob("*.qml")):
        text = qml.read_text(encoding="utf-8")
        assert "YSJohnson" not in text, f"Legacy GitHub owner found in {qml.name}"
        assert "UnipusAI-Helper" not in text, f"Legacy app name found in {qml.name}"
    theme_text = (qml_dir / "Theme.qml").read_text(encoding="utf-8")
    assert "readonly property bool dark: false" in theme_text, "Theme.qml must be light mode"
    print(f"[PASS] Branding, version (v{meta.APP_VERSION}), GitHub URL, and light-mode Theme.qml verified")


def check_qml_zero_warnings_and_preview() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QMetaObject, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from unipus_helper.model_api import ModelCheck
    from unipus_helper.native_ui import ConfigEditor

    QQuickStyle.setStyle("Basic")
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlApplicationEngine()
    warnings: list[str] = []
    engine.warnings.connect(lambda ws: warnings.extend(w.toString() for w in ws))

    qml_dir = ROOT / "unipus_helper" / "qml"
    model = ConfigEditor(ROOT / "config.example.json", first_run=True)
    engine.rootContext().setContextProperty("configEditor", model)
    engine.load(QUrl.fromLocalFile(str(qml_dir / "ConfigEditor.qml")))
    assert engine.rootObjects(), "ConfigEditor.qml failed to instantiate"
    window = engine.rootObjects()[0]

    def find_item(item, name):
        if item.objectName() == name:
            return item
        for child in item.childItems():
            match = find_item(child, name)
            if match is not None:
                return match
        return None

    draft_model = find_item(window.contentItem(), "field_model")
    test_button = find_item(window.contentItem(), "testApiButton")
    assert draft_model is not None and test_button is not None
    assert draft_model.setProperty("text", "Draft-Model")
    replies = []
    model.apiTestFinished.connect(lambda message, ok: replies.append((message, ok)))
    with patch("unipus_helper.native_ui.probe_model", return_value=ModelCheck(True, "离线接口通过")) as probe:
        assert QMetaObject.invokeMethod(test_button, "clicked")
        # Qt 需处理来自探针线程的排队信号。
        for _ in range(100):
            app.processEvents()
            if replies:
                break
            threading.Event().wait(0.01)
        probe.assert_called_once()
        assert probe.call_args.args[-1] == "Draft-Model"
    assert replies == [("离线接口通过", True)] and not model.apiTesting
    assert model.values["model"] != "Draft-Model", "API testing must leave saved configuration intact"
    for obj in engine.rootObjects():
        obj.close()
    assert not warnings, f"QML warnings detected: {warnings}"
    print("[PASS] Light-mode ConfigEditor.qml + ConfigForm.qml + ActionButton.qml loaded with 0 warnings")


def check_core_regressions() -> None:
    import UnipusAI_Helper as uah
    from selenium.common.exceptions import StaleElementReferenceException
    from unipus_helper.meta import APP_VERSION

    assert uah.APP_NAME == "UniCampusHelper"
    assert uah.APP_VERSION == APP_VERSION

    # 1. Strict word bank 3-tier alignment + repair hook (Unit 4 Activity 1)
    pe = uah.PublishAnswerExecutor.__new__(uah.PublishAnswerExecutor)
    filled: list[list[str]] = []
    pe._fill_required_inputs = lambda inputs, answers: (filled.append(list(answers)), True)[1]
    bank = ["beverage", "vendor", "verify", "flourishing", "hospitality", "fascinating", "embody", "bustling"]
    q = uah.Question(
        number=1, q_type=uah.QuestionType.FILL_IN, text="...", element=object(),
        inputs=[object()] * 8, blank_count=8, banked_options=bank, strict_word_bank=True,
    )
    ans = "1. beverage\n2. vendor\n3. verify\n4. flourish\n5. hospitality\n6. fascinating\n7. embody\n8. bustling"
    assert pe._fill_fill_in(q, ans) is True and filled[-1][3] == "flourishing"

    # 2. Stale file_input re-query after TTS synthesis (Unit 4 Roundtable discussion)
    class StaleInput:
        def send_keys(self, path):
            raise StaleElementReferenceException("stale")

    class FreshInput:
        def __init__(self):
            self.sent = []
        def send_keys(self, path):
            self.sent.append(path)

    fresh = FreshInput()
    class LiveGroup:
        def find_elements(self, by, sel):
            return [fresh] if "file" in sel else []

    pe2 = uah.PublishAnswerExecutor.__new__(uah.PublishAnswerExecutor)
    pe2.driver = SimpleNamespace(execute_script=lambda *a, **k: None, find_elements=lambda *a, **k: [])
    pe2._upload_file_count = lambda root: 0
    pe2._wait_upload_complete = lambda root, before, timeout=30.0: len(fresh.sent) == 1
    pe2._tts = lambda: None
    tmp_mp3 = Path(tempfile.gettempdir()) / "verify_release_dummy.mp3"
    tmp_mp3.write_bytes(b"ID3DUMMY")
    mq = uah.Question(
        number=1, q_type=uah.QuestionType.RECORD, text="Discuss.",
        element=LiveGroup(), group=LiveGroup(), inputs=[],
        record_items=[{"file_inputs": [StaleInput()], "accept": "audio/*", "has_record_btn": False, "prompt_hint": ""}],
    )
    with patch("unipus_helper.upload_builder.build_upload_attachment", return_value=("mp3", tmp_mp3)):
        assert pe2._fill_media_upload(mq, "Cultural heritage thrives with digital innovation.") is True
    assert len(fresh.sent) == 1

    # 3. Unit 5 retry skips prefetch + bottom-up explicit answer extraction
    solver = uah.AISolver.__new__(uah.AISolver)
    solver.stop_requested = threading.Event()
    solver._retry_hint = "已失败组合 ACD"
    assert solver._start_answer_prefetch([], "") is None
    assert solver._extract_single_answer("根据听力音频转写 1 核对：\n- A 正确\n答案：ACE", 1) == "ACE"

    # 4. Unit 4 Writing skill low-score (<85) reopen
    solver._publish_scope_url = "https://ucontent.unipus.cn/course-v3:unit4"
    solver._publish_record_route_matches = lambda url: True
    solver._current_directory_task = {
        "_record_groups": [{"kind": "task", "scored": True, "score": 60.7, "progress": 100}]
    }
    wg = {"type": "writing_group", "kind": "text", "element": object()}
    assert solver._publish_unrecorded_objective_group([wg]) is wg
    print("[PASS] Core Unit 4 & Unit 5 anti-regression suite passed")


def check_legacy_submission_receipt() -> None:
    import UnipusAI_Helper as uah

    review_page = "答题情况\n客观题 正确 (1/1)\n继续学习"
    task_url = "https://ucontent.unipus.cn/explorationpc_detail/pc.html#/lesson/exercise-1"
    other_task_url = task_url.replace("exercise-1", "exercise-2")

    def observe_review(before, after, url_after=task_url):
        page = SimpleNamespace(text=before)
        solver = uah.AISolver.__new__(uah.AISolver)
        solver.driver = SimpleNamespace(current_url=task_url, find_element=lambda *_: page)
        solver.stop_requested = SimpleNamespace(wait=lambda _: False)
        solver._should_stop = lambda: False
        solver._handle_confirm_dialog_detail = lambda: {}
        solver._get_submit_button_state = lambda: "gone"
        solver._read_task_score = lambda: None
        solver._has_redo_button = lambda: False
        solver._read_answer_analysis = lambda: ""
        solver._post_submit_diagnostic = lambda: ""
        solver._submission_unconfirmed = False

        unreviewed_url = solver._legacy_unreviewed_url()
        page.text = after
        solver.driver.current_url = url_after
        with patch("UnipusAI_Helper.time.time", side_effect=[0, 0, 2]), patch("builtins.print") as output:
            accepted = solver._wait_for_submit_complete(timeout=1, legacy_unreviewed_url=unreviewed_url)
        assert not accepted and solver._submission_unconfirmed, "Completion must await the platform record"
        return [call.args[0] for call in output.call_args_list]

    new_review_log = observe_review("提交", review_page)
    assert any("已显示本次答题情况" in line for line in new_review_log), new_review_log
    assert all("等待超时" not in line for line in new_review_log), new_review_log
    for invalid in (
        observe_review(review_page, review_page),
        observe_review("提交", "继续学习"),
        observe_review("提交", review_page, other_task_url),
    ):
        assert any("等待超时" in line for line in invalid), invalid
        assert all("已显示本次答题情况" not in line for line in invalid), invalid
    print("[PASS] Legacy result transition waits for the platform record")


def check_stopped_batch_verification() -> None:
    from unipus_helper.workbench_controller import WorkbenchController

    class Solver:
        def __init__(self):
            self.stopped = False
            self.calls = 0

        def request_stop(self):
            self.stopped = True

        def _should_stop(self):
            return self.stopped

        def process_selected_tabs(self, tasks, on_task_done, on_task_started):
            self.calls += 1
            on_task_started(tasks[0])
            on_task_done(tasks[0])
            progress = workbench.snapshot()["progress"]
            assert progress == {"completed": 0, "total": 2, "started": 1, "activeTask": "First"}
            workbench.stop()

    url = "https://course.example.invalid/directory"
    first, second = ({"id": str(idx), "l1_title": name, "status": "unknown", "section": "Section",
                       "_directory_url": url, "_unit_idx": idx, "_submission_unconfirmed": True}
                      for idx, name in enumerate(("First", "Second")))
    solver = Solver()
    with patch.object(threading.Thread, "start"):
        workbench = WorkbenchController(driver=object(), solver=solver)
    workbench._tasks = [first, second]
    workbench._selected = {first["id"], second["id"]}
    workbench._total = 2
    workbench._operation = "run"
    pending_record = dict(first, status="doing", _record_matched=False)
    platform_record = dict(first, status="done", _record_matched=True, score=90.0, progress=100)
    with patch.object(workbench._closed, "wait", return_value=False), \
         patch.object(workbench, "refresh_units_from_server", side_effect=[[pending_record], [platform_record]]) as refresh:
        workbench._run_operation("run", [first, second])
        assert refresh.call_args_list == [call(url, {first["_unit_idx"]})] * 2
    assert solver.calls == 1 and workbench._status == "已停止"
    assert workbench._completed == 1 and first["score"] == 90.0
    assert second["status"] == "unknown" and second["id"] in workbench._selected
    print("[PASS] After stopping, delayed platform records are confirmed without restarting any task")


def check_startup_diagnostics() -> None:
    from scripts import launch_workbench
    from unipus_helper.environment_checker import EnvironmentChecker

    checker = EnvironmentChecker()
    with patch.object(type(checker.browser), "detect_install_path", return_value="synthetic browser"), \
         patch("unipus_helper.environment_checker.shutil.which", return_value=None), \
         patch("unipus_helper.environment_checker.os.path.exists", return_value=False):
        assert checker.check_all(require_ffmpeg=False)
        assert any("FFmpeg" in warning for warning in checker.warnings)
        assert not checker.check_all()

    with tempfile.TemporaryDirectory(prefix="ucampus-startup-check-") as folder:
        root = Path(folder)
        main_script = root / "UnipusAI_Helper.py"
        main_script.write_text('raise ModuleNotFoundError("Missing UI", name="PySide6")', encoding="utf-8")
        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None), \
             patch.object(launch_workbench, "show_startup_error") as alert:
            assert launch_workbench.launch(root) == 1
        assert "PySide6" in alert.call_args.args[0] and "startup.log" in alert.call_args.args[0]
        boot_log = root / "logs" / "startup.log"
        assert "ModuleNotFoundError" in boot_log.read_text(encoding="utf-8")

        (root / "config.json").write_text(json.dumps({"api_key": "example-startup-secret"}), encoding="utf-8")
        main_script.write_text('raise RuntimeError("Driver failure example-startup-secret")', encoding="utf-8")
        with patch.object(launch_workbench, "show_startup_error") as alert:
            assert launch_workbench.launch(root) == 1
            assert "Driver failure" in alert.call_args.args[0] and "example-startup-secret" not in alert.call_args.args[0]
        previous = boot_log.read_text(encoding="utf-8")
        assert "example-startup-secret" not in previous
        main_script.write_text("raise SystemExit(0)", encoding="utf-8")
        with patch.object(launch_workbench, "show_startup_error") as alert:
            assert launch_workbench.launch(root) == 0
            alert.assert_not_called()
        assert boot_log.read_text(encoding="utf-8") == previous
    print("[PASS] Startup without required imports reports a diagnostic, and FFmpeg is optional for core startup")


def check_configuration_and_run_log() -> None:
    from PySide6.QtGui import QGuiApplication
    from unipus_helper.config_store import read_config, write_config
    from unipus_helper.diagnostics import RedactingFormatter, redact_log
    from unipus_helper.native_ui import NativeBridge
    from unipus_helper.workbench_controller import WorkbenchController

    with tempfile.TemporaryDirectory(prefix="ucampus-config-check-") as folder:
        config = Path(folder) / "config.json"
        previous = {"api_key": "synthetic-model-key", "voice_clone_api_key": "synthetic-voice-key"}
        config.write_text(json.dumps(previous), encoding="utf-8-sig")
        assert read_config(config) == previous
        with patch("unipus_helper.config_store.os.replace", side_effect=PermissionError("simulated lock")):
            try:
                write_config(config, {"model": "custom"})
            except PermissionError:
                pass
            else:
                raise AssertionError("Expected configuration replacement failure")
        assert read_config(config) == previous and not list(config.parent.glob(".config-*.tmp"))
        write_config(config, dict(previous, model="custom"))
        assert read_config(config) == dict(previous, model="custom")

        app = QGuiApplication.instance() or QGuiApplication([])
        logger = logging.Logger("synthetic-run")
        file_handler = logging.FileHandler(Path(folder) / "run.log", encoding="utf-8")
        file_handler.setFormatter(RedactingFormatter("%(message)s"))
        logger.addHandler(file_handler)
        redact_log.add_config(previous)
        try:
            with patch.object(threading.Thread, "start"):
                controller = WorkbenchController(app_globals={"logger": logger})
            logger.info("Oldest entry still present")
            for index in range(501):
                logger.info("Line %d", index)
            raw_line = ("api_key='synthetic-model-key' voice: synthetic-voice-key "
                        "https://gateway.invalid/?ticket=ST-abcd&view=menu Bearer test-header")
            logger.warning(raw_line)
            controller._add_log(raw_line)
            bridge = NativeBridge(controller)
            try:
                bridge.copyLogs()
                copied = app.clipboard().text()
                on_disk = controller.current_run_log()
                ui_line = controller.snapshot()["logs"][-1]["message"]
                assert "Oldest entry still present" in copied and "Line 500" in copied
                for text in (on_disk, copied, ui_line):
                    assert "view=menu" in text
                    assert all(secret not in text for secret in (*previous.values(), "ST-abcd", "test-header"))
            finally:
                bridge.timer.stop()
        finally:
            file_handler.close()
    print("[PASS] Atomic configuration failure leaves old JSON intact, and disk, UI and full log exports redact credentials")


def main() -> int:
    from unipus_helper.meta import APP_VERSION

    check_py_compile()
    check_metadata_and_branding()
    check_qml_zero_warnings_and_preview()
    check_core_regressions()
    check_legacy_submission_receipt()
    check_stopped_batch_verification()
    check_startup_diagnostics()
    check_configuration_and_run_log()
    print(f"=== ALL UniCampusHelper v{APP_VERSION} RELEASE CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
