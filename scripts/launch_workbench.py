"""Start the Windows workbench with diagnostics available before imports."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
import os
import runpy
import sys
import traceback


def show_startup_error(message: str) -> None:
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "UniCampusHelper 启动失败", 0x10)


def report_startup_error(root: Path, message: str, details: str = "") -> None:
    try:
        from unipus_helper.diagnostics import redact_log
    except ImportError:
        # A damaged source directory may also prevent loading the formatter.
        message = "工作台启动失败。请检查项目文件是否完整。"
        details = ""
    else:
        redact_log.add_config_file(root / "config.json")
        message, details = redact_log(message), redact_log(details)
    log_path = root / "logs" / "startup.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if log_path.exists() and log_path.stat().st_size > 256_000 else "a"
        with log_path.open(mode, encoding="utf-8") as log:
            log.write(f"\n{datetime.now():%Y-%m-%d %H:%M:%S}\n{message}\n{details}\n")
    except OSError:
        show_startup_error(message + "\n\n启动日志写入失败。请确认安装目录可写。")
        return
    show_startup_error(f"{message}\n\n启动日志\n{log_path}")


def launch(root: Path) -> int:
    if sys.version_info < (3, 11):
        report_startup_error(root, "需要 Python 3.11 或更新版本。推荐使用 Python 3.12 或 3.13 创建 .venv。")
        return 1

    sys.path.insert(0, str(root))
    old_argv = sys.argv[:]
    sys.argv.append("--desktop-launch")
    try:
        # pythonw 的三个标准流可以为 None。程序日志仍由工作台的处理器记录。
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with redirect_stdout(sys.stdout or sink), redirect_stderr(sys.stderr or sink):
                runpy.run_path(str(root / "UnipusAI_Helper.py"), run_name="__main__")
    except SystemExit as exc:
        if exc.code in (None, 0):
            return 0
        report_startup_error(root, f"工作台启动中断（退出码 {exc.code}）。")
        return 1
    except ModuleNotFoundError as exc:
        repair = ("请重新下载并完整解压 Windows 免安装包。" if sys.executable and Path(sys.executable).parent.name == ".runtime"
                  else "请双击「安装-UniCampusHelper.cmd」修复基础依赖。")
        report_startup_error(
            root,
            f"缺少 Python 模块 {exc.name or '（未知）'}。{repair}",
            traceback.format_exc(),
        )
        return 1
    except Exception as exc:
        report_startup_error(root, str(exc)[:320] or type(exc).__name__, traceback.format_exc())
        return 1
    finally:
        sys.argv[:] = old_argv
        sys.path.remove(str(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(launch(Path(__file__).resolve().parent.parent))
