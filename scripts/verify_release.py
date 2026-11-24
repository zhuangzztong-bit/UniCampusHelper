"""UniCampusHelper v4.0.0 release verification suite.

Checks:
1. All Python modules compile cleanly (py_compile).
2. Centralized metadata (APP_NAME, APP_VERSION, GITHUB_DISPLAY) is consistent.
3. Light-mode QML theme loads with 0 warnings and zero legacy dark hardcodes.
4. Core Unit 4 (ipublish) & Unit 5 (ucontent) anti-regression checks pass.
"""

from __future__ import annotations

import os
import py_compile
import re
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
venv_site = ROOT / ".venv" / "Lib" / "site-packages"
if venv_site.is_dir():
    sys.path.insert(0, str(venv_site))


def check_py_compile() -> None:
    files = [ROOT / "UnipusAI_Helper.py"] + sorted((ROOT / "unipus_helper").glob("*.py"))
    for path in files:
        py_compile.compile(str(path), doraise=True)
    print(f"[PASS] py_compile: {len(files)} Python modules compiled cleanly")


def check_metadata_and_branding() -> None:
    from unipus_helper import meta

    assert meta.APP_NAME == "UniCampusHelper"
    assert meta.APP_VERSION == "4.0.0"
    assert meta.GITHUB_OWNER == "zhuangzztong-bit"
    assert meta.GITHUB_DISPLAY == "github.com/zhuangzztong-bit/UniCampusHelper"

    qml_dir = ROOT / "unipus_helper" / "qml"
    for qml in sorted(qml_dir.glob("*.qml")):
        text = qml.read_text(encoding="utf-8")
        assert "YSJohnson" not in text, f"Legacy GitHub owner found in {qml.name}"
        assert "UnipusAI-Helper" not in text, f"Legacy app name found in {qml.name}"
    theme_text = (qml_dir / "Theme.qml").read_text(encoding="utf-8")
    assert "readonly property bool dark: false" in theme_text, "Theme.qml must be light mode"
    print("[PASS] Branding, version (v4.0.0), GitHub URL, and light-mode Theme.qml verified")


def check_qml_zero_warnings_and_preview() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
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
    for obj in engine.rootObjects():
        obj.close()
    assert not warnings, f"QML warnings detected: {warnings}"
    print("[PASS] Light-mode ConfigEditor.qml + ConfigForm.qml + ActionButton.qml loaded with 0 warnings")


def check_core_regressions() -> None:
    import UnipusAI_Helper as uah
    from selenium.common.exceptions import StaleElementReferenceException

    assert uah.APP_NAME == "UniCampusHelper"
    assert uah.APP_VERSION == "4.0.0"

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


def main() -> int:
    check_py_compile()
    check_metadata_and_branding()
    check_qml_zero_warnings_and_preview()
    check_core_regressions()
    print("=== ALL UniCampusHelper v4.0.0 RELEASE CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
