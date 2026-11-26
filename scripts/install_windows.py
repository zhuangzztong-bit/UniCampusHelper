"""Install dependencies beside the Windows source or portable distribution."""

import argparse
import msvcrt
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime


INDEXES = ("https://pypi.tuna.tsinghua.edu.cn/simple/", "https://pypi.org/simple")
CORE_CHECK = "import selenium, openai, numpy; from PySide6 import QtCore, QtQml, QtQuick"
AUDIO_CHECK = "import whisper, edge_tts, sounddevice, av"


def install(root: Path, *, audio: bool, start: bool, settings: bool) -> int:
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    scratch = logs / "install-tmp"
    scratch.mkdir(exist_ok=True)
    env = dict(os.environ, TEMP=str(scratch), TMP=str(scratch), PYTHONUTF8="1")
    env["PIP_CONFIG_FILE"] = os.devnull
    log_path = logs / "install.log"
    executable = root / ".venv" / "Scripts" / "python.exe"
    check = f"{CORE_CHECK}; {AUDIO_CHECK}" if audio else CORE_CHECK
    requirements = root / ("requirements.txt" if audio else "requirements-core.txt")

    with (logs / "install.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            print("安装正在进行。请等待另一个安装窗口完成。")
            return 1

        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n{datetime.now():%Y-%m-%d %H:%M:%S} {requirements.name}\n")
            log.flush()

            def run(executable_args, timeout=60):
                try:
                    result = subprocess.run(executable_args, cwd=root, env=env, stdout=log,
                                            stderr=subprocess.STDOUT, timeout=timeout)
                    return result.returncode == 0
                except (OSError, subprocess.TimeoutExpired) as exc:
                    log.write(f"{type(exc).__name__}\n")
                    log.flush()
                    return False

            if not executable.is_file():
                print("正在创建项目内的 Python 环境…", flush=True)
                if not run([sys.executable, "-m", "venv", str(executable.parent.parent)], 180):
                    print(f"创建 Python 环境失败。请确认解压目录可写。安装日志在 {log_path}")
                    return 1

            if not run([str(executable), "-B", "-c", check]):
                if not run([str(executable), "-m", "pip", "--version"]):
                    if not run([str(executable), "-m", "ensurepip", "--upgrade"], 120):
                        print(f"初始化 pip 失败。安装日志在 {log_path}")
                        return 1

                for index in INDEXES:
                    print(f"正在安装{'语音支持' if audio else '基础依赖'}，来源 {index}", flush=True)
                    print(f"安装输出写入 {log_path}", flush=True)
                    args = [str(executable), "-m", "pip", "--isolated", "--disable-pip-version-check",
                            "install", "--no-input", "--no-cache-dir", "--progress-bar", "off", "--timeout", "12",
                            "--retries", "1", "--index-url", index, "-r", str(requirements)]
                    if run(args, 1200 if audio else 240) and run([str(executable), "-B", "-c", check]):
                        break
                else:
                    print(f"依赖未能安装或加载。请查看 {log_path}，然后重新运行安装入口。")
                    return 1

            (executable.parent.parent / ".ucampus-ready").touch()

    print("语音依赖检查通过。" if audio else "基础环境检查通过。")
    if audio and not (shutil.which("ffmpeg") or (root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe").is_file()):
        print("听力转写需要 FFmpeg。可将 ffmpeg.exe 和 ffprobe.exe 放到 tools\\ffmpeg\\bin，参见 README。")
    if start:
        windowed = executable.with_name("pythonw.exe")
        args = [str(windowed if windowed.is_file() else executable), "-B", str(root / "scripts" / "launch_workbench.py")]
        if settings:
            args.append("--settings")
        subprocess.Popen(args, cwd=root)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", action="store_true")
    parser.add_argument("--settings", action="store_true")
    parser.add_argument("--no-start", action="store_true", help="Install dependencies and exit")
    args = parser.parse_args()
    try:
        return install(Path(__file__).resolve().parents[1], audio=args.audio, start=not args.no_start,
                       settings=args.settings)
    except OSError as exc:
        print(f"安装目录或 Python 环境不可用 · {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
