from contextlib import contextmanager
import gc
import os
import sys
import tempfile
import threading
import traceback
import urllib.error
import urllib.request
from typing import Dict, Optional

from .paths import PROJECT_ROOT


class _NullStream:
    """pythonw.exe 下 sys.stderr 为 None 时，给 whisper/tqdm 兜底的空流。"""

    def write(self, data: str) -> int:
        return len(data) if data else 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


_STDERR_LOCK = threading.Lock()
_STDERR_NULL_DEPTH = 0


@contextmanager
def _ensure_stderr():
    global _STDERR_NULL_DEPTH
    installed = False
    with _STDERR_LOCK:
        if sys.stderr is None:
            sys.stderr = _NullStream()
            _STDERR_NULL_DEPTH = 1
            installed = True
        elif _STDERR_NULL_DEPTH > 0:
            _STDERR_NULL_DEPTH += 1
            installed = True
    try:
        yield
    finally:
        if installed:
            with _STDERR_LOCK:
                _STDERR_NULL_DEPTH = max(0, _STDERR_NULL_DEPTH - 1)
                if _STDERR_NULL_DEPTH == 0 and isinstance(sys.stderr, _NullStream):
                    sys.stderr = None


class AudioTranscriber:
    """
    使用本地 Whisper 进行语音识别
    """

    def __init__(self):
        self.local_model = None
        self._transcript_cache: Dict[str, str] = {}

    @staticmethod
    def _print_exception(prefix: str, exc: Exception) -> None:
        """完整输出异常信息与堆栈，方便定位问题"""
        print(f"{prefix}: {exc}")
        tb = traceback.format_exc().rstrip()
        if tb and tb != "NoneType: None":
            print(tb)

    @staticmethod
    def _is_writable_dir(path: str) -> bool:
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, f".whisper_write_probe_{os.getpid()}")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)
            return True
        except OSError:
            return False

    @classmethod
    def _resolve_cache_dir(cls, model_name: str = "base") -> Optional[str]:
        """解析可写的 Whisper 模型缓存目录，避开受限环境下 ~/.cache 拒绝访问的问题。"""
        env_dir = (os.environ.get("WHISPER_CACHE_DIR") or "").strip()
        project_cache = str(PROJECT_ROOT / "logs" / "whisper_cache")
        temp_cache = os.path.join(tempfile.gettempdir(), "whisper_cache")
        xdg_cache = (os.environ.get("XDG_CACHE_HOME") or "").strip()
        default_root = xdg_cache or os.path.join(os.path.expanduser("~"), ".cache")
        default_whisper = os.path.join(default_root, "whisper")

        # 优先复用已有且所在目录可写的本地模型，避免重复下载
        for candidate in [env_dir, project_cache, default_whisper]:
            if not candidate:
                continue
            model_file = os.path.join(candidate, f"{model_name}.pt")
            if os.path.isfile(model_file) and cls._is_writable_dir(candidate):
                return candidate

        candidates = []
        for candidate in [env_dir, project_cache, temp_cache, default_whisper]:
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        for candidate in candidates:
            if cls._is_writable_dir(candidate):
                return candidate
        return None

    def _init_local_model(self):
        """初始化本地 Whisper 模型"""
        try:
            # 限制 PyTorch/OpenMP 线程数，降低 Windows 虚拟内存峰值占用，避免高负载下加载模型 OOM
            os.environ.setdefault("OMP_NUM_THREADS", "2")
            os.environ.setdefault("MKL_NUM_THREADS", "2")
            os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
            gc.collect()
            # 进到需要转写的页面才导入，省掉整轮只播放的练习里的启动开销
            import whisper
            print("       加载 Whisper 本地模型 (base)...")
            # 可选: tiny, base, small, medium, large
            # base 是速度与准确率的平衡选择
            cache_dir = self._resolve_cache_dir("base")
            if cache_dir:
                print(f"       Whisper 缓存目录: {cache_dir}")
                if not os.path.isfile(os.path.join(cache_dir, "base.pt")):
                    print("       首次使用正在下载 Whisper base 模型（约 139 MB），请稍候...")
            with _ensure_stderr():
                self.local_model = whisper.load_model("base", download_root=cache_dir)
            print("       本地模型加载完成")
        except ImportError:
            print("       未安装 whisper，请运行: pip install openai-whisper")
            raise
        except Exception as e:
            self._print_exception("       加载本地模型失败", e)
            raise

    def _ensure_local_model(self) -> bool:
        """真要转写时才加载模型。"""
        if self.local_model is not None:
            return True
        try:
            self._init_local_model()
        except Exception:
            return False
        return self.local_model is not None

    def release_local_model(self) -> None:
        """释放本地模型，下次要转写时再加载。"""
        if self.local_model is None:
            return
        self.local_model = None
        gc.collect()
        print("       已释放 Whisper 模型")

    def download_audio(self, audio_url: str) -> Optional[str]:
        """下载远程音频到临时文件并返回路径（可并发预取，调用方或 transcribe 负责清理）。"""
        if not audio_url or audio_url in self._transcript_cache:
            return None
        audio_path = None
        try:
            with urllib.request.urlopen(audio_url, timeout=30) as response, \
                    tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(response.read())
                audio_path = f.name
            return audio_path
        except urllib.error.URLError as e:
            self._print_exception("       下载音频失败", e)
        except Exception as e:
            self._print_exception("       下载音频异常", e)
        if audio_path:
            try:
                os.unlink(audio_path)
            except OSError:
                pass
        return None

    def transcribe(self, audio_url: str, language: str = "en",
                   predownloaded_path: Optional[str] = None) -> str:
        """
        下载音频并转录为文字

        Args:
            audio_url: 音频文件URL
            language: 语言代码，默认英语 en，中文 zh
            predownloaded_path: 已预先下载到本地的临时音频路径（可选）

        Returns:
            识别出的文字
        """
        # 检查缓存
        if audio_url in self._transcript_cache:
            print(f"       使用缓存的识别结果")
            if predownloaded_path:
                try:
                    os.unlink(predownloaded_path)
                except OSError:
                    pass
            return self._transcript_cache[audio_url]

        audio_path = predownloaded_path

        try:
            if not self._ensure_local_model():
                return ""
            if not audio_path or not os.path.isfile(audio_path):
                print(f"      【下载】获取音频...")
                audio_path = self.download_audio(audio_url)
                if not audio_path:
                    return ""

            print(f"        开始识别...")
            text = self._transcribe_local(audio_path, language)

            if text:
                self._transcript_cache[audio_url] = text
                print(f"       识别成功 ({len(text)} 字符)")
                print(f"       音频识别结果{text}")

            return text or ""

        except Exception as e:
            self._print_exception("       识别失败", e)
            return ""
        finally:
            if audio_path:
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass

    def _transcribe_local(self, audio_path: str, language: str) -> Optional[str]:
        """使用本地 Whisper 模型识别"""
        if self.local_model is None:
            print("       本地模型未加载")
            return None

        with _ensure_stderr():
            result = self.local_model.transcribe(
                audio_path,
                language=language,
                fp16=False  # CPU 运行设为 False
            )

        return result["text"].strip() if result else None
