import hashlib, json, logging, os, sys, random, re, tempfile, threading, time, base64
try:
    import winsound
except ImportError:
    class _MockWinsound:
        @staticmethod
        def MessageBeep(*args, **kwargs):
            try:
                print('\a', end='', flush=True)
            except Exception:
                pass
    winsound = _MockWinsound()
import shutil
import subprocess
import urllib.request
import queue
from abc import ABC, abstractmethod
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from glob import glob
from typing import List, Optional, Dict, Any, Tuple, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from unipus_helper.paths import PROJECT_ROOT

BASE_DIR = str(PROJECT_ROOT)


def ensure_writable_temp_dir() -> str:
    """确保 TEMP/TMP 指向可写目录。

    Windows 上系统临时目录可能不可写（受限用户、策略重定向、沙箱环境等），
    此时 Chromium/Edge 启动会报
    "cannot create temp dir for unpacking extensions"。
    这里按「系统 TEMP/TMP → tempfile 默认 → 程序目录 logs/tmp」顺序挑第一个可写的，
    并同步写回 os.environ，供后续启动的浏览器进程继承。
    """
    candidates = []
    for name in ('TEMP', 'TMP'):
        value = os.environ.get(name)
        if value and value not in candidates:
            candidates.append(value)
    local_appdata = os.environ.get('LOCALAPPDATA')
    if local_appdata:
        app_temp = os.path.join(local_appdata, 'UniCampusHelper', 'tmp')
        if app_temp not in candidates:
            candidates.append(app_temp)
    program_temp = os.path.join(BASE_DIR, 'logs', 'tmp')
    if program_temp not in candidates:
        candidates.append(program_temp)
    workspace_temp = os.path.join(BASE_DIR, '.tmp')
    if workspace_temp not in candidates:
        candidates.append(workspace_temp)

    original_temp = os.environ.get('TEMP', '')
    for candidate in candidates:
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, f'.ucampus_write_probe_{os.getpid()}')
            with open(probe, 'w', encoding='utf-8') as handle:
                handle.write('ok')
            os.remove(probe)
        except Exception:
            continue
        os.environ['TEMP'] = candidate
        os.environ['TMP'] = candidate
        try:
            tempfile.tempdir = candidate
        except Exception:
            pass
        if original_temp and os.path.abspath(original_temp) != os.path.abspath(candidate):
            print(f" 系统临时目录不可写，已切换到: {candidate}")
        return candidate
    return ''


# 必须先确保临时目录可写并限制数值库线程数，再导入 whisper/torch/numpy：
# 系统 temp 不可写时 torch 的 DLL 初始化会直接抛出 WinError 1114；
# 限制 OpenMP/MKL/OpenBLAS 线程数可避免 16GB 内存机器在高负载下虚拟内存不足。
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
ensure_writable_temp_dir()

from unipus_helper.audio_recognizer import AudioTranscriber
from unipus_helper.browser import DEFAULT_BROWSER, driver_api, get_spec, normalize_browser
from unipus_helper.course_catalog import CourseCatalogMixin
from unipus_helper.environment_checker import EnvironmentChecker
from unipus_helper.meta import APP_NAME, APP_VERSION, GITHUB_DISPLAY, GITHUB_URL
from unipus_helper.task_grading import MIN_TASK_SCORE, confirmed_score, needs_live_conversation
from unipus_helper.model_api import (
    MODEL_REQUEST_TIMEOUT, MODEL_RETRY_BUDGET, describe_model_error, make_model_client,
    model_error_retryable, model_error_status,
)
from selenium.common.exceptions import (NoSuchDriverException, NoSuchElementException,
                                        NoSuchFrameException, TimeoutException,
                                        StaleElementReferenceException, SessionNotCreatedException,
                                        WebDriverException)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

gui_log_queue = queue.Queue()

DEBUG_MODE = False

DEFAULT_LOGIN_URL = "https://uai.unipus.cn/sso/index.html?service=https%3A%2F%2Fucloud.unipus.cn%2Fhome"
UCLOUD_HOME_URL = "https://ucloud.unipus.cn/home"

# setup_logging() 之前就要有可用的 logger（测试、模块被 import 时都会走到）
logger = logging.getLogger('UCampusBot')


def setup_logging():
    """配置日志系统：控制台简洁输出 + 文件详细记录 + UI队列同步"""

    def clean_all_logs(log_dir, keep: int = 10):
        """保留最近 keep 份运行日志，清理更早的旧日志（避免重启后丢失排错证据）。"""
        try:
            log_pattern = os.path.join(log_dir, 'ucampus_*.log')
            log_files = sorted(glob(log_pattern), key=os.path.getmtime, reverse=True)
            for old_file in log_files[keep:]:
                try:
                    os.remove(old_file)
                except Exception:
                    pass
        except Exception:
            pass

    logger = logging.getLogger('UCampusBot')
    if logger.handlers:
        # 已经初始化过：重复调用会把 sys.stdout（此时已被 PrintRedirector 接管）
        # 再包一层 StreamHandler，日志会自我递归直到 RecursionError
        return logger
    logger.setLevel(logging.DEBUG)
    logger.handlers = []
    log_dir = os.path.join(BASE_DIR, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    clean_all_logs(log_dir)
    log_file = os.path.join(log_dir, f'ucampus_{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}.log')

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] [%(funcName)s:%(lineno)d]\n%(message)s\n',
        datefmt='%H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    class ConsoleFilter(logging.Filter):
        def filter(self, record):
            if record.levelno >= logging.ERROR:
                record.msg = f" {record.msg}"
            return True

    console_handler.addFilter(ConsoleFilter())
    console_formatter = logging.Formatter('%(message)s')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    class PrintRedirector:
        def __init__(self, logger, level=logging.INFO):
            self.logger = logger
            self.level = level
            self.buffer = ""

        def write(self, text):
            if text.strip():
                if any(x in text for x in ['Error', 'Exception', 'Traceback']):
                    self.logger.error(text.strip())
                elif 'Warning' in text:
                    self.logger.warning(text.strip())
                else:
                    self.logger.info(text.strip())
                gui_log_queue.put(text.strip())

        def flush(self):
            pass

    sys.stdout = PrintRedirector(logger)
    return logger


@dataclass(frozen=True)
class Config:
    """不可变配置类"""
    url: str
    username: str
    password: str
    api_key: str
    token_full: str
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    # 录音题（Exercise 2 这类没有原声的自由作答）用 TTS 合成语音
    tts_engine: str = "auto"  # auto / edge / sapi / off
    tts_voice: str = ""       # 留空按引擎给默认英语音色
    tts_proxy: str = ""       # edge-tts 走代理时填，如 http://127.0.0.1:7890
    # 题目区域截图一并发给支持识图的模型（如 qwen-vl、gpt-4o），默认关闭
    screenshot: bool = False
    # 浏览器后端：edge（默认）/ chrome，见 unipus_helper/browser.py
    browser: str = DEFAULT_BROWSER
    # 新版视频播放器原生倍速（仅播放器界面开放倍速控件时生效）：1.0 / 1.25 / 1.5 / 2.0
    video_playback_rate: float = 1.0
    # 用户自定义音色克隆（本地 OpenVoice V2 或可选云端 API）
    voice_clone_enabled: bool = False
    voice_clone_mode: str = "local"
    voice_clone_profile: str = "my_voice"
    voice_clone_tau: float = 0.35
    voice_clone_base_gender: str = "auto"
    voice_clone_rate: str = "+0%"
    voice_clone_convert_reference: bool = True
    voice_clone_api_url: str = ""
    voice_clone_api_key: str = ""
    voice_clone_api_model: str = "FunAudioLLM/CosyVoice2-0.5B"

    @classmethod
    def from_json(cls, path: str = "config.json") -> "Config":
        with open(path, "r", encoding="UTF-8") as f:
            data = json.load(f)
        global DEBUG_MODE
        DEBUG_MODE = data.get("debug_mode", False)
        try:
            vc_tau = float(data.get("voice_clone_tau", 0.35))
        except (TypeError, ValueError):
            vc_tau = 0.35
        try:
            v_rate = float(data.get("video_playback_rate", 1.0))
            if v_rate not in (1.0, 1.25, 1.5, 2.0):
                v_rate = 1.0
        except (TypeError, ValueError):
            v_rate = 1.0
        return cls(
            url=data.get("url") or DEFAULT_LOGIN_URL,
            username=data.get("username"),
            password=data.get("password"),
            token_full=data.get("token_full") or "",
            api_key=data.get("api_key"),
            base_url=data.get("base_url", "https://api.moonshot.cn/v1"),
            model=data.get("model", "kimi-k2-turbo-preview"),
            temperature=data.get("temperature", 0.3),
            max_tokens=data.get("max_tokens", 32768),
            tts_engine=(data.get("tts_engine") or "auto").strip(),
            tts_voice=(data.get("tts_voice") or "").strip(),
            tts_proxy=(data.get("tts_proxy") or "").strip(),
            screenshot=bool(data.get("screenshot", False)),
            browser=normalize_browser(data.get("browser")),
            video_playback_rate=v_rate,
            voice_clone_enabled=bool(data.get("voice_clone_enabled", False)),
            voice_clone_mode=(data.get("voice_clone_mode") or "local").strip().lower(),
            voice_clone_profile=(data.get("voice_clone_profile") or "my_voice").strip(),
            voice_clone_tau=max(0.10, min(0.85, vc_tau)),
            voice_clone_base_gender=(data.get("voice_clone_base_gender") or "auto").strip().lower(),
            voice_clone_rate=(data.get("voice_clone_rate") or "+0%").strip(),
            voice_clone_convert_reference=bool(data.get("voice_clone_convert_reference", True)),
            voice_clone_api_url=(data.get("voice_clone_api_url") or "").strip(),
            voice_clone_api_key=(data.get("voice_clone_api_key") or "").strip(),
            voice_clone_api_model=(data.get("voice_clone_api_model") or "FunAudioLLM/CosyVoice2-0.5B").strip(),
        )


def _parse_auth_token(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return (legacy __token JSON, JWT) for either supported config format."""
    parsed = raw if isinstance(raw, dict) else None
    token = "" if parsed is not None else str(raw or "").strip()

    for _ in range(2):
        if parsed is not None or not token:
            break
        try:
            decoded = json.loads(token)
        except (TypeError, ValueError):
            if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
                token = token[1:-1].strip()
                continue
            break
        if isinstance(decoded, str):
            token = decoded.strip()
        else:
            parsed = decoded

    if isinstance(parsed, dict):
        token_info = parsed.get("tokenInfo")
        jwt = parsed.get("jwt") or parsed.get("Authorization")
        if not jwt and isinstance(token_info, dict):
            jwt = token_info.get("jwt")
        if isinstance(jwt, str) and jwt.lower().startswith("bearer "):
            jwt = jwt[7:].strip()
        if not isinstance(jwt, str) or jwt.count(".") != 2:
            return None, None

        legacy_keys = {"rt", "jwt", "rtExpire", "jwtExpire"}
        legacy = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) \
            if legacy_keys.issubset(parsed) else None
        return legacy, jwt

    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token.count(".") == 2 and all(token.split(".")):
        return None, token
    return None, None


class QuestionType(Enum):
    """题目类型"""
    SINGLE_CHOICE = auto()
    MULTIPLE_CHOICE = auto()
    FILL_IN = auto()
    TEXT = auto()
    SORTING = auto()
    VOCABULARY_FLASHCARD = auto()
    BANKED_CLOZE = auto()
    VOCABULARY_TEST = auto()  # 词汇测试（英汉互译）
    VIDEO = auto()  # 纯视频页面
    AUDIO = auto()  # 纯听音频页面，需要播放至自然结束
    DISCUSSION_BOARD = auto()
    SELF_CHECK = auto()
    MY_VOICE_TEXT = auto()
    DROPDOWN_SELECT = auto()
    LISTENING_FILL_IN = auto()
    LISTENING_CHOICE = auto()
    VIDEO_CHOICE = auto()
    TRUE_FALSE = auto()  # 判断题（course-v3 播放器 T/F）
    MATCHING = auto()  # 人物 A-F ↔ 陈述 的匹配题（空里只填字母）
    RECORD = auto()  # 跟读/朗读录音题（course-v2 播放器 oral-study 题型）
    ORAL_ALOUD = auto()  # 整段朗读：点 start 后强制准备再强制录音
    LINK_MATCH = auto()  # 连线匹配（course-v3 播放器 React Flow 拖拽连线，text_match_group）
    PARAGRAPH_MATCH = auto()  # 段落信息匹配（Skimming and scanning，空里只填段落字母 A-O）


@dataclass
class Option:
    """选项"""
    letter: str
    text: str
    element: Any
    is_selected: bool = False


@dataclass
class Question:
    """题目"""
    number: int
    text: str
    q_type: QuestionType
    element: Any
    options: List[Option] = field(default_factory=list)
    inputs: List[Any] = field(default_factory=list)
    banked_options: List[str] = field(default_factory=list)
    banked_blanks: List[Dict] = field(default_factory=list)
    directions: str = ""
    group: Any = None  # course-v3 题目组容器（提交单位）
    needs_upload: bool = False  # course-v3 需要上传附件的题型
    blank_count: int = 0  # course-v3 下拉空位可能没有 input
    record_items: List[Dict] = field(default_factory=list)  # 录音题：逐条 {index,url,name,duration,text,element}
    link_items: List[Dict] = field(default_factory=list)  # 连线匹配：逐节点 {i,id,side,text}，right=提示，left=选项
    needs_correction: bool = False
    strict_word_bank: bool = False  # course-v3 选词题按页面词项原样作答

    @property
    def answer_count(self) -> int:
        return self.blank_count or len(self.inputs)

    def is_interactive(self) -> bool:
        """是否有交互元素"""
        if self.q_type in [QuestionType.VIDEO, QuestionType.AUDIO,
                           QuestionType.VOCABULARY_FLASHCARD,  # 闪卡需要交互
                           QuestionType.DISCUSSION_BOARD,
                           QuestionType.RECORD,
                           QuestionType.ORAL_ALOUD,
                           QuestionType.SELF_CHECK]:
            return True
        return bool(self.options or self.inputs or self.banked_blanks)

    @property
    def is_phrase_mode(self) -> bool:
        """检测是短语填空还是单词填空"""
        if not self.banked_options:
            return False
        phrase_count = sum(1 for opt in self.banked_options if ' ' in opt.strip() or len(opt) > 15)
        return phrase_count / len(self.banked_options) > 0.3

    @property
    def needs_speech_script(self) -> bool:
        """录音题里有没有既无原声、也无指定朗读文本的条目。

        Vocabulary / Exercise 1 的跟读题自带参考音频，直接把原声喂进麦克风即可；
        Exercise 2 这类主观题只有一个计时录音按钮，没有原声，只能先让模型写口语稿
        再用 TTS 合成。
        """
        items = self.record_items or []
        if not items:
            return False
        return any(not (item.get('url') or '').strip()
                   and not (item.get('text') or '').strip() for item in items)


CJK_HINT_PATTERN = re.compile(r'[（(]\s*([\u4e00-\u9fa5][^（）()]{0,40}?)\s*[）)]')
CJK_CHAR_PATTERN = re.compile(r'[\u4e00-\u9fa5]')

# Expressions in use / Words in use：Directions 要求「用所给表达填空、按需变形、每个只用一次」
EXPRESSION_BANK_PATTERN = re.compile(
    r'expressions?\s+given\s+below|fill\s+in\s+the\s+blanks\s+with\s+the\s+expressions',
    re.I,
)
EXPRESSION_FORM_PATTERN = re.compile(r'change\s+the\s+form', re.I)
EXPRESSION_ONCE_PATTERN = re.compile(r'each\s+expression|expressions?\s+can\s+be\s+used', re.I)

EXPRESSION_BANK_HINTS = [
    "【短语变形填空】请输出按句子语法变形后的完整表达，不要照抄词库原形。",
    "1. 不规则动词整体变化：dig up→dug up、take up→took up、come to the rescue→came to the rescue。",
    "2. 空格若缺助动词或 be 动词，要一并写进空里：will get away from、are equipped with（be 随主语变位）。",
    "3. take ... for granted 的 ... 要换成句子需要的宾语；put one's feet up 的 one's 要按主语换成 their / his / her。",
    "4. 词库里每个表达恰好用一次：不要重复、不要漏用；先通读全部句子做全局分配，再逐空给答案。",
    "5. 每个空可以包含多个单词，答案里不要带括号注释、词形解释或其它说明。",
    "6. 只有空位在句首时首字母才大写：At the heart of / Early on / In the course of；句中一律保持小写：distinguish / come up with / wrestled with / rather than / refrain from。平台大小写也计分。",
]


def is_expression_bank_question(q: "Question") -> bool:
    """Directions 明确要求「用所给表达填空并按需变形」的短语题。

    用 Directions 文本判定，不依赖词库容器的 class——同一题型在不同教材里
    渲染结构并不一致，靠 DOM 特征识别容易整题漏判、退化成普通填空。
    """
    text = f"{getattr(q, 'directions', '') or ''}\n{getattr(q, 'text', '') or ''}"
    if EXPRESSION_BANK_PATTERN.search(text):
        return True
    # 只有「change the form」+「each expression ... only once」同时出现才算，
    # 避免把 Words in use（单词变形）也当成多词短语题。
    return bool(EXPRESSION_FORM_PATTERN.search(text)
                and EXPRESSION_ONCE_PATTERN.search(text))

# Cloze 下拉选择题：不同教材用的下拉组件不一样（Ant Select / 自绘 / 原生 select），
# 但占位文案是统一的「点击选择」。优先按文案定位，class 只作为兜底，避免整题漏判。
CLOZE_DROPDOWN_PLACEHOLDER = '点击选择'
DROPDOWN_TRIGGER_SELECTOR = (
    '.scoop-select-wrapper, .ant-select, .ant-select-selector, '
    '.ant-dropdown-trigger, [role="combobox"], select'
)

DROPDOWN_TRIGGER_JS = """
const root = arguments[0];
const SEL = %s;
const LABEL = %s;
const visible = (el) => el && el.getClientRects && el.getClientRects().length > 0;
const byText = [];
const seen = new Set();
const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
let node;
while ((node = walker.nextNode())) {
    if ((node.textContent || '').indexOf(LABEL) < 0) continue;
    let el = node.parentElement;
    let picked = null;
    for (let i = 0; i < 4 && el && el !== root; i++) {
        if (el.matches('a,button,[role="button"],[role="combobox"],[class*="select"],[class*="dropdown"],[tabindex]')) {
            picked = el;
            break;
        }
        el = el.parentElement;
    }
    const target = picked || node.parentElement;
    if (visible(target) && !seen.has(target)) { seen.add(target); byText.push(target); }
}
if (byText.length) return byText;
const bySelector = [];
root.querySelectorAll(SEL).forEach(function (el) {
    if (visible(el) && !seen.has(el)) { seen.add(el); bySelector.push(el); }
});
return bySelector;
""" % (json.dumps(DROPDOWN_TRIGGER_SELECTOR, ensure_ascii=False),
       json.dumps(CLOZE_DROPDOWN_PLACEHOLDER, ensure_ascii=False))


def find_dropdown_triggers(driver, root):
    """返回容器内可见的下拉触发器元素列表（不依赖具体组件 class）。"""
    try:
        return driver.execute_script(DROPDOWN_TRIGGER_JS, root) or []
    except Exception:
        return []


def has_visible_dropdown(driver, root) -> bool:
    return bool(find_dropdown_triggers(driver, root))


# 「已给出的首字母」只可能是短短的字母串；这些常用词是句子里正常出现的词，不是提示
PREFIX_STOPWORDS = {
    'a', 'an', 'the', 'and', 'or', 'of', 'to', 'in', 'on', 'at', 'for', 'with', 'by',
    'as', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'that', 'this', 'these',
    'those', 'it', 'its', 'their', 'his', 'her', 'our', 'your', 'my', 'from', 'into',
    'about', 'than', 'then', 'so', 'but', 'if', 'when', 'while', 'can', 'will',
    'would', 'should', 'could', 'may', 'might', 'must', 'have', 'has', 'had', 'do',
    'does', 'did', 'not', 'no', 'more', 'most', 'very', 'also', 'such', 'which',
    'who', 'whom', 'whose', 'there', 'here', 'we', 'you', 'they', 'he', 'she', 'i',
    'per', 'all', 'any', 'some', 'each', 'every', 'both', 'other', 'same', 'own',
}


def looks_like_given_prefix(hint: str) -> bool:
    """判断「空位前的字母」到底是教材印出的首字母，还是句子里普通的单词。"""
    text = (hint or '').strip()
    if not text or len(text) > 5:
        return False
    if text.lower() in PREFIX_STOPWORDS:
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", text))


def extract_cjk_hints(text: str) -> List[str]:
    """取出题干括号里的中文提示，例如 `____ (老年大学)` → ['老年大学']。"""
    return [hint.strip() for hint in CJK_HINT_PATTERN.findall(text or '') if hint.strip()]


def blank_cjk_hints(q: "Question") -> List[str]:
    """按空位对齐的中文括注；对不齐（空数与括注数不一致）时返回空列表。"""
    inputs = getattr(q, 'inputs', None) or []
    count = len(inputs)
    if count <= 0:
        return []

    aligned = []
    for blank in (getattr(q, 'banked_blanks', None) or [])[:count]:
        around = f"{blank.get('right_context') or ''} {blank.get('context') or ''}"
        found = extract_cjk_hints(around)
        aligned.append(found[0] if found else '')
    if len(aligned) == count and all(aligned):
        return aligned

    hints = extract_cjk_hints(getattr(q, 'text', '') or '')
    return hints if len(hints) == count else []


def _normalize_vocab_key(text: str) -> str:
    """中文释义归一化：去括注、去标点、去空格，降低对照表命中难度。"""
    value = (text or '').strip()
    value = re.sub(r'[（(].*?[)）]', '', value)
    value = re.sub(r'[\s，,。.；;、:：!！?？\'"“”‘’…—-]+', '', value)
    return value.lower()


class VocabularyStore:
    """Vocabulary 页的中英对照缓存。

    Exercise 1 的填空题就是把 Vocabulary 里的词按中文提示还原成英文，让模型
    自由发挥时经常写错搭配（例如把「老年大学」译成 the elderly university）。
    这里把页面上的对照表收下来：能直接命中的空完全不问模型，命不中的则把整张
    对照表塞进提示词，要求模型优先使用已有译法。
    """

    MAX_PAIRS = 80

    def __init__(self):
        self.pairs: Dict[str, str] = {}       # 归一化中文 → 英文
        self.rows: List[Tuple[str, str]] = []  # (英文, 中文) 按页面出现顺序

    def __len__(self) -> int:
        return len(self.rows)

    def add_rows(self, rows) -> int:
        """批量写入 [(en, zh)] 或 [{'en':..,'zh':..}]，返回新增条数。"""
        added = 0
        for row in rows or []:
            if isinstance(row, dict):
                en, zh = row.get('en') or row.get('english'), row.get('zh') or row.get('chinese')
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                en, zh = row[0], row[1]
            else:
                continue
            if self.add(en, zh):
                added += 1
        return added

    def add(self, en: str, zh: str) -> bool:
        en = re.sub(r'\s+', ' ', (en or '')).strip(' .;:-')
        # 原声 <audio> 的 name 参数常带 .wav/.mp3 后缀，不是词本身
        en = re.sub(r'\.(wav|mp3|m4a|ogg|flac)$', '', en, flags=re.I).strip()
        zh = re.sub(r'\s+', ' ', (zh or '')).strip(' .;:-')
        if not en or not zh:
            return False
        if not re.search(r'[A-Za-z]{2}', en) or not CJK_CHAR_PATTERN.search(zh):
            return False
        if len(en) > 80 or len(zh) > 40:
            return False
        key = _normalize_vocab_key(zh)
        if not key:
            return False
        if self.pairs.get(key, '').lower() == en.lower():
            return False
        if key not in self.pairs and len(self.rows) >= self.MAX_PAIRS:
            return False
        self.pairs[key] = en
        self.rows = [row for row in self.rows if _normalize_vocab_key(row[1]) != key]
        self.rows.append((en, zh))
        return True

    def lookup(self, hint: str) -> Optional[str]:
        """按中文提示找英文原词：先精确、再包含、最后模糊匹配。"""
        key = _normalize_vocab_key(hint)
        if not key:
            return None
        if key in self.pairs:
            return self.pairs[key]

        for candidate, en in self.pairs.items():
            if not candidate:
                continue
            if len(key) >= 2 and (key in candidate or candidate in key):
                return en

        best, best_score = None, 0.0
        for candidate, en in self.pairs.items():
            score = _similarity(key, candidate)
            if score > best_score:
                best, best_score = en, score
        return best if best_score >= 0.84 else None

    def prompt_lines(self, limit: int = 40) -> List[str]:
        lines = []
        for en, zh in self.rows[:limit]:
            lines.append(f"     {zh} = {en}")
        return lines

    def clear(self):
        self.pairs.clear()
        self.rows.clear()


def _similarity(left: str, right: str) -> float:
    """两个归一化中文串的相似度（0-1），用于兜住「老年大学/老年大学校」这类变体。"""
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    common = len(set(left) & set(right))
    base = max(len(set(left)), len(set(right))) or 1
    return common / base * 0.9


class Selectors:
    """CSS选择器仓库"""
    CHOICE_OPTIONS = [
        '.option.isNotReview',
        'div.option',
        '.MultipleChoice--checkbox-item-34A_-',
        'ul[class*="single-choice"] li label',
        '.option-wrap',
    ]
    OPTION_CAPTION = ['.caption', 'span[class*="index"]', '.MultipleChoice--checkbox-opt-2F4xY']
    OPTION_CONTENT = ['.component-htmlview.content', 'div.html-view[class*="content"]', '.html-view', '.content', 'p']
    QUESTION_TITLE = [
        '.ques-title',
        '.component-htmlview.ques-title',
        '.question-inputbox-header',
        '.component-htmlview',
        '.title',
        'p',
        '.question-stem',
    ]
    SUBMIT_BUTTON = [
        'button[type="submit"]',
        'button[class*="submit"]',
        'button[class*="confirm"]',
        '.submit-bar-pc--btn-1_Xvo',
        '.btns-submit button.submit-btn',
        'button.submit-btn',
        '.btn',
    ]
    LEVEL1_TABS = [
        '.pc-header-tabs-container .pc-tab-row > .tab',
        '.pc-header-tabs-container .ant-col.tab',
        '.pc-tab-row > [class*="pc-header-tab"]',
    ]
    LEVEL2_TABS = [
        '.pc-header-tasks-row > .pc-task',
        ':scope > div > div > .pc-header-tasks-row > .pc-task',
    ]


class WebDriverHelper:
    """WebDriver辅助工具类（静态方法）"""

    @staticmethod
    def scroll_into_view(driver, element, pause: float = 0.05) -> None:
        """元素不在视口中心区时才瞬间滚入视口，避免每次操作都触发平滑滚动动画与死等。"""
        try:
            moved = driver.execute_script("""
                const el = arguments[0];
                const r = el.getBoundingClientRect();
                const vh = window.innerHeight || document.documentElement.clientHeight || 800;
                if (r.top >= 40 && r.bottom <= vh - 40) return false;
                el.scrollIntoView({block: 'center', behavior: 'instant'});
                return true;
            """, element)
            if moved and pause > 0:
                time.sleep(pause)
        except Exception:
            pass

    @staticmethod
    def safe_find_element(driver, selectors: List[str], parent=None, timeout: float = 1.5) -> Optional[Any]:
        """安全查找单个元素：先无阻塞快扫全部选择器，未命中且在顶层查找时再合并等待一次。"""
        search_context = parent if parent else driver
        for selector in selectors:
            try:
                for element in search_context.find_elements(By.CSS_SELECTOR, selector):
                    if element.is_displayed():
                        return element
            except Exception:
                continue

        # parent 已是渲染好的题目容器，无需再逐个等待 5 秒导致单题解析卡顿 15~50 秒
        if parent is not None or timeout <= 0 or not selectors:
            return None

        combined = ", ".join(selectors)
        try:
            wait = WebDriverWait(search_context, timeout)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, combined)))
            for selector in selectors:
                try:
                    for element in search_context.find_elements(By.CSS_SELECTOR, selector):
                        if element.is_displayed():
                            return element
                except Exception:
                    continue
        except (TimeoutException, NoSuchElementException):
            pass
        return None

    @staticmethod
    def safe_find_elements(driver, selectors: List[str], parent=None) -> List[Any]:
        """安全查找多个元素"""
        search_context = parent if parent else driver
        for selector in selectors:
            try:
                elements = [
                    e for e in search_context.find_elements(By.CSS_SELECTOR, selector)
                    if e.is_displayed()
                ]
                if elements:
                    return elements
            except Exception as e:
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")  # 控制台只显示简短信息
                logger.error(f"详细错误: {error_msg}", exc_info=True)  # 详细堆栈保存到文件
                continue
        return []

    @staticmethod
    def human_like_delay(base_delay: float = 0.05) -> None:
        """短暂停顿"""
        if base_delay > 0:
            time.sleep(base_delay)

    @staticmethod
    def simulate_typing(driver, element, text: str) -> None:
        """向输入框写入文本并触发标准表单事件（整段写入，避免逐字 HTTP 往返拖慢速度）。"""
        WebDriverHelper.scroll_into_view(driver, element, pause=0.04)
        try:
            element.click()
        except Exception:
            try:
                ActionChains(driver).move_to_element(element).click().perform()
            except Exception:
                driver.execute_script("arguments[0].focus();", element)
        try:
            element.clear()
        except Exception:
            pass
        if text:
            element.send_keys(text)
        driver.execute_script("""
            arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
            arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
            arguments[0].dispatchEvent(new Event('blur', {bubbles: true}));
        """, element)

    @staticmethod
    def safe_click(driver, element, retries: int = 3) -> bool:
        """安全点击元素"""
        for i in range(retries):
            try:
                WebDriverHelper.scroll_into_view(driver, element, pause=0.06)
                try:
                    element.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", element)
                return True

            except StaleElementReferenceException:
                if i < retries - 1:
                    time.sleep(0.35)
                    continue
            except Exception as e:
                if i < retries - 1:
                    time.sleep(0.25)
                    continue
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")  # 控制台只显示简短信息
                logger.error(f"详细错误: {error_msg}", exc_info=True)  # 详细堆栈保存到文件
        return False

    @staticmethod
    def is_choice_option_selected(driver, element) -> bool:
        """统一检测单选/多选选项是否处于真实选中态（兼容自定义 class、Ant Checkbox、原生 input 与 aria）。"""
        js = r"""
        const el = arguments[0];
        if (!el) return false;
        const hasPosClass = (raw) => {
            const cls = String(raw || '');
            if (!cls) return false;
            for (const tok of cls.split(/\s+/)) {
                const low = tok.toLowerCase();
                if (!low) continue;
                if (low.includes('unselected') || low.includes('unchecked')
                        || low.includes('inactive') || low.includes('notselected')
                        || low.includes('not-selected') || low.includes('not_selected')
                        || low === 'isnotreview') {
                    continue;
                }
                if (/(?:^|[_-])(?:selected|checked|active|choosed|picked|sele)(?:$|[_-])/i.test(low)) {
                    return true;
                }
                if (/(?:is|option|item|checkbox|radio|btn|icon)(?:Selected|Checked|Active)/.test(tok)) {
                    return true;
                }
            }
            return false;
        };
        if (el.checked === true) return true;
        if (el.getAttribute && (el.getAttribute('aria-checked') === 'true'
                || el.getAttribute('aria-selected') === 'true'
                || el.getAttribute('data-selected') === 'true'
                || el.getAttribute('data-checked') === 'true')) {
            return true;
        }
        if (hasPosClass(el.className)) return true;
        if (!el.querySelectorAll) return false;
        const inputs = el.querySelectorAll('input[type="checkbox"], input[type="radio"]');
        for (const inp of inputs) {
            if (inp.checked) return true;
        }
        const ariaChecked = el.querySelector('[aria-checked="true"], [aria-selected="true"], .ant-checkbox-checked, .ant-checkbox-wrapper-checked, .ant-radio-checked, .ant-radio-wrapper-checked');
        if (ariaChecked) return true;
        const subNodes = el.querySelectorAll(
            'label, [role="checkbox"], [role="radio"], [class*="checkbox"], [class*="radio"], '
            + '[class*="check"], [class*="caption"], [class*="option"], [class*="icon"], span, i'
        );
        for (const node of subNodes) {
            if (hasPosClass(node.className)) return true;
        }
        const imgs = el.querySelectorAll('img');
        for (const img of imgs) {
            const src = (img.getAttribute('src') || '').toLowerCase();
            if ((src.includes('checked') || src.includes('selected'))
                    && !src.includes('unchecked') && !src.includes('unselected')) {
                return true;
            }
        }
        return false;
        """
        if driver is not None and hasattr(driver, 'execute_script'):
            try:
                res = driver.execute_script(js, element)
                if isinstance(res, bool):
                    return res
            except Exception:
                pass
        try:
            cls = (element.get_attribute('class') or '').lower()
            aria = (element.get_attribute('aria-checked') or '').lower()
            if aria == 'true':
                return True
            for tok in cls.split():
                if any(neg in tok for neg in ('unselected', 'unchecked', 'inactive', 'notselected')):
                    continue
                if any(pos in tok for pos in ('selected', 'checked', 'active', 'choosed', 'picked')):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def option_visual_snapshot(driver, element) -> Optional[str]:
        """抓取选项的 DOM/样式快照，用于在非标准 class 组件上验证点击是否真正引发状态变化。"""
        if driver is None or not hasattr(driver, 'execute_script'):
            return None
        js = r"""
        const el = arguments[0];
        if (!el || !window.getComputedStyle) return null;
        const cs = window.getComputedStyle(el);
        const cap = el.querySelector('.caption, span[class*="index"], [class*="checkbox"], [class*="radio"], [class*="icon"], label');
        const ccs = cap ? window.getComputedStyle(cap) : null;
        const inp = el.querySelector('input[type="checkbox"], input[type="radio"]');
        const imgs = Array.from(el.querySelectorAll('img')).map(i => i.getAttribute('src') || '').join('|');
        const svgs = Array.from(el.querySelectorAll('svg')).map(s => {
            const scs = window.getComputedStyle(s);
            return (s.getAttribute('class') || '') + ':' + (s.getAttribute('fill') || '') + ':' + (scs ? scs.color : '');
        }).join('|');
        return JSON.stringify({
            cls: String(el.className || ''),
            aria: (el.getAttribute('aria-checked') || '') + '/' + (el.getAttribute('aria-selected') || ''),
            bg: cs.backgroundColor || '',
            bd: cs.borderColor || '',
            co: cs.color || '',
            ccls: cap ? String(cap.className || '') : '',
            cbg: ccs ? (ccs.backgroundColor || '') : '',
            cco: ccs ? (ccs.color || '') : '',
            cbd: ccs ? (ccs.borderColor || '') : '',
            chk: inp ? !!inp.checked : null,
            imgs: imgs,
            svgs: svgs,
        });
        """
        try:
            res = driver.execute_script(js, element)
            return res if isinstance(res, str) and res else None
        except Exception:
            return None

    @staticmethod
    def dismiss_onboarding_tips(driver) -> int:
        """静默关闭首次进入任务页时的教学引导气泡（.know-box .iKnow），避免遮挡选项或下拉触发器。
        严禁触碰 ant-modal 提交确认弹窗。"""
        if driver is None or not hasattr(driver, 'execute_script'):
            return 0
        js = r"""
        let closed = 0;
        const isVisible = el => {
            if (!el || !el.getClientRects || !el.getClientRects().length) return false;
            const st = window.getComputedStyle(el);
            return !!(st && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0');
        };
        const selectors = ['.know-box .iKnow', '.know-box button', '.guide-box .iKnow'];
        const tryDoc = (doc) => {
            for (const sel of selectors) {
                let nodes;
                try { nodes = doc.querySelectorAll(sel); } catch (e) { continue; }
                for (const el of nodes) {
                    if (!isVisible(el)) continue;
                    try { el.click(); closed += 1; } catch (e) {}
                }
            }
        };
        tryDoc(document);
        for (let i = 0; i < window.frames.length; i++) {
            try { tryDoc(window.frames[i].document); } catch (e) {}
        }
        return closed;
        """
        try:
            n = driver.execute_script(js)
            return int(n) if isinstance(n, (int, float)) else 0
        except Exception:
            return 0


class OpenAICompatibleClient:
    """OpenAI兼容 API客户端 - 职责：仅处理API通信"""

    SYSTEM_PROMPT = """你是一个专业的英语教学助手，擅长分析英语题目。
请根据题目要求给出准确答案，注意区分不同题型：
- 词汇匹配题：根据英文选中文，或根据中文选英文
- 选词填空：选择最合适的单词填入
- 阅读理解：基于文章内容作答
- 听力选择题：基于音频转写内容作答
- 视频选择题：基于视频转写内容作答
- 排序题：根据材料出现顺序返回选项字母序列"""

    def __init__(self, config: Config):
        self.config = config
        self.client = make_model_client(config.base_url, config.api_key)
        self.conversation_history: List[Dict] = []
        self.material_messages: List[Dict] = []  # 材料独立保留，不随问答窗口裁剪
        self.current_chapter_id: Optional[str] = None
        self.accumulated_passages: set = set()  # 已累积的原文哈希，防重复

    def start_new_chapter(self, chapter_id: str):
        """开始新章节，记录章节ID（不自动清空历史）"""
        self.current_chapter_id = chapter_id
        print(f" 记录章节: {chapter_id[:50]}")

    def force_reset(self, chapter_id: str):
        """强制清空所有历史，无论章节是否相同"""
        self.conversation_history = []
        self.material_messages = []
        self.current_chapter_id = chapter_id
        self.accumulated_passages = set()
        print(f" 强制重置章节: {chapter_id[:50]}")

    def add_passage_if_new(self, passage: str) -> bool:
        """添加原文（如果是新的），返回是否添加成功"""
        return self._add_context_if_new(passage, "阅读材料", "材料")

    def add_audio_transcript_if_new(self, transcript: str) -> bool:
        """添加音频转写（如果是新的），返回是否添加成功"""
        return self._add_context_if_new(transcript, "听力音频转写", "音频转写")

    def add_video_transcript_if_new(self, transcript: str) -> bool:
        """添加视频转写（如果是新的），返回是否添加成功"""
        return self._add_context_if_new(transcript, "视频转写", "视频转写")

    def _add_context_if_new(self, content: str, label: str, ack_label: str) -> bool:
        """添加可复用上下文，按内容哈希去重"""
        if not content or not content.strip() or (label == '阅读材料' and len(content) < 50):
            return False

        passage_hash = hashlib.md5(content.encode()).hexdigest()[:16]

        if passage_hash in self.accumulated_passages:
            print(f"    {ack_label}已存在，跳过")
            return False

        self.accumulated_passages.add(passage_hash)
        material_index = len(self.accumulated_passages)

        passage_msg = {
            "role": "user",
            "content": f"【{label} {material_index}】\n\n{content}\n\n请理解以上内容，等待后续问题。"
        }
        self.material_messages.append(passage_msg)
        self.material_messages.append({
            "role": "assistant",
            "content": f"我已理解{ack_label} {material_index}。请提出问题。"
        })

        print(f"    新增{ack_label}（{len(content)}字符），当前共{len(self.accumulated_passages)}份上下文")
        return True

    @staticmethod
    def _is_image_unsupported_error(exc: Exception) -> bool:
        text = str(exc).lower()
        if not any(word in text for word in
                   ('image', 'vision', 'multimodal', 'image_url', 'unsupported content')):
            return False
        status = getattr(exc, 'status_code', None)
        if status is None:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
        return status is None or status in (400, 404, 413, 415, 422)

    def ask(self, prompt: str, retry_count: int = 3, stop_requested: Optional[threading.Event] = None,
            screenshot: Optional[bytes] = None, remember: bool = True,
            require_image: bool = False) -> Optional[str]:
        """Retry recoverable failures within the configured output limit and retry budget."""
        try:
            max_tokens = max(1, int(self.config.max_tokens))
        except (ValueError, TypeError, OverflowError):
            max_tokens = 32768
        deadline = time.monotonic() + MODEL_RETRY_BUDGET
        print(f"当前ai对话历史共{len(self.conversation_history)}条")

        for attempt in range(retry_count):
            if stop_requested is not None and stop_requested.is_set():
                return None
            if time.monotonic() >= deadline:
                break
            ask_prompt = prompt
            if attempt:
                ask_prompt += "\n\n【重要】请直接给出最终答案，不要输出推理过程，不要解释。"
            user_message: Dict[str, Any] = {"role": "user", "content": ask_prompt}
            if screenshot:
                user_message["content"] = [
                    {"type": "text", "text": ask_prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," +
                                                       base64.b64encode(screenshot).decode("ascii")}},
                ]
            messages = [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                *getattr(self, "material_messages", []),
                *self.conversation_history,
                user_message,
            ]
            shot = f"，附截图 {len(screenshot) // 1024} KB" if screenshot else ""
            print(f"【请求】{self.config.model}（{len(ask_prompt)} 字{shot}，max_tokens={max_tokens}，"
                  f"第 {attempt + 1}/{retry_count} 次）")
            request_started = time.monotonic()
            try:
                remaining = deadline - request_started
                if remaining <= 0:
                    break
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    temperature=self.config.temperature,
                    max_tokens=max_tokens,
                    timeout=min(MODEL_REQUEST_TIMEOUT, remaining),
                )
                if stop_requested is not None and stop_requested.is_set():
                    return None
                choices = getattr(response, "choices", None)
                if not isinstance(choices, (list, tuple)) or not choices:
                    print("模型未返回有效作答数据，请检查 API 地址和响应格式")
                    return None
                choice = choices[0]
                finish_reason = choice.finish_reason
                answer = (choice.message.content or "").strip()
                elapsed = time.monotonic() - request_started
                print(f"【返回】AI 已返回（耗时 {elapsed:.1f}s，finish_reason={finish_reason}，{len(answer)} 字）")
                usage = getattr(response, "usage", None)
                if usage is not None:
                    print(f"模型用量：输入 {getattr(usage, 'prompt_tokens', 0)} Token，"
                          f"输出 {getattr(usage, 'completion_tokens', 0)} Token")
                if not answer:
                    if finish_reason == "length" and attempt + 1 < retry_count:
                        print("模型耗尽本次输出预算，使用简化问法重试")
                        continue
                    print(f"模型未返回正文（finish_reason={finish_reason}）")
                    return None
                if remember:
                    self.conversation_history.extend((
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": answer},
                    ))
                    self.conversation_history = self.conversation_history[-20:]
                print(f"AI回答: {answer}")
                return answer
            except Exception as exc:
                if stop_requested is not None and stop_requested.is_set():
                    return None
                error = describe_model_error(exc)
                if screenshot is not None and self._is_image_unsupported_error(exc):
                    if require_image:
                        print("接口无法识别本题所需图片")
                        return None
                    if attempt + 1 < retry_count:
                        screenshot = None
                        print("接口拒绝图片，使用文本重试")
                        continue
                message = str(exc).lower()
                if (model_error_status(exc) in (400, 422) and max_tokens > 4096
                        and ("max_tokens" in message or "max output" in message)
                        and attempt + 1 < retry_count):
                    max_tokens //= 2
                    print(f"接口拒绝当前输出上限，减半至 {max_tokens} Token 重试")
                    continue
                if not model_error_retryable(exc) or attempt + 1 >= retry_count:
                    print(f"AI调用失败：{error.message}")
                    return None
                delay = (2 ** attempt) + random.random()
                if delay >= deadline - time.monotonic():
                    break
                print(f"AI调用暂时失败：{error.message}，{delay:.1f}s 后重试")
                if stop_requested is not None:
                    if stop_requested.wait(delay):
                        return None
                else:
                    time.sleep(delay)
        print("模型请求已达到重试时限或次数上限")
        return None


class QuestionParserStrategy(ABC):
    """题目解析策略基类"""

    @staticmethod
    def _is_multiple_choice(container, directions: str = "") -> bool:
        class_name = (container.get_attribute('class') or '').lower()
        if 'multiplechoice' in class_name or 'multiple-choice' in class_name:
            return True
        if container.find_elements(By.CSS_SELECTOR,
                                   'input[type="checkbox"], [role="checkbox"], .multipleChoice, '
                                   '[class*="MultipleChoice"], [class*="multiple-choice"]'):
            return True
        instructions = ' '.join([directions, *[e.text for e in container.find_elements(By.CSS_SELECTOR, '.ques-title')]])
        return bool(re.search(
            r'多选|\b(?:choose|select|check|tick|mark)\s+(?:all|two|three|four|five|six|seven|eight|nine|ten|[2-9]|multiple|more than one)\b',
            instructions, re.IGNORECASE))

    @abstractmethod
    def can_parse(self, container, driver) -> bool:
        """是否能解析该容器"""
        pass

    @abstractmethod
    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        """解析题目"""
        pass


class QuestionParser:
    """题目解析器 - 使用策略模式"""

    def __init__(self, driver):
        self.driver = driver
        self.strategies: List[QuestionParserStrategy] = [
            RolePlayStrategy(),
            ParagraphMatchingStrategy(),  # 段落信息匹配：条件最独特，放最前避免被通用策略抢走
            VocabularyReadAloudStrategy(),  # 词汇跟读卡片：优先于 RecordStrategy
            OralAloudStrategy(),  # 整段朗读题：靠 .p-oral-aloud 判定
            VideoDubStrategy(),  # 须先于 VideoStrategy
            VideoStrategy(),
            AudioStrategy(),
            DiscussionBoardStrategy(),
            RecordStrategy(),  # 跟读录音题：靠 .record-icon.button-record 判定，必须在通用策略之前
            ChecklistStrategy(),
            SelfCheckStrategy(),
            SortingStrategy(),
            VocabularyFlashcardStrategy(),
            VocabularyTestStrategy(),
            DropdownSelectStrategy(),
            BankedClozeStrategy(),
            MatchingStrategy(),  # 人物 A-F 匹配题必须在听力填空之前，否则会被当成写单词
            ListeningFillInStrategy(),  # 听力填空（转录由AISolver预处理完成）
            StandardChoiceStrategy(),
            MyVoiceTextStrategy(),
            TextInputStrategy(),
            FillInStrategy(),
            GenericReplyStrategy(),  # 兜底：翻译/总结/首字母搭配等结构不固定的页面
        ]

    def _find_reading_question_containers(self) -> List[Any]:
        """
        查找需要拆分的问答题容器（阅读问答题、翻译题）
        关键：多个reply，每个包含.question-inputbox，且direction表明是多题作答
        """
        try:
            direction_text = ""
            try:
                direction_elem = self.driver.find_element(
                    By.CSS_SELECTOR,
                    ".layout-direction-container .component-htmlview"
                )
                direction_text = direction_elem.text.lower()
            except:
                pass

            is_multi_question_type = any(kw in direction_text for kw in [
                'answer', 'question', 'according to',  # 阅读问答题
                'translate',  # 翻译题
            ])

            if not is_multi_question_type:
                return []

            body_selectors = [
                '.layoutBody-container.has-material.has-reply',
                '.layoutBody-container.has-reply',  # 翻译题
            ]

            for selector in body_selectors:
                body_containers = self.driver.find_elements(By.CSS_SELECTOR, selector)

                for body in body_containers:
                    has_scoop = body.find_elements(By.CSS_SELECTOR, '.fe-scoop')
                    if has_scoop:
                        continue

                    has_options = body.find_elements(By.CSS_SELECTOR, '.option-wrapper, .banked-options')
                    if has_options:
                        continue

                    reply_containers = body.find_elements(By.CSS_SELECTOR, '.question-common-abs-reply')

                    valid_replies = []
                    for reply in reply_containers:
                        try:
                            if reply.is_displayed() and reply.find_elements(By.CSS_SELECTOR, '.question-inputbox'):
                                valid_replies.append(reply)
                        except:
                            continue

                    if len(valid_replies) >= 2:
                        return valid_replies

            return []

        except Exception as e:
            logger.debug(f"查找问答题失败: {e}")
            return []

    def _extract_directions_from_page(self) -> str:
        """从页面统一提取 direction"""
        try:
            direction_elem = self.driver.find_element(
                By.CSS_SELECTOR,
                ".layout-direction-container .component-htmlview"
            )
            return direction_elem.text.strip()
        except:
            pass

        try:
            direction_elem = self.driver.find_element(
                By.CSS_SELECTOR,
                ".abs-direction .content"
            )
            return direction_elem.text.strip()
        except:
            pass

        try:
            direction_elem = self.driver.find_element(
                By.CSS_SELECTOR,
                ".direction-container"
            )
            return direction_elem.text.strip()
        except:
            pass

        return ""

    def parse_all(self) -> Tuple[List[Question], str]:
        """解析所有可见题目"""
        try:
            return self._parse_all_once()
        except StaleElementReferenceException:
            print("     页面已更新，重新获取题目")
            return self._parse_all_once()

    def _parse_all_once(self) -> Tuple[List[Question], str]:
        if self._is_discussion_board_page():
            print("     检测到讨论板页面，交给发帖处理器")
            try:
                element = self.driver.find_element(By.TAG_NAME, 'body')
            except Exception:
                element = None
            topic = ""
            for selector in ('.ds-discussion-title', '.discussion-title'):
                try:
                    topic = (self.driver.find_element(By.CSS_SELECTOR, selector).text or '').strip()
                    if topic:
                        break
                except Exception:
                    continue
            return [Question(number=1, text=topic or "讨论区发帖",
                             q_type=QuestionType.DISCUSSION_BOARD, element=element)], ""

        containers = self._find_containers()
        questions = []
        directions = self._extract_directions_from_page()

        print(f"     找到 {len(containers)} 个题目容器")

        for idx, container in enumerate(containers, 1):
            try:
                if not self._is_really_visible(container):
                    print(f"      容器 {idx} 不可见，跳过")
                    continue

                question = self._parse_single(container, idx, directions)
                if question:
                    if question.is_interactive():
                        questions.append(question)
                        print(f"      题目 {idx}: {question.q_type.name} - {question.text[:50]}...")
                    else:
                        print(f"      题目 {idx} 非交互类型: {question.q_type.name}")
                else:
                    print(f"      容器 {idx} 解析为None")

            except StaleElementReferenceException:
                raise
            except Exception as e:
                error_msg = str(e)
                print(f"       解析容器 {idx} 失败:{error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                continue

        return questions, directions

    def _is_discussion_board_page(self) -> bool:
        """检查当前页面是否是讨论板"""
        try:
            strong_indicators = [
                '.discussion-course-page-sdk',
                '.discussion-title',
                '.ds-discussion-bottom-textArea-container',
                '.discussion-cloud-recordList'
            ]

            score = 0
            for indicator in strong_indicators:
                if self.driver.find_elements(By.CSS_SELECTOR, indicator):
                    score += 1

            if score >= 2:
                print(f"     讨论板检测得分: {score}/{len(strong_indicators)}")
                return True
            return False
        except Exception as e:
            error_msg = str(e)
            print(f"操作失败: {error_msg[:50]}")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return False

    def _find_containers(self) -> List[Any]:
        """查找题目容器"""
        if self._is_discussion_board_page():
            print("     检测到讨论板页面，跳过")
            return []

        role_play = [c for c in self.driver.find_elements(
            By.CSS_SELECTOR, '.layoutBody-container')
                     if c.is_displayed() and c.find_elements(By.CSS_SELECTOR, '.role-play-wrapper')]
        if role_play:
            return role_play

        # New words / Phrases and expressions 词汇跟读卡片：结构独特，必须优先于
        # _find_record_containers()，否则会被 .button-record 兜底选择器收成录音题。
        try:
            vocab_containers = [c for c in self.driver.find_elements(
                By.CSS_SELECTOR, VOCAB_READ_ALOUD_SELECTOR) if c.is_displayed()]
        except Exception:
            vocab_containers = []
        if vocab_containers:
            print(f"     找到 {len(vocab_containers)} 个词汇跟读页")
            return vocab_containers

        # 整段朗读：一页一段，点 start 后强制准备再强制录音
        try:
            aloud_containers = [c for c in self.driver.find_elements(
                By.CSS_SELECTOR, '.p-oral-aloud') if c.is_displayed()
                and c.find_elements(By.CSS_SELECTOR, '.p-oral-aloud-button')]
        except Exception:
            aloud_containers = []
        if aloud_containers:
            print(f"     找到 {len(aloud_containers)} 个段落朗读题")
            return aloud_containers

        try:
            dub_containers = [c for c in self.driver.find_elements(
                By.CSS_SELECTOR, '.question-video-dub-pc') if c.is_displayed()]
        except Exception:
            dub_containers = []
        if dub_containers:
            print(f"     找到 {len(dub_containers)} 个视频配音题")
            return dub_containers

        # 跟读/朗读录音题：每句右侧是麦克风按钮，先按行收容器，避免落到通用判定里
        record_containers = self._find_record_containers()
        if record_containers:
            print(f"     找到 {len(record_containers)} 个跟读录音题容器")
            return record_containers

        # 自评清单（Wrapping up）优先于选择题判定，否则表格里的图标会被误判成选项
        checklist_containers = []
        for container in self.driver.find_elements(By.CSS_SELECTOR, '.layoutBody-container'):
            try:
                text = (container.text or '').lower()
                if 'got it' not in text:
                    continue
                has_table = container.find_elements(By.CSS_SELECTOR, 'table, .ant-table-tbody')
                has_icon = container.find_elements(By.CSS_SELECTOR, '.anticon-border, [aria-label="border"]')
                if has_table and has_icon and container.is_displayed():
                    checklist_containers.append(container)
            except Exception:
                continue
        if checklist_containers:
            print(f"     找到 {len(checklist_containers)} 个自评清单容器")
            return checklist_containers

        reading_containers = self._find_reading_question_containers()
        if reading_containers:
            print(f"     找到 {len(reading_containers)} 道阅读问答题（共享材料）")
            return reading_containers

        sequence_containers = []
        for reply in self.driver.find_elements(By.CSS_SELECTOR, '.question-common-abs-reply'):
            try:
                if reply.is_displayed() and reply.find_elements(By.CSS_SELECTOR, '.sequence-view, .sortable-list-wrapper'):
                    sequence_containers.append(reply)
            except:
                continue
        if sequence_containers:
            print(f"     找到 {len(sequence_containers)} 道排序题")
            return sequence_containers

        choice_containers = self.driver.find_elements(
            By.CSS_SELECTOR,
            '.question-common-abs-reply > .question-common-abs-choice'
        )

        if len(choice_containers) >= 2:
            reply_containers = []
            for choice in choice_containers:
                try:
                    reply = choice.find_element(By.XPATH,
                                                './parent::div[contains(@class, "question-common-abs-reply")]')
                    if reply not in reply_containers:
                        reply_containers.append(reply)
                except:
                    pass

            if reply_containers:
                print(f"     找到 {len(reply_containers)} 道独立选择题")
                return reply_containers

        banked_containers = WebDriverHelper.safe_find_elements(
            self.driver,
            ['.layoutBody-container.has-material.has-reply']
        )
        valid_banked = []
        for container in banked_containers:
            has_options = container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option')
            has_blanks = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .comp-abs-input input')
            if has_options and has_blanks:
                valid_banked.append(container)

        if valid_banked:
            print(f"     找到 {len(valid_banked)} 个选词填空容器")
            return valid_banked

        self_check_containers = []
        for container in self.driver.find_elements(By.CSS_SELECTOR, '.layoutBody-container'):
            try:
                has_table = container.find_elements(By.CSS_SELECTOR, '.ticket-view table, .ant-table-tbody')
                has_got_it = 'got it' in container.text.lower()
                if has_table and has_got_it and container.is_displayed():
                    self_check_containers.append(container)
            except:
                continue

        if self_check_containers:
            print(f"     找到 {len(self_check_containers)} 个Self-check词汇勾选容器")
            return self_check_containers

        video_containers = WebDriverHelper.safe_find_elements(
            self.driver,
            ['.layoutBody-container:has(video)', '.question-video-point-read', '.video-box']
        )
        if video_containers:
            for container in video_containers:
                has_questions = container.find_elements(By.CSS_SELECTOR,
                                                        '.question-common-abs-choice, .question-inputbox, .option, .fe-scoop')
                if not has_questions:
                    print(f"     找到纯视频容器")
                    return [container]

        containers = self.driver.find_elements(By.CSS_SELECTOR, '.layout-container')
        valid_containers = []
        for c in containers:
            try:
                has_content = (
                        c.find_elements(By.CSS_SELECTOR, '.question-inputbox, .option, .fe-scoop, textarea') or
                        c.find_elements(By.CSS_SELECTOR, 'input[type="text"]')
                )
                # Cloze 下拉选择题只有自绘下拉、没有 input / textarea，
                # 不补充探测会让整个 .layout-container 在容器发现阶段被丢弃。
                if not has_content:
                    has_content = has_visible_dropdown(self.driver, c)
                if has_content and c.is_displayed():
                    valid_containers.append(c)
            except:
                continue

        if valid_containers:
            print(f"     找到 {len(valid_containers)} 个有效题目容器（layout-container）")
            return valid_containers

        fallback = WebDriverHelper.safe_find_elements(
            self.driver,
            ['.layoutBody-container', '.layout-reply-container', '.reply-wrap']
        )
        if fallback:
            print(f"     备用方案找到 {len(fallback)} 个容器")
            return fallback

        # 最后兜底：页面上只有一个可见的作答区（textarea / 空位）时直接拿它，
        # 避免「总结/翻译」这类没有标准 layout-container 的页面整页放弃
        reply_fallback = []
        for reply in self.driver.find_elements(By.CSS_SELECTOR, '.question-common-abs-reply'):
            try:
                if not reply.is_displayed():
                    continue
                if reply.find_elements(By.CSS_SELECTOR, 'textarea, .fe-scoop input'):
                    reply_fallback.append(reply)
            except Exception:
                continue
        if reply_fallback:
            print(f"     兜底找到 {len(reply_fallback)} 个作答容器")
            return reply_fallback

        return []

    def _find_record_containers(self) -> List[Any]:
        """跟读/朗读录音题：按每个麦克风按钮所属的句子行收容器。"""
        buttons = find_record_buttons(self.driver)
        if not buttons:
            return []

        rows = []
        for button in buttons:
            row = None
            for xpath in (
                "ancestor::*[contains(@class,'oral-study-sentence')][1]",
                "ancestor::div[contains(@class,'oral-paragraph-container')][1]",
                "ancestor::div[contains(@class,'record-button-group')][1]",
                "ancestor::div[contains(@class,'layoutBody-container')][1]",
            ):
                try:
                    row = button.find_element(By.XPATH, xpath)
                    break
                except Exception:
                    continue
            if row is None:
                # Exercise 2 的自由作答区没有 oral-study 结构，退一层到题块
                try:
                    row = button.find_element(
                        By.XPATH, "ancestor::div[contains(@class,'question-')][1]")
                except Exception:
                    row = None
            if row is None:
                continue
            if row not in rows:
                rows.append(row)
        return rows

    def _is_really_visible(self, element) -> bool:
        """检查元素真正可见"""
        try:
            if not element.is_displayed():
                return False

            parent = element
            for _ in range(3):
                try:
                    parent = parent.find_element(By.XPATH, '..')
                    parent_display = self.driver.execute_script(
                        "return window.getComputedStyle(arguments[0]).display",
                        parent
                    )
                    if parent_display == 'none':
                        return False
                except StaleElementReferenceException:
                    raise
                except:
                    break

            return True
        except StaleElementReferenceException:
            raise
        except:
            return False

    def _parse_single(self, container, number: int, directions: str = "") -> Optional[Question]:
        """使用策略解析单个容器"""
        for strategy in self.strategies:
            try:
                if strategy.can_parse(container, self.driver):
                    print(f"      使用策略: {strategy.__class__.__name__}")
                    question = strategy.parse(container, self.driver, number, directions)
                    if question:
                        if question.number is None:
                            question.number = number
                        print(f"      解析成功: {question.q_type.name}")
                        return question
                    else:
                        print(f"      策略返回None")
            except StaleElementReferenceException:
                raise
            except Exception as e:
                error_msg = str(e)
                print(f"      策略 {strategy.__class__.__name__} 失败: {error_msg[:50]} ")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                continue
        print(f"      没有匹配的策略")
        return None


class RolePlayStrategy(QuestionParserStrategy):
    def can_parse(self, container, driver) -> bool:
        return bool(container.find_elements(By.CSS_SELECTOR, '.role-play-wrapper'))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        return Question(number=question_number, text=directions or '角色扮演录音',
                        q_type=QuestionType.RECORD, element=container, directions=directions,
                        record_items=[{'role_play': True, 'text': '角色扮演'}])


class DiscussionBoardStrategy(QuestionParserStrategy):
    """讨论板策略"""

    def can_parse(self, container, driver) -> bool:
        discussion_features = [
            '.discussion-course-page-sdk',
            '.ds-discussion-reply',
            '.discussion-cloud-recordList-title',
        ]

        has_discussion_feature = any(
            container.find_elements(By.CSS_SELECTOR, feature)
            for feature in discussion_features
        )

        if not has_discussion_feature:
            return False

        banked_features = [
            '.question-material-banked-cloze-reply',
            '.banked-options',
            '.fe-scoop[data-scoop-index]',
        ]

        has_banked_feature = any(
            container.find_elements(By.CSS_SELECTOR, feature)
            for feature in banked_features
        )
        if has_banked_feature:
            return False

        return True

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        return Question(
            number=question_number,
            text="讨论板页面（无需作答）",
            q_type=QuestionType.DISCUSSION_BOARD,
            element=container
        )


class SelfCheckStrategy(QuestionParserStrategy):
    """Self-check 词汇勾选表解析策略"""

    def can_parse(self, container, driver) -> bool:
        direction_text = ""
        try:
            direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
            direction_text = direction_elem.text.lower()
        except:
            pass

        has_table = bool(container.find_elements(By.CSS_SELECTOR, '.ticket-view table, .ant-table-tbody'))
        has_got_it = 'got it' in container.text.lower()
        has_check_instruction = any(kw in direction_text for kw in (
            'check in the box', "you've learned", 'you have learned', 'self-assessment', 'checklist'))
        return has_table and has_got_it and has_check_instruction

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        rows = []
        for row in container.find_elements(By.CSS_SELECTOR, 'tbody tr.ant-table-row:not(.category-name)'):
            try:
                word_elem = row.find_element(By.CSS_SELECTOR, '.content-text')
                word = word_elem.text.strip()
                got_it_icon = row.find_element(By.CSS_SELECTOR, 'td:nth-child(2) .anticon')
                if word and got_it_icon:
                    rows.append({'word': word, 'element': got_it_icon})
            except:
                continue

        return Question(
            number=question_number,
            text=f"Self-check 词汇勾选（共{len(rows)}项）",
            q_type=QuestionType.SELF_CHECK,
            element=container,
            banked_blanks=rows,
            directions=directions,
        )


class SortingStrategy(QuestionParserStrategy):
    """拖拽排序题解析策略"""

    CONTROL_SELECTOR = '.sequence-view, .sortable-list-wrapper'

    def can_parse(self, container, driver) -> bool:
        return bool(container.find_elements(By.CSS_SELECTOR, self.CONTROL_SELECTOR))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        option_elements = container.find_elements(By.CSS_SELECTOR, '.sequence-reply-view-item-text')
        options = []

        for idx, elem in enumerate(option_elements):
            full_text = elem.text.strip()
            if not full_text:
                continue

            letter = ""
            text = full_text
            try:
                spans = elem.find_elements(By.TAG_NAME, 'span')
                if spans:
                    raw_letter = re.sub(r'[\s.、):：]+', '', spans[0].text).upper()
                    if re.fullmatch(r'[A-Z]', raw_letter):
                        letter = raw_letter
                        if len(spans) > 1:
                            text = spans[1].text.strip() or full_text
            except:
                pass

            if not letter:
                match = re.match(r'^([A-Z])\s*[.、):：]\s*(.+)$', full_text, re.DOTALL)
                if match:
                    letter = match.group(1).upper()
                    text = match.group(2).strip()
                else:
                    letter = chr(65 + idx)

            options.append(Option(letter=letter, text=text, element=elem))

        if len(options) < 2:
            return None

        question_text = "排序题：请根据材料出现顺序重新排列选项"
        if directions:
            question_text = f"{directions}\n{question_text}"

        return Question(
            number=question_number,
            text=question_text,
            q_type=QuestionType.SORTING,
            element=container,
            options=options,
            directions=directions,
        )


class VocabularyTestStrategy(QuestionParserStrategy):
    """词汇测试题解析策略"""

    def can_parse(self, container, driver) -> bool:
        if self._is_multiple_choice(container):
            return False
        options = self._extract_options(container, driver)
        if len(options) < 2:
            return False

        title_elem = WebDriverHelper.safe_find_element(driver, Selectors.QUESTION_TITLE, container)
        if not title_elem:
            return False

        text = title_elem.text.strip()
        text = re.sub(r"^\d+[.、)\]]\s*", "", text)

        is_eng_word = bool(re.match(r"^[a-zA-Z\-]+$", text)) and 1 < len(text) <= 20
        has_chinese = bool(re.search(r"[\u4e00-\u9fff]", text))

        option_texts = [opt.text for opt in options]
        has_eng_opts = any(re.search(r"[a-zA-Z]{3,}", t) for t in option_texts)
        has_chi_opts = any(re.search(r"[\u4e00-\u9fff]", t) for t in option_texts)

        return (is_eng_word and has_chi_opts) or (has_chinese and has_eng_opts)

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        title_elem = WebDriverHelper.safe_find_element(driver, Selectors.QUESTION_TITLE, container)
        text = title_elem.text.strip() if title_elem else ""

        options = self._extract_options(container, driver)

        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.VOCABULARY_TEST,
            element=container,
            options=options,
            directions=directions
        )

    def _extract_options(self, container, driver) -> List[Option]:
        """提取选项"""
        options = []
        option_elements = WebDriverHelper.safe_find_elements(driver, Selectors.CHOICE_OPTIONS, container)

        for opt_elem in option_elements:
            letter = ""
            text = ""

            caption_elem = WebDriverHelper.safe_find_element(driver, Selectors.OPTION_CAPTION, opt_elem)
            if caption_elem:
                letter = caption_elem.text.strip().replace('.', '').replace(')', '').replace('、', '')

            content_elem = WebDriverHelper.safe_find_element(driver, Selectors.OPTION_CONTENT, opt_elem)
            if content_elem:
                text = content_elem.text.strip()
            else:
                full_text = opt_elem.text.strip()
                text = re.sub(rf"^{re.escape(letter)}[.)、\\s]*", "", full_text)

            is_selected = WebDriverHelper.is_choice_option_selected(driver, opt_elem)

            if letter or text:
                options.append(Option(letter=letter, text=text, element=opt_elem, is_selected=is_selected))

        return options


class BankedClozeStrategy(QuestionParserStrategy):
    """选词填空解析策略"""

    def can_parse(self, container, driver) -> bool:
        has_options = bool(
            container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option, .option-wrapper .option-placeholder') or
            container.find_elements(By.CSS_SELECTOR, '.banked-options .option')
        )
        has_blanks = bool(container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .comp-abs-input input'))
        return has_options and has_blanks

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        banked_options = []

        placeholder_elements = container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option-placeholder')
        for elem in placeholder_elements:
            text = elem.text.strip()
            if text and text not in banked_options:
                banked_options.append(text)

        if not banked_options:
            option_elements = container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option')
            for elem in option_elements:
                text = elem.text.strip()
                if text and text not in banked_options:
                    banked_options.append(text)

        if not banked_options:
            option_elements = container.find_elements(By.CSS_SELECTOR,
                                                      '.banked-options .option, [data-rbd-draggable-id^="options-"]')
            for elem in option_elements:
                text = elem.text.strip()
                if text and text not in banked_options:
                    banked_options.append(text)

        print(f"      选项池（短语）: {banked_options}")

        inputs = []
        banked_blanks = []

        scoops = container.find_elements(By.CSS_SELECTOR, '.fe-scoop')

        for i, scoop in enumerate(scoops):
            context = ""
            try:
                context_elem = scoop.find_element(By.XPATH, './ancestor::p')
                # WebElement.text 不含 input 的 value，空位会在上下文里凭空消失。
                # 克隆整句，把输入框换成 ____ 占位符，模型才知道空在哪、前后是什么。
                context = driver.execute_script("""
                    const p = arguments[0];
                    const clone = p.cloneNode(true);
                    for (const el of clone.querySelectorAll('input, textarea')) {
                        el.replaceWith(document.createTextNode(' ____ '));
                    }
                    return (clone.textContent || '').trim();
                """, context_elem) or ""
                context = re.sub(r'\s+', ' ', context).strip()
            except:
                try:
                    context = scoop.text.strip()
                except:
                    context = ""

            input_box = None
            try:
                input_box = scoop.find_element(By.CSS_SELECTOR, 'input')
                inputs.append(input_box)
            except:
                pass

            banked_blanks.append({
                'index': i,
                'context': context,
                'input': input_box,
                'element': scoop
            })

        print(f"      找到 {len(banked_blanks)} 个填空位置")

        # 只给每个空 180 字的尾巴，模型猜不出该填哪个搭配；把整段题面（含词库）一起给它
        material = FillInStrategy._page_material(container, limit=6000)
        question_text = f"选词填空（{len(banked_blanks)}个空）"
        if material:
            question_text += f"\n【全文】{material}"
        if banked_options:
            # 词库必须完整给出：10 个表达的题组用 [:8] 会把后两个藏成「等共 10 个」，
            # 模型无法在「每个表达只用一次」的约束下做全局分配。
            question_text += f"\n可选选项: {', '.join(banked_options)}"


        return Question(
            number=question_number,
            text=question_text,
            q_type=QuestionType.BANKED_CLOZE,
            element=container,
            inputs=inputs,
            banked_options=banked_options,
            banked_blanks=banked_blanks,
            directions=directions,
        )


class StandardChoiceStrategy(QuestionParserStrategy):
    def _is_listening_choice_page(self, driver, directions: str = "") -> bool:
        direction_text = (directions or "").lower()
        if not direction_text:
            try:
                direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
                direction_text = direction_elem.text.lower()
            except:
                direction_text = ""

        has_listening_hint = any(kw in direction_text for kw in ['listen', 'audio', 'hear', 'conversation', 'passage', 'news'])
        has_choice_hint = any(kw in direction_text for kw in ['choose', 'answer', 'best answer'])
        return has_listening_hint and has_choice_hint

    def _is_video_choice_page(self, driver, directions: str = "") -> bool:
        direction_text = (directions or "").lower()
        if not direction_text:
            try:
                direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
                direction_text = direction_elem.text.lower()
            except:
                direction_text = ""

        has_video_hint = any(kw in direction_text for kw in ['watch', 'video', 'clip'])
        has_choice_hint = any(kw in direction_text for kw in ['choose', 'answer', 'decide', 'true or false', 'statements'])
        return has_video_hint and has_choice_hint

    def can_parse(self, container, driver) -> bool:
        if container.tag_name == 'div' and 'question-common-abs-choice' in (container.get_attribute('class') or ''):
            options = container.find_elements(By.CSS_SELECTOR, '.option-wrap .option, .option.isNotReview')
        else:
            choices = container.find_elements(By.CSS_SELECTOR, '.question-common-abs-choice')
            if choices:
                if len(choices) > 1:
                    return False
                options = choices[0].find_elements(By.CSS_SELECTOR, '.option-wrap .option, .option.isNotReview')
            else:
                options = container.find_elements(By.CSS_SELECTOR, '.option-wrap .option, .option.isNotReview')

        return len(options) >= 2

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        if 'question-common-abs-choice' in (container.get_attribute('class') or ''):
            choice_container = container
        else:
            choices = container.find_elements(By.CSS_SELECTOR, '.question-common-abs-choice')
            choice_container = choices[0] if choices else container

        title_elem = choice_container.find_element(By.CSS_SELECTOR, '.ques-title')
        text = title_elem.text.strip() if title_elem else ""

        vocab_strategy = VocabularyTestStrategy()
        options = vocab_strategy._extract_options(container, driver)

        is_listening_choice = self._is_listening_choice_page(driver, directions)
        is_video_choice = self._is_video_choice_page(driver, directions)
        is_multi = self._is_multiple_choice(container, directions)
        if is_video_choice and not is_multi:
            q_type = QuestionType.VIDEO_CHOICE
        elif is_listening_choice and not is_multi:
            q_type = QuestionType.LISTENING_CHOICE
        else:
            q_type = QuestionType.MULTIPLE_CHOICE if is_multi else QuestionType.SINGLE_CHOICE

        return Question(
            number=question_number,
            text=text,
            q_type=q_type,
            element=container,
            options=options,
            directions=directions
        )


class MyVoiceTextStrategy(QuestionParserStrategy):
    """My voice 上传页的文字作答策略"""

    TEXTAREA_SELECTORS = [
        '.question-multi-file-upload textarea.ant-input',
        '.question-multi-file-upload textarea',
        'textarea[placeholder*="输入文字作答"]',
    ]

    def can_parse(self, container, driver) -> bool:
        textarea = self._find_textarea(container)
        if not textarea:
            return False

        has_upload = bool(container.find_elements(By.CSS_SELECTOR, '.question-multi-file-upload, .unipus-upload'))
        if not has_upload:
            return False

        page_text = self._collect_page_text(container, driver).lower()
        return any(kw in page_text for kw in ['record', 'upload', 'introduction', 'my voice', '输入文字作答'])

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        textarea = self._find_textarea(container)
        if not textarea:
            return None

        material_text = self._extract_material_text(container, driver)
        full_text = ""
        if directions:
            full_text += f"【题目要求】{directions}\n\n"
        if material_text:
            full_text += f"【任务材料】\n{material_text}\n\n"
        full_text += "请根据以上要求，直接写一段可粘贴到输入框的英文介绍，控制在500字符以内。"

        return Question(
            number=question_number,
            text=full_text,
            q_type=QuestionType.MY_VOICE_TEXT,
            element=container,
            inputs=[textarea],
            directions=directions,
        )

    def _find_textarea(self, container):
        for selector in self.TEXTAREA_SELECTORS:
            try:
                for elem in container.find_elements(By.CSS_SELECTOR, selector):
                    if elem.is_displayed():
                        return elem
            except:
                continue
        return None

    def _collect_page_text(self, container, driver) -> str:
        parts = []
        for source in [container, driver]:
            try:
                text = source.text.strip()
                if text:
                    parts.append(text)
            except:
                pass
        return "\n".join(parts)

    def _extract_material_text(self, container, driver) -> str:
        selectors = [
            '.layout-material-container',
            '.question-common-abs-material',
            '.text-material-wrapper',
        ]
        for selector in selectors:
            try:
                elem = container.find_element(By.CSS_SELECTOR, selector)
                text = elem.text.strip()
                if text:
                    return text
            except:
                continue

        for selector in selectors:
            try:
                elem = driver.find_element(By.CSS_SELECTOR, selector)
                text = elem.text.strip()
                if text:
                    return text
            except:
                continue
        return ""


# New words / Phrases and expressions：词汇跟读卡片（course 播放器 swiper）。
# 页面上已经给出要读的单词/短语和 1.2 秒原声，不需要 AI 生成任何内容。
VOCAB_READ_ALOUD_SELECTOR = '.vocabulary-wrapper'

# 从当前 active 卡片读词、原声、进度。swiper 只渲染 active slide，
# 其余是 "vocabulary card" 占位，所以每翻一张都要重新抓一次。
VOCAB_CARD_JS = r"""
return (function (root) {
  const scope = root || document;
  const wrapper = (scope.matches && scope.matches('.vocabulary-wrapper'))
      ? scope
      : (scope.querySelector('.vocabulary-wrapper') ||
         document.querySelector('.vocabulary-wrapper'));
  if (!wrapper) { return {}; }
  const slide = wrapper.querySelector('.swiper-slide-active') ||
                wrapper.querySelector('.swiper-vocabulary-slide');
  if (!slide) { return {}; }
  const txt = function (sel) {
    const n = slide.querySelector(sel);
    return n ? (n.innerText || n.textContent || '').trim() : '';
  };
  const audioNode = slide.querySelector('.soundWrap .question-audio audio, .soundWrap audio');
  const raw = audioNode
      ? (audioNode.getAttribute('src') || audioNode.getAttribute('title') || '')
      : '';
  let duration = 0;
  const dm = raw.match(/duration=([0-9.]+)/);
  if (dm) { duration = parseFloat(dm[1]); }
  let name = '';
  const nm = raw.match(/name=([^&]*)/);
  if (nm) { try { name = decodeURIComponent(nm[1]); } catch (e) { name = nm[1]; } }
  let index = 0, total = 0;
  const spans = wrapper.querySelectorAll('.progressBar .ratio span');
  if (spans.length >= 2) {
    index = parseInt((spans[0].innerText || spans[0].textContent || '').trim(), 10) || 0;
    total = parseInt((spans[1].innerText || spans[1].textContent || '')
        .replace(/[^0-9]/g, ''), 10) || 0;
  }
  const button = slide.querySelector('.ucomp-recorder .button-record, .button-record');
  const recordings = Array.from(slide.querySelectorAll('.practice-recorder audio'))
      .map(a => a.getAttribute('src') || '').filter(Boolean);
  return {
    word: txt('.vocWord .component-htmlview'),
    definition: txt('.explanations'),
    audio: raw,
    duration: duration,
    name: name,
    index: index,
    total: total,
    hasButton: !!button,
    recording: !!(button && button.querySelector('svg rect[fill="currentColor"]')),
    recordings: recordings
  };
})(arguments[0]);
"""

# 翻到下一张：优先用 Swiper 实例 API（页面带 swiper-no-swiping，手势不可用），
# 再退到 next 按钮 / 挂在 window 上的实例。
VOCAB_NEXT_JS = r"""
return (function (root, reset) {
  const scope = root || document;
  const wrapper = (scope.matches && scope.matches('.vocabulary-wrapper'))
      ? scope
      : (scope.querySelector('.vocabulary-wrapper') ||
         document.querySelector('.vocabulary-wrapper'));
  if (!wrapper) { return ''; }
  const sw = wrapper.querySelector('.swiper-container') ||
             wrapper.querySelector('[class*="swiper-container"]');
  const move = function (inst) {
    const method = reset ? 'slideTo' : 'slideNext';
    if (!inst || typeof inst[method] !== 'function') { return false; }
    if (reset) { inst.slideTo(0); } else { inst.slideNext(); }
    return true;
  };
  if (sw && move(sw.swiper)) {
    return 'swiper';
  }
  const btn = !reset && wrapper.querySelector(
      '.swiper-button-next, [class*="vocNext"], [class*="next-btn"], [class*="nextBtn"]');
  if (btn) { btn.click(); return 'button'; }
  for (const key of ['vocSwiper', 'swiper', 'mySwiper']) {
    const inst = window[key];
    if (move(inst)) {
      return 'window.' + key;
    }
  }
  return '';
})(arguments[0], arguments[1]);
"""

RECORD_BUTTON_SELECTOR = '.record-icon.button-record'

# 兜底选择器：Exercise 2 那种「限时自由作答」的录音按钮不一定带 button-record，
# 主选择器找不到时依次尝试，避免整题被当成「没有输入框」跳过。
RECORD_BUTTON_FALLBACKS = (
    '.button-record',
    '.oral-record-btn',
    '.record-btn',
    '.record-button',
    '[class*="record"][class*="btn"]',
)


def record_button_selectors() -> Tuple[str, ...]:
    return (RECORD_BUTTON_SELECTOR,) + RECORD_BUTTON_FALLBACKS


# 只要求「听/看」的页面（Listening skills 等）不做转写。
SKIP_ASR_TITLE_KEYWORDS = ('skill', '技能')

ANSWER_WIDGET_SELECTORS = (
    SortingStrategy.CONTROL_SELECTOR,
    '.question-common-abs-choice',
    '.option-wrapper .option',
    '.option-wrap',
    '.banked-options',
    '.fe-scoop',
    '.question-common-abs-scoop',
    '.comp-abs-input input',
    '.question-inputbox',
    '.question-common-abs-reply',
    '.question-common-abs-textmatch .input-wrapper',
    '.record-icon.button-record',
    '.button-record',
    '.blank-item',
    '[class*="cloze"]',
    '[class*="fill-blank"]',
    'input[type="text"]',
    'input[type="radio"]',
    'input[type="checkbox"]',
    'textarea',
    'select',
    '[contenteditable="true"]',
)


def find_record_buttons(driver, scope=None) -> List[Any]:
    """按「主 → 兜底」顺序找录音按钮，返回第一组有结果的可见按钮。"""
    root = driver if scope is None else scope
    for selector in record_button_selectors():
        try:
            buttons = root.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        if not isinstance(buttons, (list, tuple)):
            continue
        usable = []
        for button in buttons:
            try:
                if not button.is_displayed():
                    continue
                size = button.size or {}
                # 录音列表容器之类的超大元素不是按钮
                if (size.get('width') or 0) > 180 or (size.get('height') or 0) > 180:
                    continue
            except Exception:
                continue
            usable.append(button)
        if usable:
            return usable
    return []


# 从 Vocabulary 页抓中英对照。跟读行本身就是「英文 + 中文释义」两行，
# 原声 <audio> 的 src 里还带 #name=英文，两条路都收一遍。
VOCAB_PAIRS_JS = r"""
return (function () {
  const out = [];
  const seen = new Set();
  const CJK = /[\u4e00-\u9fa5]/;
  const push = function (en, zh) {
    en = (en || '').replace(/\s+/g, ' ').trim().replace(/^[\s"'“”]+|[\s"'“”]+$/g, '');
    zh = (zh || '').replace(/\s+/g, ' ').trim();
    if (!en || !zh) { return; }
    if (!/[A-Za-z]{2}/.test(en) || CJK.test(en)) { return; }
    if (!CJK.test(zh)) { return; }
    if (en.length > 80 || zh.length > 40) { return; }
    const key = en.toLowerCase() + '|' + zh;
    if (seen.has(key)) { return; }
    seen.add(key);
    out.push({ en: en, zh: zh });
  };
  const decodeName = function (raw) {
    const match = raw.match(/name=([^&]*)/);
    if (!match) { return ''; }
    try { return decodeURIComponent(match[1]); } catch (e) { return match[1]; }
  };
  const rowText = function (row) {
    const lines = (row.innerText || '').split('\n').map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
    let en = '', zh = '';
    for (const line of lines) {
      if (!zh && CJK.test(line)) { zh = line; continue; }
      if (!en && /[A-Za-z]{2}/.test(line) && !CJK.test(line) && line.length <= 80) { en = line; }
    }
    return { en: en, zh: zh };
  };
  const wrapRows = Array.from(document.querySelectorAll('.record-button-wrap'));
  for (const wrap of wrapRows) {
    const row = wrap.closest('.oral-study-sentence') || wrap.closest('.record-button-group') ||
      wrap.parentElement;
    if (!row) { continue; }
    const text = rowText(row);
    const audio = wrap.querySelector('audio');
    const fromAudio = audio ? decodeName(audio.getAttribute('src') || '') : '';
    push(fromAudio || text.en, text.zh);
  }
  // 词汇表/单词列表这类不走录音组件的结构
  const listRows = Array.from(document.querySelectorAll(
    '.vocabulary-list li, .word-list li, .vocab-list li, .vocabulary-item, .word-item'));
  for (const row of listRows) {
    const text = rowText(row);
    push(text.en, text.zh);
  }
  return out;
})();
"""

# 录音题的浏览器侧支撑库：把一段音频喂给 navigator.mediaDevices.getUserMedia，
# 页面上的录音器（chivox）拿到的就是这段音频，而不是真的麦克风。
#
# 关键细节：录音器开始拉流后的前 ~0.6 秒必丢块并把丢失部分补成静音，
# 所以参考音频前面要垫 LEAD_PAD 秒静音（实测 0.8s 时逐词满分）。
RECORD_MIC_BOOTSTRAP_JS = r"""
(function () {
  if (window.__ucRec) { return; }
  const md = navigator.mediaDevices || (navigator.mediaDevices = {});
  const api = { buf: null, ctx: null, pad: 0.8, calls: 0, dur: 0, srcDur: 0, maxDur: 0, lastStream: null };
  api.ctxOf = function () {
    const ctx = api.ctx || (api.ctx = new AudioContext());
    try { ctx.resume(); } catch (e) {}
    return ctx;
  };
  api.silent = function () {
    return api.ctxOf().createMediaStreamDestination().stream;
  };
  api.stream = function () {
    const ctx = api.ctxOf();
    if (!api.buf) { return ctx.createMediaStreamDestination().stream; }
    const padSec = api.pad || 0;
    const sr = api.buf.sampleRate;
    // 限时题（如 2 分钟自由作答）要把音频裁到时长以内，末尾做个短淡出防爆音
    let srcFrames = api.buf.length;
    if (api.maxDur > 0 && srcFrames > Math.round(api.maxDur * sr)) {
      srcFrames = Math.round(api.maxDur * sr);
    }
    const frames = srcFrames + Math.round(padSec * sr);
    const buf = ctx.createBuffer(api.buf.numberOfChannels, frames, sr);
    const fade = Math.min(Math.round(0.25 * sr), Math.floor(srcFrames / 4));
    for (let c = 0; c < api.buf.numberOfChannels; c++) {
      const src = api.buf.getChannelData(c);
      const dst = buf.getChannelData(c);
      dst.set(src.subarray(0, srcFrames), Math.round(padSec * sr));
      for (let i = 0; i < fade; i++) {
        dst[frames - 1 - i] *= i / fade;
      }
    }
    const src = ctx.createBufferSource();
    src.buffer = buf;
    const dest = ctx.createMediaStreamDestination();
    src.connect(dest);
    src.start();
    api.calls += 1;
    api.dur = buf.duration;
    api.lastStream = dest.stream;
    return dest.stream;
  };
  md.getUserMedia = function () { return Promise.resolve(api.stream()); };
  md.getDisplayMedia = md.getDisplayMedia || function () { return Promise.resolve(api.silent()); };
  window.__ucRec = api;
})();
"""

RECORD_MIC_JS = r"""
const done = arguments[arguments.length - 1];
const b64 = arguments[0] || '';
const pad = (typeof arguments[1] === 'number') ? arguments[1] : 0.8;
const maxDur = (typeof arguments[2] === 'number') ? arguments[2] : 0;
(async () => {
  const api = window.__ucRec;
  if (!api) { done({ ok: false, err: 'bootstrap missing' }); return; }
  api.pad = pad;
  api.maxDur = maxDur;
  if (!b64) { done({ ok: true, dur: api.dur, srcDur: api.srcDur, pad: pad, reused: true }); return; }
  const ctx = api.ctxOf();
  const raw = atob(b64);
  const u8 = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) u8[i] = raw.charCodeAt(i);
  const buf = await ctx.decodeAudioData(u8.buffer);
  api.buf = buf;
  api.srcDur = buf.duration;
  const usable = maxDur > 0 ? Math.min(buf.duration, maxDur) : buf.duration;
  api.dur = usable + pad;
  done({ ok: true, dur: api.dur, srcDur: api.srcDur, sr: buf.sampleRate, pad: pad, maxDur: maxDur });
})().catch((e) => done({ ok: false, err: String(e) }));
"""

RECORD_ITEMS_JS = r"""
const root = arguments[0] || document;
const tidy = function (value) {
  let text = (value || '');
  for (const mark of ['(w)', '(W)', '(s)', '(S)']) {
    text = text.split(mark).join(' ');
  }
  return text.split(' ').filter(function (part) { return part.length > 0; })
    .join(' ').trim();
};
const oneLine = function (value) {
  const parts = (value || '').split(String.fromCharCode(10));
  const out = [];
  for (const part of parts) {
    const clean = tidy(part);
    if (clean) { out.push(clean); }
  }
  return out.join(' ');
};
const audioOf = function (scope) {
  if (!scope) { return null; }
  let nodes = [];
  try { nodes = Array.from(scope.querySelectorAll('audio')); } catch (e) { nodes = []; }
  const usable = nodes.filter(function (audio) {
    if (audio.closest('.audio-replay')) { return false; }
    const src = audio.getAttribute('src') || audio.getAttribute('data-src') ||
      audio.getAttribute('title') || '';
    return src.indexOf('http') === 0 || src.indexOf('data:') === 0;
  });
  const origin = usable.find(function (audio) {
    return !!audio.closest('.question-audio');
  });
  return origin || usable[0] || null;
};
return Array.from(arguments[1]).map(function (button) {
  const wrap = button.closest('.record-button-wrap');
  const paragraph = button.closest('.oral-paragraph-container');
  const group = button.closest('.record-button-group');
  // 口语回答这类题（oral-personal-state）：题干在 oral-container 里，
  // 录音区里只有倒计时和「点击按钮，结束录音」提示，不能拿来当题名。
  const oralBox = button.closest('.oral-personal-state-wrapper');
  const row = button.closest('.oral-study-sentence') || paragraph || root;
  let audio = null;
  for (const scope of [paragraph, group, row]) {
    audio = audioOf(scope);
    if (audio) { break; }
  }
  if (!audio) { audio = audioOf(wrap); }
  const song = (group && group.querySelector('.question-audio.audio-origin')) ||
    (row && row.querySelector('.question-audio.audio-origin'));
  const raw = audio
    ? (audio.getAttribute('src') || audio.getAttribute('data-src') ||
       audio.getAttribute('title') || '')
    : (song ? (song.getAttribute('title') || '') : '');
  let passage = null;
  for (const scope of [paragraph, row]) {
    if (!scope) { continue; }
    passage = scope.querySelector('.sentence-html-container') ||
      scope.querySelector('.oral-personal-state-sentence-container');
    if (passage) { break; }
  }
  const text = passage ? oneLine(passage.innerText || passage.textContent || '') : '';
  // 口语题的问题题干只能当题名/签名。写进 text 会优先于 AI 稿件被当成“朗读原文”。
  const promptBox = oralBox ? oralBox.querySelector('.oral-personal-state-oral-container') : null;
  const promptText = promptBox
    ? oneLine(promptBox.innerText || promptBox.textContent || '') : '';
  let lines = [];
  if (row) {
    lines = (row.innerText || '').split(String.fromCharCode(10))
      .map(function (part) { return tidy(part); })
      // 倒计时与录音提示不是题名，拾到会把“03:00”当成题干。
      .filter(function (part) {
        return part.length > 0 && !/^\d{1,2}:\d{2}$/.test(part) &&
          part.indexOf('点击按钮') === -1 && part.indexOf('结束录音') === -1;
      });
  }
  const rows = Array.from(document.querySelectorAll('.oral-study-sentence'));
  return {
    raw: raw,
    name: text || promptText || (lines.length ? lines[0] : ''),
    text: text,
    // 单独给出稳定题干，供签名使用：name 末位可能是播放器状态文案。
    prompt: promptText,
    lines: lines.slice(0, 4),
    src: song ? (song.getAttribute('title') || '') : '',
    rowIndex: rows.indexOf(row)
  };
});
"""

RECORD_ROW_STATE_JS = r"""
return (function (root, idx, selectors) {
  const blank = { text: '', score: null, stop: false, wave: '', recorded: false,
                  htmlLen: 0, blob: 0, uploaded: 0, btnSig: '', micLive: false, micCalls: 0 };
  if (!root) { return blank; }
  const sel = (selectors && selectors.length) ? selectors.join(',') : '.record-icon.button-record';
  let rows;
  try {
    rows = (root.matches && root.matches('.oral-study-sentence'))
      ? [root]
      : Array.from(root.querySelectorAll('.oral-study-sentence'));
  } catch (e) { rows = []; }
  if (!rows.length) {
    try {
      rows = (root.matches && root.matches('.question-common-abs-reply > .item'))
        ? [root]
        : Array.from(root.querySelectorAll('.question-common-abs-reply > .item'));
    } catch (e) { rows = []; }
  }
  if (!rows.length) {
    try { rows = Array.from(root.querySelectorAll('.record-button-group')); } catch (e) { rows = []; }
  }
  if (!rows.length) { rows = [root]; }
  const row = rows[idx] || rows[0];
  if (!row) { return blank; }
  // Exercise 2 这类自由作答的分数与回放挂在再上一层 .p-oral-personal-state 上，
  // 麦克风按钮所在的 .record-button-group 里什么都没有，所以要看更大的作用域。
  // 但只有「整块里就一个录音按钮」时才上扩，避免跟读题多行共用外层时串分。
  let scope = row;
  const outer = row.closest('.oral-paragraph-container, .p-oral-personal-state');
  if (outer && !row.querySelector('.score_layout') &&
      outer.querySelectorAll(sel).length === 1) {
    scope = outer;
  }
  const node = row.querySelector('.score_layout') ||
    (row.matches && row.matches('.item') ? row.querySelector('.score') : null) ||
    scope.querySelector('.score_layout');
  const text = node ? ((node.innerText || node.textContent || '').trim()) : '';
  const match = text.match(/(\d{1,3})/);
  const icon = row.querySelector(sel);
  const recorderBox = icon ? icon.closest('.ucomp-recorder, .record-button-group') : null;
  // 录音中图标会换成实心方块，空闲时 svg 里只有一个 opacity=0 的 rect；
  // 视频配音题等组件可能通过类名标识录音状态，一并检查。
  const stop = !!(icon && (
    icon.querySelector('svg rect[fill="currentColor"]') ||
    /\b(recording|is-recording|stopping)\b/i.test(icon.className || '') ||
    (recorderBox && /\b(recording|is-recording)\b/i.test(recorderBox.className || ''))
  ));
  const btnSig = icon
    ? ((icon.className || '') + '|' + (icon.getAttribute('aria-label') || '') + '|' +
       (icon.innerText || '').trim() + '|' +
       (icon.innerHTML || '').replace(/\s+/g, ' ').slice(0, 200))
    : '';
  const waveNode = scope.querySelector('.audio-control-box.audio-control-wave');
  // 录完之后平台会上传音频并给一个可回放的 <audio src="http://...mp3">；
  // 题目自带原声放在 .record-button-wrap 里，不能算作答结果。
  const audios = Array.from(scope.querySelectorAll('audio')).filter(function (a) {
    if (a.closest('.paragraphPlayerContainer')) { return false; }
    if (a.closest('.record-button-wrap') && !a.closest('.audio-replay')) { return false; }
    return ((a.getAttribute('src') || '').length > 0);
  });
  const blob = audios.filter(function (a) {
    const src = a.getAttribute('src') || '';
    return src.indexOf('blob:') === 0 || src.indexOf('data:') === 0;
  }).length;
  const uploaded = audios.length - blob;
  const recApi = window.__ucRec;
  const micLive = !!(recApi && recApi.lastStream && recApi.lastStream.getAudioTracks &&
    recApi.lastStream.getAudioTracks().some(function (t) { return t.readyState === 'live'; }));
  const micCalls = (recApi && typeof recApi.calls === 'number') ? recApi.calls : 0;
  return {
    text: text,
    score: match ? parseInt(match[1], 10) : null,
    stop: stop,
    wave: waveNode ? waveNode.className : '',
    recorded: (blob + uploaded) > 0,
    htmlLen: (scope.innerHTML || '').length,
    blob: blob,
    uploaded: uploaded,
    btnSig: btnSig,
    micLive: micLive,
    micCalls: micCalls
  };
})(arguments[0], arguments[1], arguments[2]);
"""
ORAL_ALOUD_STATE_JS = r"""
const score = document.querySelector('.score_layout');
const btn = document.querySelector('.p-oral-aloud-button');
const timer = document.querySelector('.count-down-time');
const text = score ? (score.innerText || '').replace(/[^0-9.]/g, '') : '';
return {
    score: text,
    btn: btn ? (btn.innerText || '').replace(/\s+/g, ' ').trim() : '',
    timer: timer ? (timer.innerText || '').trim() : '',
    recording: !!document.querySelector('[class*="record-sentence-container"]'),
    done: !!document.querySelector('.p-oral-aloud [class*="luyin"], .p-oral-aloud [class*="huifang"]')
};
"""


RECORD_STATE_JS = r"""
return (function () {
  // 调试/自检用：整页录音题的状态快照（每条分数、波形按钮态、注入调用次数）
  const rows = Array.from(document.querySelectorAll('.oral-study-sentence, .record-button-group'));
  const pick = (row) => {
    const score = row.querySelector('.score_layout');
    const wave = row.querySelector('.audio-control-box.audio-control-wave');
    return {
      score: score ? (score.innerText || score.textContent || '').trim() : '',
      wave: wave ? (wave.className || '') : '',
      text: (row.innerText || '').trim().slice(0, 80).replace(/\s+/g, ' '),
    };
  };
  return {
    rows: rows.map(pick),
    buttons: document.querySelectorAll('.record-icon.button-record').length,
    calls: (window.__ucRec && window.__ucRec.calls) || 0,
  };
})();
"""


class TTSEngine:
    """录音题的语音合成：把文字变成可以冒充麦克风的音频。

    Vocabulary / Exercise 1 的跟读题页面自带原声，直接借原声即可；Exercise 2 这类
    主观题只有一个计时录音按钮，没有原声可用，只能先让模型写一段口语稿，再合成成
    语音喂进麦克风轨道。这里做两套后端：

    - edge：微软在线朗读（自然度最高，需要联网，可走代理）；
    - sapi：Windows 自带 System.Speech（离线，机器味重但永远可用）。

    两者产出的格式（mp3 / wav）浏览器 decodeAudioData 都能直接解。
    """

    EDGE_VOICE = "en-US-AriaNeural"
    EDGE_FAILURE_LIMIT = 2
    CACHE_LIMIT = 16
    WORDS_PER_SECOND = 2.4  # 正常语速，用来估算「这段话要不要裁」
    SAPI_RATE = 0

    def __init__(self, engine: str = "auto", voice: str = "", proxy: str = "", rate: str = "+0%",
                 voice_cloner=None):
        self.engine = (engine or "auto").strip().lower()
        self.voice = (voice or "").strip()
        self.proxy = (proxy or os.environ.get("UNIPUS_TTS_PROXY", "") or "").strip()
        self.rate = rate or "+0%"
        self.voice_cloner = voice_cloner
        self.last_error = ""
        self._edge_failures = 0
        self._cache: Dict[str, Tuple[str, bytes]] = {}
        self._lock = threading.Lock()

    def set_voice_cloner(self, voice_cloner):
        with self._lock:
            self.voice_cloner = voice_cloner
            self._cache.clear()

    def release(self):
        cloner = getattr(self, 'voice_cloner', None)
        if cloner is not None:
            try:
                cloner.release()
            except Exception:
                pass

    def convert_reference_audio(self, raw_audio: Optional[bytes]) -> Optional[bytes]:
        """当用户启用「我的音色」且开启跟读原声迁移时，把教材原声转成使用者音色。"""
        if not raw_audio:
            return raw_audio
        cloner = getattr(self, 'voice_cloner', None)
        if cloner is None or not getattr(cloner, 'enabled', False) or not getattr(cloner, 'convert_reference', False):
            return raw_audio
        if not cloner.is_ready():
            return raw_audio
        converted = cloner.convert_reference_audio(raw_audio, out_format="mp3")
        if converted and converted is not raw_audio:
            print(f"    已使用「我的音色」模拟参考原声（{len(converted)} 字节）")
            return converted
        if getattr(cloner, 'last_error', ''):
            print(f"    {cloner.last_error}")
        return raw_audio

    @property
    def enabled(self) -> bool:
        cloner = getattr(self, 'voice_cloner', None)
        if cloner is not None and getattr(cloner, 'enabled', False) and cloner.is_ready():
            return True
        return self.engine not in ("off", "none", "disable", "disabled")

    def synthesize(self, text: str, max_seconds: float = 110.0) -> Tuple[str, bytes]:
        """合成音频，返回 (格式, 字节)；失败返回 ("", b"")，原因写在 last_error。"""
        if not self.enabled:
            self.last_error = "TTS 已在配置里关闭"
            return "", b""

        spoken = self.prepare_text(text, max_seconds)
        if not spoken:
            self.last_error = "没有可朗读的英文文本"
            return "", b""

        # 若用户已录入并开启「我的音色」，优先用使用者自己的音色生成流畅自然英文语音
        cloner = getattr(self, 'voice_cloner', None)
        if cloner is not None and getattr(cloner, 'enabled', False):
            if cloner.is_ready():
                fmt, data = cloner.synthesize(spoken, out_format="mp3")
                if data:
                    print(f"    TTS(我的音色) 合成 {len(spoken.split())} 词 / {len(data)} 字节")
                    return fmt, data
                print(f"    我的音色合成失败（{cloner.last_error or '未知原因'}），自动回退标准语音")
            else:
                print("    已开启「我的音色」但尚未就绪（缺少样本或本地模型），自动回退标准语音")

        key = hashlib.md5(f"{spoken}|{self.voice}|{self.rate}".encode()).hexdigest()
        with self._lock:
            cached = self._cache.get(key)
        if cached:
            print(f"    TTS 复用缓存（{len(spoken.split())} 词）")
            return cached

        errors = []
        for name in self._engine_order():
            fmt, data = self._run_engine(name, spoken)
            if data:
                with self._lock:
                    if len(self._cache) >= self.CACHE_LIMIT:
                        self._cache.clear()
                    self._cache[key] = (fmt, data)
                print(f"    TTS({name}) 合成 {len(spoken.split())} 词 / {len(data)} 字节")
                return fmt, data
            errors.append(f"{name}: {self.last_error}")
        self.last_error = "; ".join(e for e in errors if e)
        return "", b""

    def _engine_order(self) -> Tuple[str, ...]:
        if self.engine in ("sapi", "system", "windows"):
            return ("sapi",)
        if self.engine == "edge":
            return ("edge",)
        return ("edge", "sapi")

    def _run_engine(self, name: str, text: str) -> Tuple[str, bytes]:
        try:
            if name == "edge":
                return self._synth_edge(text)
            return self._synth_sapi(text)
        except Exception as exc:
            self.last_error = f"{name} 合成异常: {str(exc)[:120]}"
            logger.error("TTS 合成失败", exc_info=True)
            return "", b""

    # ---- 文本预处理 ------------------------------------------------------
    def prepare_text(self, text: str, max_seconds: float = 110.0) -> str:
        """清掉 markdown / 中文说明，并按语速把过长稿子裁到时长以内。"""
        raw = (text or '').replace('\r', '\n')
        # 只摘掉围栏本身，保留里面的正文：模型常把整篇口语稿包在 ``` 里
        raw = re.sub(r'^[ \t]*```[a-zA-Z]*[ \t]*$', '', raw, flags=re.M)
        raw = re.sub(r'```', ' ', raw)
        raw = re.sub(r'[*_`#>]+', ' ', raw)

        kept = []
        for line in raw.split('\n'):
            line = line.strip()
            if not line:
                continue
            # 纯中文行（模型的解释、译文）不朗读
            if not re.search(r'[A-Za-z]{2}', line):
                continue
            line = re.sub(r'^[\s\-–—•\d]+[.、)）:：]\s*', '', line)
            kept.append(line)
        spoken = ' '.join(kept)
        spoken = re.sub(r'[（(][^）)]*[\u4e00-\u9fa5][^）)]*[）)]', ' ', spoken)
        spoken = re.sub(r'\s+', ' ', spoken).strip()
        if not spoken:
            return ""
        return self._trim_to_seconds(spoken, max_seconds)

    def _trim_to_seconds(self, text: str, max_seconds: float) -> str:
        budget = int(max(15.0, float(max_seconds or 0)) * self.WORDS_PER_SECOND)
        words = text.split()
        if len(words) <= budget:
            return text

        clipped = ' '.join(words[:budget])
        # 尽量在句末切开，免得半句话被截断
        end = max(clipped.rfind('. '), clipped.rfind('! '), clipped.rfind('? '))
        if end >= len(clipped) * 0.6:
            clipped = clipped[:end + 1]
        return clipped.strip()

    # ---- edge-tts --------------------------------------------------------
    def _synth_edge(self, text: str) -> Tuple[str, bytes]:
        if self._edge_failures >= self.EDGE_FAILURE_LIMIT:
            self.last_error = "edge-tts 连续失败，本轮改用离线引擎"
            return "", b""
        try:
            import edge_tts  # 可选依赖：pip install edge-tts
        except Exception:
            self._edge_failures = self.EDGE_FAILURE_LIMIT
            self.last_error = "未安装 edge-tts（pip install edge-tts 后自动启用）"
            return "", b""

        voice = self.voice or self.EDGE_VOICE
        path = os.path.join(tempfile.gettempdir(),
                            f"unipus_tts_{int(time.time() * 1000)}_{random.randint(100, 999)}.mp3")
        kwargs = {"rate": self.rate}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        try:
            try:
                self._edge_save(edge_tts, text, voice, path, kwargs)
            except TypeError:
                # 老版本 Communicate 不收 proxy/rate，退一步只传文本和音色
                self._edge_save(edge_tts, text, voice, path, {})
            with open(path, 'rb') as handle:
                data = handle.read()
            if not data:
                raise RuntimeError("edge-tts 返回空音频")
            self._edge_failures = 0
            return "mp3", data
        except Exception as exc:
            self._edge_failures += 1
            self.last_error = f"edge-tts 合成失败: {str(exc)[:120]}"
            return "", b""
        finally:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _edge_save(edge_tts, text: str, voice: str, path: str, kwargs: Dict[str, Any]):
        import asyncio

        async def run():
            communicate = edge_tts.Communicate(text, voice, **kwargs)
            await communicate.save(path)

        asyncio.run(run())

    @staticmethod
    def _decode_console(raw: bytes) -> str:
        """控制台输出可能是 UTF-8，也可能是 GBK/系统代码页，逐个试。"""
        for encoding in ('utf-8', 'mbcs', 'gbk'):
            try:
                return raw.decode(encoding)
            except Exception:
                continue
        return raw.decode('utf-8', 'ignore')

    # ---- Windows SAPI ----------------------------------------------------
    def _synth_sapi(self, text: str) -> Tuple[str, bytes]:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.last_error = "未找到 powershell，无法使用 Windows 语音合成"
            return "", b""

        stamp = f"{int(time.time() * 1000)}_{random.randint(100, 999)}"
        txt_path = os.path.join(tempfile.gettempdir(), f"unipus_tts_{stamp}.txt")
        wav_path = os.path.join(tempfile.gettempdir(), f"unipus_tts_{stamp}.wav")
        with open(txt_path, 'w', encoding='utf-8') as handle:
            handle.write(text)

        script = SAPI_TTS_SCRIPT.replace('__RATE__', str(int(self.SAPI_RATE)))
        env = dict(os.environ)
        env['UNIPUS_TTS_TEXT'] = txt_path
        env['UNIPUS_TTS_OUT'] = wav_path
        env['UNIPUS_TTS_VOICE'] = self.voice

        creation_flags = 0x08000000 if os.name == 'nt' else 0
        try:
            completed = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-Command", script],
                capture_output=True, timeout=180, env=env,
                creationflags=creation_flags,
            )
            if completed.returncode != 0:
                detail = self._decode_console(completed.stderr or completed.stdout or b'')
                self.last_error = f"SAPI 失败: {detail.strip()[:160]}"
                return "", b""
            with open(wav_path, 'rb') as handle:
                data = handle.read()
            if not data:
                self.last_error = "SAPI 返回空音频"
                return "", b""
            return "wav", data
        except Exception as exc:
            self.last_error = f"SAPI 异常: {str(exc)[:120]}"
            return "", b""
        finally:
            for path in (txt_path, wav_path):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass


SAPI_TTS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $synth.Rate = __RATE__ } catch { }
$installed = @($synth.GetInstalledVoices())

$candidates = New-Object System.Collections.ArrayList
if ($env:UNIPUS_TTS_VOICE) { [void]$candidates.Add($env:UNIPUS_TTS_VOICE) }
foreach ($item in $installed) {
  if ($item.Enabled -and $item.VoiceInfo.Culture.Name -like 'en*') {
    [void]$candidates.Add($item.VoiceInfo.Name)
  }
}
foreach ($item in $installed) {
  if ($item.Enabled) { [void]$candidates.Add($item.VoiceInfo.Name) }
}

foreach ($name in $candidates) {
  if (-not $name) { continue }
  try { $synth.SelectVoice($name); break } catch { }
}

$text = [System.IO.File]::ReadAllText($env:UNIPUS_TTS_TEXT, [System.Text.Encoding]::UTF8)
$synth.SetOutputToWaveFile($env:UNIPUS_TTS_OUT)
$synth.Speak($text)
$synth.Dispose()
"""


class VocabularyReadAloudStrategy(QuestionParserStrategy):
    """New words / Phrases and expressions 词汇跟读卡片。

    DOM：.vocabulary-wrapper > .vocContainer .swiper-container > .swiper-slide-active，
    词在 .vocWord .component-htmlview，原声在 .soundWrap .question-audio audio，
    按钮是 .ucomp-recorder .button-record，进度在 .progressBar .ratio。

    这页的答案就是卡片上印着的单词/短语，且自带 1.2 秒原声，因此
    record_items 同时带 url 与 text，让 needs_speech_script 恒为 False，
    完全不触发「AI 写口语稿 + TTS 录 90 秒」那条链路。
    """

    def can_parse(self, container, driver) -> bool:
        try:
            if container.find_elements(By.CSS_SELECTOR, VOCAB_READ_ALOUD_SELECTOR):
                return True
            return bool(container.find_elements(By.CSS_SELECTOR, '.vocContainer .vocWord'))
        except Exception:
            return False

    def parse(self, container, driver, question_number: int,
              directions: str = "") -> Optional[Question]:
        try:
            info = driver.execute_script(VOCAB_CARD_JS, container) or {}
        except Exception:
            info = {}
        word = (info.get('word') or '').strip()
        raw = info.get('audio') or ''
        url = raw.split('#')[0].strip()
        item = {
            'index': 0,
            'url': url,
            'name': word or (info.get('name') or ''),
            'duration': float(info.get('duration') or 0),
            'text': word,
            'lines': [],
            'element': None,
            'needs_tts': not url,
            'vocab': True,
            'vocab_total': int(info.get('total') or 0),
            'vocab_index': int(info.get('index') or 0),
        }
        text = word or '词汇跟读'
        definition = (info.get('definition') or '').strip()
        if definition:
            text += '\n' + definition[:200]
        if directions:
            text = directions.strip() + '\n' + text
        print(f"    词汇跟读卡片: {word or '(未读到)'} | "
              f"原声 {'有' if url else '无'} | "
              f"进度 {item['vocab_index']}/{item['vocab_total']}")
        return Question(
            number=question_number,
            text=text[:1200],
            q_type=QuestionType.RECORD,
            element=container,
            directions=directions,
            record_items=[item],
        )


class OralAloudStrategy(QuestionParserStrategy):
    """整段朗读题（3-8 Unit test 的 Passage speaking）

    页面只给一段文章，点 start 后先强制准备 2 分钟，再强制录一遍音；录音结束才出分，
    之后才是可以手动重录的界面。自动化只需要一次动作：准备阶段就把 TTS 音频装进
    假麦克风，强制录音一开始平台录到的就是整段朗读。
    """

    CONTAINER_SELECTOR = '.p-oral-aloud'

    def can_parse(self, container, driver) -> bool:
        try:
            return bool(container.find_elements(By.CSS_SELECTOR, '.p-oral-aloud-button'))
        except Exception:
            return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        text = ''
        for selector in ('.p-oral-aloud-sentence .component-htmlview',
                         '.p-oral-aloud-content .component-htmlview',
                         '.p-oral-aloud-sentence'):
            try:
                nodes = container.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                nodes = []
            for node in nodes:
                value = (node.text or '').strip()
                if len(value) > 40:
                    text = re.sub(r'\s+', ' ', value)
                    break
            if text:
                break
        if not text:
            return None
        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.ORAL_ALOUD,
            element=container,
            directions=directions,
            record_items=[{'url': '', 'name': '', 'text': text, 'duration': 0.0}],
        )


class RecordStrategy(QuestionParserStrategy):
    """跟读/朗读录音题（course-v2 播放器 .oral-study-sentence）

    新版视听说教材的 Vocabulary、Exercise 里有一类题型：页面上给一句英文和它的
    原声，要求"听并跟读"，每条右侧是一个麦克风按钮。做题方式不是填输入框，而是
    让浏览器录一段音交给腾讯智聆（SOE）打分——所以只能走「把参考音频冒充成麦克风
    输入」这条路，不需要 TTS、也不需要真的说话。
    """

    def can_parse(self, container, driver) -> bool:
        try:
            if container.find_elements(By.CSS_SELECTOR, RECORD_BUTTON_SELECTOR):
                return True
        except Exception:
            pass
        return bool(find_record_buttons(driver, container))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        buttons = find_record_buttons(driver, container)
        if not buttons:
            return None

        # 参考音频挂在 .record-button-wrap 里的 <audio>，src 后面带
        # #duration=..&name=..，这里一次性用 JS 读出来，比多层 XPath 稳。
        try:
            metas = driver.execute_script(
                RECORD_ITEMS_JS, container, buttons) or []
        except Exception:
            metas = []

        items = []
        for idx, button in enumerate(buttons):
            meta = {'index': idx, 'url': '', 'name': '', 'duration': 0.0,
                    'lines': [], 'element': button}
            if idx < len(metas):
                info = metas[idx] or {}
                raw = (info.get('raw') or '') or (info.get('src') or '')
                meta['url'] = raw.split('#')[0].strip()
                match = re.search(r'name=([^&]*)', raw)
                if match:
                    meta['name'] = self._decode_audio_name(match.group(1))
                match = re.search(r'duration=([0-9.]+)', raw)
                if match:
                    meta['duration'] = float(match.group(1))
                lines = [str(line).strip() for line in (info.get('lines') or []) if str(line).strip()]
                meta['lines'] = lines
                meta['text'] = (info.get('text') or '').strip()
                meta['prompt'] = (info.get('prompt') or '').strip()
                # 记下行号，按钮被 React 换掉后按行重定位。
                try:
                    row_index = int(info.get('rowIndex'))
                except (TypeError, ValueError):
                    row_index = -1
                if row_index >= 0:
                    meta['row_index'] = row_index
                if not meta['name']:
                    meta['name'] = (info.get('name') or '').strip()
            if not meta['name']:
                meta['name'] = (button.get_attribute('title') or '').strip()
            # 段落跟读优先用样音；无样音时只朗读给定段落，不生成新稿。
            meta['needs_tts'] = not meta['url']
            items.append(meta)

        if not items:
            return None

        text = self._question_text(container, items, directions)

        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.RECORD,
            element=container,
            directions=directions,
            record_items=items,
        )

    @staticmethod
    def _decode_audio_name(raw: str) -> str:
        try:
            return unquote(raw).strip()
        except Exception:
            return (raw or '').strip()

    @staticmethod
    def _question_text(container, items: List[Dict], directions: str) -> str:
        """题目要求（题目本身 + 指示），供没有原声的录音题写口语稿用。

        Exercise 2 这类主观题的结构是
        `.p-oral-personal-state > .oral-personal-state-wrapper > (题干段落 | .record-button-group)`，
        麦克风按钮所在的 `.record-button-group` 只是兄弟节点的一部分，题干在它上面一层，
        所以先在本层找，找不到再往上两三层找。
        """
        prompt_selectors = ('.oral-personal-state-sentence-container',
                            '.component-htmlview',
                            '.sentence-html-container',
                            '.question-title', '.topic-title',
                            '.layoutBody-title', '.direction-content')

        candidates = []
        scopes = [container]
        for xpath in ("ancestor::div[contains(@class,'oral-personal-state-wrapper')][1]",
                      "ancestor::div[contains(@class,'question-abs-question')][1]"):
            try:
                scopes.append(container.find_element(By.XPATH, xpath))
            except Exception:
                continue

        for scope in scopes:
            for selector in prompt_selectors:
                try:
                    nodes = scope.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    continue
                for node in nodes:
                    value = (node.text or '').strip()
                    if value and value not in candidates:
                        candidates.append(value)
            if candidates:
                break

        if not candidates:
            try:
                body_text = (container.text or '').strip()
            except Exception:
                body_text = ''
            if body_text:
                candidates.append(body_text)
        if directions:
            candidates.insert(0, directions.strip())

        joined = "\n".join(c for c in candidates if c)
        if joined:
            return joined[:1200]
        if items and items[0].get('name'):
            return items[0]['name']
        return "录音题（跟读/自由作答）"


class VideoDubStrategy(QuestionParserStrategy):
    """视频配音题：一个视频 + 多条字幕，每条单独录音。"""

    CONTAINER_SELECTOR = '.question-video-dub-pc'
    ROW_SELECTOR = '.question-common-abs-reply > .item'

    def can_parse(self, container, driver) -> bool:
        try:
            if not container.find_elements(By.CSS_SELECTOR, self.ROW_SELECTOR):
                return False
            return bool(container.find_elements(
                By.CSS_SELECTOR, '.ucomp-recorder .button-record, .record-button-group'))
        except Exception:
            return False

    def parse(self, container, driver, question_number: int,
              directions: str = "") -> Optional[Question]:
        # 容器可能是外层节点，收敛到配音题本身。
        if not container.find_elements(By.CSS_SELECTOR, self.ROW_SELECTOR):
            try:
                container = container.find_element(By.CSS_SELECTOR, self.CONTAINER_SELECTOR)
            except Exception:
                return None

        rows = container.find_elements(By.CSS_SELECTOR, self.ROW_SELECTOR)
        items = []
        for idx, row in enumerate(rows):
            try:
                text = (row.find_element(
                    By.CSS_SELECTOR, '.component-htmlview').text or '').strip()
            except Exception:
                text = ''
            if not text:
                continue
            items.append({
                'index': idx,
                'text': text,
                'name': text,
                'url': '',
                'duration': 0.0,
                'needs_tts': True,
                'element': row,
                'row_index': idx,
            })
        if not items:
            return None

        title = f"视频配音（共{len(items)}句）"
        text = f"【题目要求】{directions}\\n\\n{title}" if directions else title
        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.RECORD,
            element=container,
            directions=directions,
            record_items=items,
        )


class TextInputStrategy(QuestionParserStrategy):
    """文本输入题策略"""

    WRITING_KEYWORDS = ['topic', 'topic sentence', 'outline', 'things to do',
                        'concluding sentence', 'more topics']
    READING_MIN_LENGTH = 400

    def can_parse(self, container, driver) -> bool:
        textareas = container.find_elements(By.CSS_SELECTOR,
                                            'textarea.question-textarea-content, textarea.question-inputbox-input, textarea.scoopFill_textarea')
        if not textareas:
            return False

        container_class = container.get_attribute('class') or ''
        is_single_reply = 'question-common-abs-reply' in container_class

        if is_single_reply:
            has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
            if has_inputbox:
                return True

            has_scoop_container = container.find_elements(By.CSS_SELECTOR, '.question-common-abs-scoop, .fe-scoop')
            if has_scoop_container:
                return True

            return False

        has_material = container.find_elements(By.CSS_SELECTOR, '.layout-material-container')

        if has_material:
            material_text = ""
            try:
                material = container.find_element(By.CSS_SELECTOR, '.layout-material-container')
                material_text = material.text.lower()
            except:
                pass

            if any(kw in material_text for kw in self.WRITING_KEYWORDS):
                has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
                if has_scoop:
                    return True

            if any(kw in material_text for kw in ['model', 'example', '示例', '例句']):
                return True

        direction_text = ""
        try:
            direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container .component-htmlview')
            direction_text = direction_elem.text.lower()
        except:
            direction_elem = WebDriverHelper.safe_find_element(
                driver, ['.layout-direction-container .content', '.abs-direction .content'], container)
            if direction_elem:
                direction_text = direction_elem.text.lower()

        if direction_text:
            if any(kw in direction_text for kw in ['write', 'essay', 'composition', 'paragraph']):
                has_options = container.find_elements(By.CSS_SELECTOR, '.option-wrapper, .banked-options, .option-wrap')
                has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
                if not has_options and has_scoop:
                    return True

            if any(kw in direction_text for kw in ['answer', 'question', 'according to']):
                has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
                has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
                if has_inputbox and not has_scoop:
                    return True

        if len(textareas) >= 2:
            has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
            has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
            if has_inputbox and not has_scoop:
                return True

        if len(textareas) == 1:
            rows = textareas[0].get_attribute('rows')
            is_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
            if rows and int(rows) >= 5 and is_scoop:
                return True

        return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        try:
            effective_directions = directions

            if not effective_directions:
                try:
                    direction_elem = driver.find_element(By.CSS_SELECTOR,
                                                         ".layout-direction-container .component-htmlview")
                    effective_directions = direction_elem.text.strip()
                except:
                    pass

            container_class = container.get_attribute('class') or ''
            is_single_reply = 'question-common-abs-reply' in container_class

            has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
            has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')

            if is_single_reply:
                if has_inputbox and not has_scoop:
                    is_writing = False
                elif has_scoop and not has_inputbox:
                    is_writing = True
                else:
                    is_writing = self._check_is_writing_by_material(container, driver)
            else:
                is_writing = self._check_is_writing_by_material(container, driver)

            material_text = self._extract_material_text(container, driver)

            items = []

            if is_writing:
                scoop_containers = container.find_elements(By.CSS_SELECTOR, '.fe-scoop')
                for i, scoop in enumerate(scoop_containers, 1):
                    try:
                        number_elem = scoop.find_element(By.CSS_SELECTOR, '.question-number')
                        number = number_elem.text.strip()

                        textarea = scoop.find_element(By.CSS_SELECTOR, 'textarea.question-textarea-content')
                        placeholder = textarea.get_attribute('placeholder') or "写作"

                        items.append({
                            'index': i,
                            'words': f"题{number}: {placeholder}",
                            'input': textarea,
                            'question_text': placeholder
                        })
                    except Exception as e:
                        continue
            else:
                if is_single_reply:
                    input_boxes = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
                else:
                    input_boxes = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')

                for i, box in enumerate(input_boxes, question_number):
                    try:
                        header = box.find_element(By.CSS_SELECTOR, '.question-inputbox-header')
                        question_text = header.text.strip()
                        question_text = re.sub(rf'^\d+[\s.、)]+', '', question_text)

                        textarea = box.find_element(By.CSS_SELECTOR, 'textarea')

                        items.append({
                            'index': i,
                            'words': question_text,
                            'input': textarea,
                            'question_text': question_text
                        })
                    except Exception as e:
                        continue

            if not items:
                return None

            full_text = ""
            if effective_directions:
                full_text += f"【题目要求】{effective_directions}\n\n"

            if material_text:
                if is_writing:
                    full_text += f"【写作提纲/材料】\n{material_text[:500]}\n\n"
                else:
                    full_text += f"【阅读材料】\n{material_text[:6000]}\n\n"

            full_text += "【问题列表】\n"
            for item in items:
                if is_writing:
                    full_text += f"{item['index']}. {item['words']}\n"
                else:
                    full_text += f"{item['index']}. {item['question_text']}\n"

            if is_single_reply and not is_writing and len(items) == 1:
                item = items[0]
                return Question(
                    number=item['index'],
                    text=full_text,
                    q_type=QuestionType.TEXT,
                    element=container,
                    inputs=[item['input']],
                    banked_blanks=[item],
                    directions=effective_directions,
                )

            return Question(
                number=question_number,
                text=full_text,
                q_type=QuestionType.TEXT,
                element=container,
                inputs=[item['input'] for item in items],
                banked_blanks=items,
                directions=effective_directions,
            )

        except Exception as e:
            error_msg = str(e)
            print(f"      TextInputStrategy解析失败: {error_msg[:100]}")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return None

    def _check_is_writing_by_material(self, container=None, driver=None) -> bool:
        material_text = self._extract_material_text(container, driver)
        if material_text:
            material_lower = material_text.lower()
            return any(kw in material_lower for kw in self.WRITING_KEYWORDS)
        return False

    def _extract_material_text(self, container=None, driver=None) -> str:
        material_text = ""
        try:
            if container:
                try:
                    material = container.find_element(By.CSS_SELECTOR, '.layout-material-container')
                    material_text = material.text.strip()
                except:
                    pass
            if not material_text and driver:
                try:
                    material = driver.find_element(By.CSS_SELECTOR, '.layout-material-container')
                    material_text = material.text.strip()
                except:
                    pass
        except:
            pass
        return material_text


class ListeningFillInStrategy(QuestionParserStrategy):
    """音视频填空题解析策略（仅解析题目，转录由预处理完成）"""

    def can_parse(self, container, driver) -> bool:
        has_blanks = bool(container.find_elements(By.CSS_SELECTOR, '.fe-scoop input'))
        has_option_pool = bool(container.find_elements(By.CSS_SELECTOR, '.option-wrapper, .banked-options'))
        if not has_blanks or has_option_pool:
            return False

        try:
            direction = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
            text = direction.text.lower()
            has_media_hint = any(kw in text for kw in [
                'listen', 'audio', 'hear', 'talk', 'conversation',
                'watch', 'video', 'clip', 'view'
            ])
            has_fill_hint = any(kw in text for kw in [
                'fill in', 'complete', 'blank', 'blanks'
            ])
            return has_media_hint and has_fill_hint
        except:
            return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        inputs = container.find_elements(By.CSS_SELECTOR, '.fe-scoop input')
        if not inputs:
            return None

        media_label = "视频填空题" if self._is_video_fill_page(directions) else "听力填空题"
        full_text = f"{media_label}（共{len(inputs)}个空）"
        if directions:
            full_text = f"【题目要求】{directions}\n\n{full_text}"

        blank_contexts = []
        for i, inp in enumerate(inputs):
            scoop = None
            try:
                scoop = inp.find_element(By.XPATH, './ancestor::span[@class="fe-scoop"]')
                sentence = self._extract_blank_sentence(scoop)
            except Exception:
                sentence = ""
            left_context, right_context = self._split_blank_context(sentence, i + 1)
            blank_contexts.append({
                'index': i,
                'sentence': sentence,
                'label': self._extract_row_label(scoop, sentence) if scoop is not None else '',
                'left_context': left_context,
                'right_context': right_context,
                'input': inp
            })

        return Question(
            number=question_number,
            text=full_text,
            q_type=QuestionType.LISTENING_FILL_IN,
            element=container,
            inputs=inputs,
            banked_blanks=blank_contexts,
            directions=directions,
        )

    @staticmethod
    def _is_video_fill_page(directions: str) -> bool:
        direction_text = (directions or "").lower()
        return any(kw in direction_text for kw in ['watch', 'video', 'clip', 'view'])

    def _extract_blank_sentence(self, scoop) -> str:
        for xpath in ['./ancestor::td[1]', './ancestor::p[1]', './ancestor::div[1]']:
            try:
                text = scoop.find_element(By.XPATH, xpath).text.strip()
                if text:
                    return text
            except:
                continue
        return ""

    @staticmethod
    def _extract_row_label(scoop, sentence: str) -> str:
        """表格题的行标题（如 Opening date），模型靠它才知道该填哪种信息。"""
        if scoop is None:
            return ""
        try:
            cells = scoop.find_elements(By.XPATH, './ancestor::tr[1]/*[self::td or self::th]')
        except Exception:
            return ""
        if len(cells) < 2:
            return ""
        try:
            label = (cells[0].text or "").strip()
        except Exception:
            return ""
        return "" if label == sentence else label

    @staticmethod
    def _split_blank_context(sentence: str, blank_number: int) -> Tuple[str, str]:
        if not sentence:
            return "", ""
        patterns = [
            rf'{blank_number}\s*[）).、:：]',
            rf'{blank_number}\s*[_—-]+',
            rf'{blank_number}\s*[）).、:：]?\s*空\s*{blank_number}?\s*[:：]?',
            rf'空\s*{blank_number}\s*[:：]?',
        ]
        for pattern in patterns:
            match = re.search(pattern, sentence)
            if match:
                return sentence[:match.start()].strip(), sentence[match.end():].strip()
        match = re.search(rf'(?<!\d){blank_number}(?!\d)', sentence)
        if match:
            return sentence[:match.start()].strip(), sentence[match.end():].strip()
        return sentence, ""

class FillInStrategy(QuestionParserStrategy):
    """填空题解析策略"""

    FILL_INPUTS = [
        'input.fill-blank--bc-input-DelG1',
        '.fe-scoop input:not([type="hidden"])',
        '.comp-abs-input input',
        '.blankinput',
        'input[type="text"]',
    ]

    def can_parse(self, container, driver) -> bool:
        # 先看空位：搭配填空（un____ la____）也是 .fe-scoop，不能因为页面上有材料就整页放弃
        scoops = container.find_elements(By.CSS_SELECTOR, '.fe-scoop input:not([type="hidden"])')
        has_options = container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option, .banked-options')
        if scoops and not has_options:
            return True

        has_material_container = container.find_elements(
            By.CSS_SELECTOR, '.layout-material-container'
        )
        if has_material_container:
            return False

        has_textarea = container.find_elements(
            By.CSS_SELECTOR, 'textarea.question-textarea-content'
        )
        if has_textarea:
            return False

        inputs = WebDriverHelper.safe_find_elements(driver, self.FILL_INPUTS, container)

        if len(inputs) >= 2:
            return True

        if len(inputs) == 1:
            inp = inputs[0]
            placeholder = inp.get_attribute('placeholder') or ''
            if 'word' in placeholder.lower() or '不少于' in placeholder:
                return False
            return True

        return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        title_elem = WebDriverHelper.safe_find_element(driver, Selectors.QUESTION_TITLE, container)
        text = title_elem.text.strip() if title_elem else ""
        text = re.sub(r"^\d+[.、)\]]\s*", "", text)

        scoops = container.find_elements(By.CSS_SELECTOR, '.fe-scoop input:not([type="hidden"])')
        if scoops:
            inputs = MatchingStrategy._sort_inputs(scoops)
        else:
            inputs = WebDriverHelper.safe_find_elements(driver, self.FILL_INPUTS, container)
        # 播放器会把其它练习/其它 Tab 的题也留在 DOM 里，必须只取当前可见的空位
        visible_inputs = []
        for inp in inputs:
            try:
                if inp.getClientRects():
                    visible_inputs.append(inp)
            except Exception:
                continue
        if visible_inputs:
            inputs = visible_inputs

        blanks = []
        segments = MatchingStrategy._container_segments(driver, container)
        for index, inp in enumerate(inputs):
            context = MatchingStrategy._text_before_blank(driver, container, inp, segments)
            context = re.sub(r'\s+', ' ', context).strip()
            if not context:
                context = MatchingStrategy._statement_for(inp)
            # 只要空位附近的一小段，避免把整段文章都塞给模型
            context = context[-180:] if len(context) > 180 else context
            blanks.append({
                'index': index,
                'context': context,
                'right_context': (
                    MatchingStrategy._leading_text(inp).get('trailing') or ''
                ).strip(),
                'hint': MatchingStrategy._hint_before(inp),
                'input': inp,
            })

        header = f"填空题（共{len(inputs)}个空）"
        material = self._page_material(container)
        if material:
            header += f"\n【题面/词库】{material}"
        if directions:
            header = f"【题目要求】{directions}\n{header}"

        return Question(
            number=question_number,
            text=header,
            q_type=QuestionType.FILL_IN,
            element=container,
            inputs=inputs,
            banked_blanks=blanks,
            directions=directions,
        )

    @staticmethod
    def _page_material(container, limit: int = 6000) -> str:
        """取当前可见题面（含词库），让模型知道可选的搭配/单词表。

        Directions 和词库都排在题面最前面，原来用 text[-limit:] 从尾部截断，
        Expressions in use 这类 10 句以上的题组会把整段词库切掉（模型只能盲猜）。
        改为从头保留，并把上限放宽到能容纳整页题面。
        """
        try:
            text = container.parent.execute_script("""
                const root = arguments[0];
                const clone = root.cloneNode(true);
                for (const el of clone.querySelectorAll('input, textarea, .fe-scoop')) { el.remove(); }
                return (clone.textContent || '').replace(/\\s+/g, ' ').trim();
            """, container) or ""
        except Exception:
            text = ""
        return text[:limit] if len(text) > limit else text


class AudioStrategy(QuestionParserStrategy):
    """只有听力材料、没有作答控件的页面。"""

    ANSWER_SELECTOR = ('video, textarea, input:not([type="hidden"]), .fe-scoop, '
                       '.question-common-abs-choice, .option, .option-wrap, '
                       '.ant-radio-wrapper, .ant-checkbox-wrapper, '
                       '.oral-paragraph-container, .oral-study-sentence')

    def can_parse(self, container, driver) -> bool:
        if container.find_elements(By.CSS_SELECTOR, self.ANSWER_SELECTOR):
            return False
        if find_record_buttons(driver, container):
            return False
        return bool(container.find_elements(By.CSS_SELECTOR, 'audio[src]:not([src=""]), audio source[src]'))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        return Question(number=question_number, text="音频听读页面",
                        q_type=QuestionType.AUDIO, element=container, directions=directions)

    @classmethod
    def is_audio_only_page(cls, driver) -> bool:
        containers = driver.find_elements(By.CSS_SELECTOR, '.layoutBody-container')
        visible = [node for node in containers if node.is_displayed()]
        if not visible:
            return False
        # 排序/填空/选择页也有音频，但不能当纯听音频页跳过转写。
        if driver.find_elements(
                By.CSS_SELECTOR,
                '.sequence-view, .sortable-list-wrapper, .question-common-abs-fill, '
                '.question-common-abs-choice, .question-common-abs-dropdown'):
            return False
        return all(cls().can_parse(node, driver) for node in visible)


class VideoStrategy(QuestionParserStrategy):
    """纯视频页面检测策略"""

    def can_parse(self, container, driver) -> bool:
        videos = container.find_elements(By.TAG_NAME, 'video')
        if not videos:
            videos = container.find_elements(By.CSS_SELECTOR,
                                             '.video-js, .video-box, .question-video-player, video')

        if not videos:
            return False

        popup_questions = container.find_elements(By.CSS_SELECTOR,
                                                  '.popupBox .question-common-abs-choice, .questionReplyBox .question-common-abs-choice')

        if popup_questions:
            return False

        has_real_questions = (
                container.find_elements(By.CSS_SELECTOR,
                                        '.question-common-abs-choice:not(.popupBox *), '
                                        '.question-inputbox:not(.popupBox *), '
                                        '.option-wrap:not(.popupBox *), '
                                        '.fe-scoop') or
                container.find_elements(By.CSS_SELECTOR,
                                        'input[type="text"]:not(.ant-input)')
        )

        if has_real_questions:
            return False

        return True

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        return Question(
            number=question_number,
            text="视频观看页面",
            q_type=QuestionType.VIDEO,
            element=container
        )


class VocabularyFlashcardStrategy(QuestionParserStrategy):
    """单词闪卡策略"""

    def can_parse(self, container, driver) -> bool:
        flashcard_indicators = [
            '.vocContainer',
            '.vocabulary-flashcard',
            '.flashcard-container',
            '.vocActions',
            '.vocabulary-actions'
        ]

        for indicator in flashcard_indicators:
            if container.find_elements(By.CSS_SELECTOR, indicator):
                has_choice = container.find_elements(By.CSS_SELECTOR, '.option-wrap, .question-common-abs-choice')
                if not has_choice:
                    return True

        return False

    def parse(self, container, driver, question_number: int, direction: str = "") -> Optional[Question]:
        return Question(
            number=question_number,
            text="单词闪卡",
            q_type=QuestionType.VOCABULARY_FLASHCARD,
            element=container
        )


class DropdownSelectStrategy(QuestionParserStrategy):
    """下拉选择填空题解析策略"""

    def can_parse(self, container, driver) -> bool:
        selects = container.find_elements(By.CSS_SELECTOR, DROPDOWN_TRIGGER_SELECTOR)
        return bool(selects) or has_visible_dropdown(driver, container)

    @staticmethod
    def _cloze_material(driver, container) -> str:
        """取整页题干，把每个下拉触发器编号成 (空1: ____) ...，模型才能看见全部空位与编号。"""
        try:
            raw = driver.execute_script("""
                const clone = arguments[0].cloneNode(true);
                let idx = 0;
                const wrappers = clone.querySelectorAll('.scoop-select-wrapper, select, .ant-select');
                if (wrappers.length) {
                    wrappers.forEach(w => {
                        idx += 1;
                        w.replaceWith(document.createTextNode(' (空' + idx + ': ____) '));
                    });
                } else {
                    const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
                    let n;
                    while ((n = walker.nextNode())) {
                        if ((n.textContent || '').trim() === '点击选择') {
                            idx += 1;
                            n.textContent = ' (空' + idx + ': ____) ';
                        }
                    }
                }
                return (clone.textContent || '').trim();
            """, container) or ""
        except Exception:
            return ""
        return re.sub(r'\s+', ' ', raw).strip()

    @staticmethod
    def _peek_dropdown_options(driver, trigger) -> List[str]:
        """点开第一个下拉抓候选词再收起；新版 Cloze 的选项只在浮层里。"""
        try:
            ActionChains(driver).move_to_element(trigger).click().perform()
            time.sleep(0.5)
            items = driver.find_elements(By.CSS_SELECTOR,
                                         '.ant-select-item-option, .ant-dropdown-menu-item, '
                                         '[role="option"], li[class*="option"]')
            options = []
            for item in items:
                try:
                    if not item.is_displayed():
                        continue
                    label = (item.text or '').strip()
                    if label and label != CLOZE_DROPDOWN_PLACEHOLDER and label not in options:
                        options.append(label)
                except Exception:
                    continue
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            return options
        except Exception:
            return []

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        blanks = []
        select_elements = container.find_elements(By.CSS_SELECTOR, '.scoop-select-wrapper')
        # 新版 Cloze 没有 .scoop-select-wrapper：回退到通用探测，
        # 否则 banked_blanks 为空会被 Question.is_interactive() 判成非交互题。
        if not select_elements:
            select_elements = find_dropdown_triggers(driver, container)

        for i, elem in enumerate(select_elements):
            context = ""
            try:
                context_elem = elem.find_element(By.XPATH,
                                                 './ancestor::li[1] | ./ancestor::p[1]')
                raw = driver.execute_script("""
                    const box = arguments[0], trigger = arguments[1];
                    const sel = '.scoop-select-wrapper, select, .ant-select';
                    const allTriggers = Array.from(box.querySelectorAll(sel));
                    const targetIdx = allTriggers.indexOf(trigger);
                    const clone = box.cloneNode(true);
                    if (targetIdx >= 0) {
                        const clonedTriggers = Array.from(clone.querySelectorAll(sel));
                        if (clonedTriggers[targetIdx]) {
                            clonedTriggers.forEach((el, k) => {
                                el.replaceWith(document.createTextNode(
                                    k === targetIdx ? ' 【此处填空】 ' : ' ____ '));
                            });
                            return (clone.textContent || '').trim();
                        }
                    }
                    const label = (trigger.textContent || '').trim();
                    const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
                    let n, done = false;
                    while ((n = walker.nextNode())) {
                        if (!done && label && (n.textContent || '').trim() === label) {
                            n.textContent = ' 【此处填空】 ';
                            done = true;
                        }
                    }
                    return (clone.textContent || '').trim();
                """, context_elem, elem) or ""
                context = re.sub(r'\s+', ' ', raw).strip()
            except Exception:
                try:
                    context = elem.find_element(By.XPATH, './ancestor::p[1]').text.strip()
                except Exception:
                    context = ""

            options = []
            try:
                hidden_div = elem.find_element(By.CSS_SELECTOR, 'div[style*="visibility: hidden"]')
                option_elems = hidden_div.find_elements(By.TAG_NAME, 'i')
                for opt in option_elems:
                    # visibility: hidden 下 Selenium .text 为空串，必须优先取 textContent
                    text = (opt.get_attribute('textContent') or opt.text or '').strip()
                    if text and text != CLOZE_DROPDOWN_PLACEHOLDER and text not in options:
                        options.append(text)
            except Exception:
                try:
                    select = elem.find_element(By.TAG_NAME, 'select')
                    option_elems = select.find_elements(By.TAG_NAME, 'option')
                    for opt in option_elems:
                        text = (opt.get_attribute('textContent') or opt.text or '').strip()
                        if text and text not in ('', CLOZE_DROPDOWN_PLACEHOLDER) and text not in options:
                            options.append(text)
                except Exception:
                    pass

            blanks.append({
                'index': i,
                'context': context,
                'element': elem,
                'options': options
            })

        title_elem = WebDriverHelper.safe_find_element(driver, Selectors.QUESTION_TITLE, container)
        text = title_elem.text.strip() if title_elem else "下拉选择填空"
        if directions:
            text = directions + text
        material = self._cloze_material(driver, container)
        if material:
            text += "\n【全文】" + material[:6000]

        banked_options = list(dict.fromkeys(
            opt for b in blanks for opt in b['options']))
        if not banked_options and blanks:
            banked_options = self._peek_dropdown_options(driver, blanks[0]['element'])
        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.DROPDOWN_SELECT,
            element=container,
            banked_blanks=blanks,
            banked_options=banked_options
        )


class ParagraphMatchingStrategy(QuestionParserStrategy):
    """段落信息匹配题（Skimming and scanning）：读字母段落，给陈述填段落字母。

    DOM（Section B Extensive reading / Skimming and scanning）：
      .layout-material-container .component-htmlview ol[upper-alpha] > li  → 段落
      .question-common-abs-textmatch .input-wrapper
          > input.textbox                       → 每题一个字母输入框
          > .component-htmlview p              → 陈述文本

    FillInStrategy 见到 .layout-material-container 会直接放弃；MatchingStrategy
    只认 .fe-scoop + A-F 人物题；两者都覆盖不到本结构，必须单独识别。
    段落字母由 CSS `list-style-type: upper-alpha` 生成、不在 textContent 里，
    所以要按 <li> 顺序补上 A./B./… 前缀，字母池随段落数动态生成。
    """

    REPLY_SELECTOR = '.question-common-abs-textmatch .input-wrapper'
    INPUT_SELECTOR = REPLY_SELECTOR + ' input.textbox'

    def can_parse(self, container, driver) -> bool:
        return len(container.find_elements(
            By.CSS_SELECTOR, self.INPUT_SELECTOR)) >= 2

    @staticmethod
    def _paragraph_material(driver, container) -> Dict:
        try:
            raw = driver.execute_script("""
                var root = arguments[0];
                var scope = root.querySelector('.layout-material-container') || root;
                var out = [], letters = [];
                var title = scope.querySelector('.component-htmlview > p');
                if (title) out.push((title.innerText || title.textContent || '').trim());
                var ol = scope.querySelector('ol');
                if (ol) {
                    var lis = [].filter.call(ol.children, function (n) {
                        return n.tagName === 'LI';
                    });
                    for (var i = 0; i < lis.length; i++) {
                        var L = String.fromCharCode(65 + i);
                        letters.push(L);
                        out.push(L + '. ' + (lis[i].innerText || lis[i].textContent || '').trim());
                    }
                }
                if (!out.length) out.push((scope.innerText || scope.textContent || '').trim());
                return {text: out.join('\\n'), letters: letters};
            """, container)
            if not isinstance(raw, dict):
                return {'text': '', 'letters': []}
            text = re.sub(r'\n{3,}', '\n\n', raw.get('text') or '').strip()
            return {'text': text, 'letters': raw.get('letters') or []}
        except Exception:
            return {'text': '', 'letters': []}

    @staticmethod
    def _statements(driver, container) -> List[str]:
        try:
            raw = driver.execute_script("""
                var root = arguments[0];
                return [].map.call(
                    root.querySelectorAll('.question-common-abs-textmatch .input-wrapper'),
                    function (w) {
                        var p = w.querySelector('.component-htmlview');
                        return p ? (p.innerText || p.textContent || '').trim() : '';
                    });
            """, container) or []
        except Exception:
            return []
        return [re.sub(r'\s+', ' ', str(s)).strip() for s in raw]

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        inputs = container.find_elements(By.CSS_SELECTOR, self.INPUT_SELECTOR)
        statements = self._statements(driver, container)
        if len(statements) < len(inputs):
            statements += [''] * (len(inputs) - len(statements))
        material = self._paragraph_material(driver, container)
        text = material.get('text') or ''
        if directions:
            text = '【题目要求】' + directions + '\n' + text
        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.PARAGRAPH_MATCH,
            element=container,
            inputs=inputs,
            banked_options=material.get('letters') or [],
            banked_blanks=[{'index': i, 'statement': s}
                           for i, s in enumerate(statements[:len(inputs)])],
            directions=directions,
        )


class MatchingStrategy(QuestionParserStrategy):
    """人物 A-F ↔ 陈述 的匹配题。

    典型题面（视听说 Watching street interviews - Exercise 3）：
      Directions: ... match the people to the answers. Filling the blanks with A-F.
      A B C D E F（照片）
      1. the environment ___  2. poverty ___ ...
    这类题每个空只填一个字母，旧版会把它们当成听力填空让模型写单词，必然 0 分。
    """

    MATCH_DIRECTION = re.compile(
        r'match\s+the\s+(?:people|persons?|speakers?|statements?|answers?)'
        r'|fill(?:ing)?\s+(?:in\s+)?the\s+blanks?\s+with\s+(?:the\s+)?(?:letters?\s+)?'
        r'(?:[A-Z]\s*[-–—~]\s*[A-Z])'
        r'|choose\s+the\s+correct\s+letter',
        re.I)

    @classmethod
    def is_matching_direction(cls, directions: str) -> bool:
        return bool(cls.MATCH_DIRECTION.search(directions or ""))

    @staticmethod
    def _page_direction(driver) -> str:
        for selector in (
                '.layout-direction-container .component-htmlview',
                '.layout-direction-container',
                '.abs-direction .content',
                '.abs-direction'):
            try:
                text = driver.find_element(By.CSS_SELECTOR, selector).text.strip()
                if text:
                    return text
            except Exception:
                continue
        return ""

    def can_parse(self, container, driver) -> bool:
        blanks = container.find_elements(By.CSS_SELECTOR, '.fe-scoop input')
        if len(blanks) < 2:
            return False
        return self.is_matching_direction(self._page_direction(driver))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        inputs = container.find_elements(By.CSS_SELECTOR, '.fe-scoop input')
        inputs = self._sort_inputs(inputs)

        segments = self._container_segments(driver, container)
        numbered = []
        for item in segments:
            text = re.sub(r'\s+', ' ', (item.get('text') or '')).strip()
            if re.match(r'^\d{1,2}\s*[.、)]', text):
                numbered.append(self._clean_statement(text))

        statements = []
        for index, inp in enumerate(inputs):
            hint = GenericReplyStrategy._hint_before(inp)
            if len(numbered) == len(inputs):
                statement = numbered[index]
            else:
                raw = self._text_before_blank(driver, container, inp, segments)
                statement = self._clean_statement(re.sub(r'\s+', ' ', raw))
            if not statement:
                statement = self._clean_statement(re.sub(r'\s+', ' ', self._statement_for(inp)))
            statements.append({
                'index': index,
                'statement': statement,
                'hint': hint,
                'input': inp,
            })

        letters = self._option_letters(container)
        if not letters:
            letters = [chr(65 + i) for i in range(min(8, max(2, len(inputs))))]

        text_lines = ["【匹配题】把陈述与人物字母对应起来，每个空只填一个字母。"]
        if directions:
            text_lines.append(f"题目要求：{directions}")
        text_lines.append(f"可选字母：{' / '.join(letters)}")
        text_lines.append("陈述：")
        for item in statements:
            text_lines.append(f"  {item['index'] + 1}. {item['statement']}")

        return Question(
            number=question_number,
            text="\n".join(text_lines),
            q_type=QuestionType.MATCHING,
            element=container,
            inputs=inputs,
            banked_blanks=statements,
            banked_options=letters,
            options=[Option(letter=letter, text="", element=None) for letter in letters],
            directions=directions,
        )

    @staticmethod
    def _clean_statement(segment: str) -> str:
        """把「A B C D E F 1. the environment」清成「1. the environment」。"""
        text = (segment or "").strip()
        if not text:
            return ""
        # 去掉开头的人物字母行
        text = re.sub(r'^(?:[A-H]\s*){2,}', '', text).strip()
        # 只保留最后一小节（避免把前面几题重复带进来）
        parts = re.split(r'(?=(?<!\d)\d{1,2}\s*[.、)]\s)', text)
        parts = [p.strip() for p in parts if p.strip()]
        if parts:
            text = parts[-1]
        return text[-200:]

    @staticmethod
    def _sort_inputs(inputs: List[Any]) -> List[Any]:
        def key(inp):
            try:
                scoop = inp.find_element(By.XPATH, './ancestor::span[contains(@class, "fe-scoop")][1]')
                return int(scoop.get_attribute('data-scoop-index') or 0)
            except Exception:
                return 0

        ordered = list(inputs)
        try:
            ordered.sort(key=key)
        except Exception:
            pass
        return ordered

    @staticmethod
    def _statement_for(inp) -> str:
        for xpath in ('./ancestor::td[1]', './ancestor::p[1]', './ancestor::li[1]', './ancestor::div[1]'):
            try:
                node = inp.find_element(By.XPATH, xpath)
                text = MatchingStrategy._node_text_without_input(node)
                if text:
                    return text
            except Exception:
                continue
        return ""

    @staticmethod
    def _container_segments(driver, container) -> List[str]:
        """按空位切分整段题面，返回每空前面那段文字（不含空位本身的输入框）。"""
        try:
            return driver.execute_script("""
                const root = arguments[0];
                const out = [];
                let buf = '';
                const walk = (n) => {
                  for (const child of n.childNodes) {
                    if (child.nodeType === 3) { buf += child.textContent; continue; }
                    if (child.nodeType !== 1) continue;
                    if (child.tagName === 'INPUT') { continue; }
                    if (child.classList && child.classList.contains('fe-scoop')) {
                      const idx = child.getAttribute('data-scoop-index');
                      out.push({text: buf, idx: idx === null ? null : Number(idx)});
                      buf = '';
                      continue;
                    }
                    walk(child);
                  }
                };
                walk(root);
                out.push({text: buf, idx: null});
                return out;
            """, container) or []
        except Exception:
            return []

    @staticmethod
    def _text_before_blank(driver, container, inp, segments=None) -> str:
        """按 data-scoop-index 找回「本空前面那段文字」。"""
        segments = segments if segments is not None else MatchingStrategy._container_segments(driver, container)
        if not segments or not isinstance(segments[0], dict):
            return ""
        try:
            scoop = inp.find_element(By.XPATH, './ancestor::span[contains(@class, "fe-scoop")][1]')
            index = int(scoop.get_attribute('data-scoop-index') or 0)
        except Exception:
            return ""
        for item in segments:
            if item.get('idx') == index:
                return (item.get('text') or '').strip()
        return ""

    @staticmethod
    def _leading_text(inp) -> Dict[str, str]:
        try:
            return inp.parent.execute_script("""
                const input = arguments[0];
                const scoop = input.closest('.fe-scoop');
                if (!scoop) return {leading: '', hint: ''};
                let node = scoop.previousSibling, out = '';
                for (let guard = 0; guard < 12 && node; guard++) {
                  if (node.nodeType === 1 && node.querySelector && node.querySelector('.fe-scoop')) break;
                  out = (node.textContent || '') + out;
                  if (node.nodeType === 1 && /^(P|DIV|TD|LI|TR|TABLE|SECTION)$/.test(node.tagName)) break;
                  node = node.previousSibling;
                }
                const rawTail = out;
                out = out.replace(/\\s+/g, ' ').trim();
                // 只有「紧贴空位、结尾没有空白」的 1-4 个字母才算教材给的首字母提示
                // （un____ / f____）；「in a ____」里的 a 只是普通单词
                let hint = '';
                const m = rawTail.match(/([A-Za-z]{1,4})$/);
                if (m && !/\\s$/.test(rawTail)) {
                  hint = m[1];
                  out = out.slice(0, out.length - hint.length).trim();
                }
                // 空位后面的文字也要给模型：un____ la____ 这种要看到后半句才知道填什么
                let next = scoop.nextSibling, after = '';
                for (let guard = 0; guard < 8 && next; guard++) {
                  if (next.nodeType === 1 && next.querySelector && next.querySelector('.fe-scoop')) break;
                  after += (next.textContent || '');
                  if (next.nodeType === 1 && /^(P|DIV|TD|LI|TR|TABLE|SECTION)$/.test(next.tagName)) break;
                  next = next.nextSibling;
                }
                after = after.replace(/\\s+/g, ' ').trim();
                return {leading: out, hint: hint, trailing: after.slice(0, 90)};
            """, inp) or {}
        except Exception:
            return {}

    @staticmethod
    def _hint_before(inp) -> str:
        """空位前面紧挨着的首字母提示（un____ → 'un'）。"""
        try:
            hint = (MatchingStrategy._leading_text(inp).get('hint') or '').strip()
        except Exception:
            return ""
        return hint if looks_like_given_prefix(hint) else ""

    @staticmethod
    def _node_text_without_input(node) -> str:
        try:
            text = node.parent.execute_script("""
                const node = arguments[0];
                const clone = node.cloneNode(true);
                for (const el of clone.querySelectorAll('input, .fe-scoop')) { el.remove(); }
                // 脱离文档的克隆节点没有 layout，innerText 会是空串，必须用 textContent
                return (clone.textContent || '').replace(/\\s+/g, ' ').trim();
            """, node)
        except Exception:
            text = ''
        if not text:
            try:
                text = re.sub(r'\s+', ' ', (node.text or '')).strip()
            except Exception:
                text = ''
        return text

    @staticmethod
    def _option_letters(container) -> List[str]:
        letters = []
        try:
            cells = container.find_elements(By.CSS_SELECTOR, 'td, .caption')
        except Exception:
            cells = []
        for cell in cells:
            try:
                text = (cell.text or '').strip()
            except Exception:
                continue
            if re.fullmatch(r'[A-H]', text) and text not in letters:
                letters.append(text)
        if letters:
            return sorted(letters)
        # 兜底：从 A B C D E F 这类纯字母单元格拿不到时，用题面里出现的字母
        try:
            body = (container.text or '')
        except Exception:
            body = ''
        found = re.findall(r'\b([A-H])\b', body)
        unique = sorted(set(found))
        return unique if len(unique) >= 2 else []


class ChecklistStrategy(QuestionParserStrategy):
    """Wrapping up 自评清单（Got it 列表）。"""

    def can_parse(self, container, driver) -> bool:
        text = ""
        try:
            text = (container.text or '').lower()
        except Exception:
            return False
        if 'got it' not in text:
            return False
        has_table = bool(container.find_elements(By.CSS_SELECTOR, 'table, .ant-table-tbody'))
        has_icons = bool(container.find_elements(By.CSS_SELECTOR, '.anticon-border, [aria-label="border"]'))
        return has_table and has_icons

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        rows = []
        try:
            for row in container.find_elements(By.CSS_SELECTOR, 'tr.ant-table-row'):
                if 'category-name' in (row.get_attribute('class') or ''):
                    continue
                try:
                    text = row.find_element(By.CSS_SELECTOR, '.content-text').text.strip()
                except Exception:
                    continue
                try:
                    icon = row.find_element(By.CSS_SELECTOR, 'td:nth-child(2) .anticon')
                except Exception:
                    continue
                if text:
                    rows.append({'word': text, 'element': icon})
        except Exception:
            rows = []

        if not rows:
            return None

        return Question(
            number=question_number,
            text=f"自我评估清单（共{len(rows)}项，全部标记 Got it）",
            q_type=QuestionType.SELF_CHECK,
            element=container,
            banked_blanks=rows,
            directions=directions,
        )


class GenericReplyStrategy(QuestionParserStrategy):
    """兜底策略：任何带 textarea / 填空输入的容器都不再被整页放弃。

    用于翻译、总结、搭配填空这类结构各异但本质简单的页面：
      - textarea → TEXT（翻译方向会保留原文段落给模型）
      - scoop 输入 → FILL_IN（带首字母提示）
    """

    def can_parse(self, container, driver) -> bool:
        return bool(self._textareas(container)) or bool(self._scoop_inputs(container))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        textareas = self._textareas(container)
        if textareas:
            material = self._material_text(container, textareas[0])
            lines = []
            if directions:
                lines.append(f"【题目要求】{directions}")
            if material:
                lines.append(f"【原文/材料】{material}")
            if re.search(r'translat|翻译', directions or '', re.I):
                lines.append("请把上面的原文完整翻译成中文，只输出译文。")
            elif re.search(r'summari|总结|概括|main idea|general idea', directions or '', re.I):
                lines.append("请根据视频/音频内容用英文写 3-4 句总结，直接输出正文。")
            question = Question(
                number=question_number,
                text="\n\n".join(lines) or "请完成本页作答",
                q_type=QuestionType.TEXT,
                element=container,
                inputs=textareas,
                directions=directions,
            )
            return question

        inputs = self._scoop_inputs(container)
        if not inputs:
            return None
        blanks = []
        for index, inp in enumerate(inputs):
            blanks.append({
                'index': index,
                'context': MatchingStrategy._statement_for(inp),
                'hint': self._hint_before(inp),
                'input': inp,
            })
        return Question(
            number=question_number,
            text=f"填空题（共{len(inputs)}个空）",
            q_type=QuestionType.FILL_IN,
            element=container,
            inputs=inputs,
            banked_blanks=blanks,
            directions=directions,
        )

    @staticmethod
    def _textareas(container) -> List[Any]:
        found = []
        try:
            for area in container.find_elements(By.CSS_SELECTOR, 'textarea'):
                try:
                    if area.is_displayed():
                        found.append(area)
                except Exception:
                    continue
        except Exception:
            return []
        return found

    @staticmethod
    def _scoop_inputs(container) -> List[Any]:
        try:
            return MatchingStrategy._sort_inputs(
                container.find_elements(By.CSS_SELECTOR, '.fe-scoop input'))
        except Exception:
            return []

    @staticmethod
    def _material_text(container, textarea) -> str:
        for selector in ('.question-inputbox-header', '.layout-material-container',
                         '.question-common-abs-material', '.text-material-wrapper'):
            try:
                node = container.find_element(By.CSS_SELECTOR, selector)
                text = (node.text or '').strip()
                if len(text) > 20:
                    return text
            except Exception:
                continue
        try:
            raw = (container.text or '').strip()[:2000]
        except Exception:
            return ""
        # 去掉播放器控件文字，只留下真正有信息量的内容
        noise = ['倍速', 'Words & tips', 'Words &amp; tips', '播放', '暂停']
        lines = []
        for line in raw.splitlines():
            item = line.strip()
            if not item:
                continue
            if re.fullmatch(r'[\d:：/\s]+', item):
                continue
            if any(token in item for token in noise):
                continue
            lines.append(item)
        text = ' '.join(lines).strip()
        return text if len(text) > 20 else ""

    @staticmethod
    def _hint_before(inp) -> str:
        """抓空位前面紧挨着的首字母提示（如 un____ la____ 里的 un / la）。"""
        return MatchingStrategy._hint_before(inp)


class PromptBuilder:
    """Prompt构建器"""

    def __init__(self, ai_client, vocab_store: Optional["VocabularyStore"] = None):
        self.ai_client = ai_client
        self.vocab_store = vocab_store

    def build(self, questions: List[Question], global_directions: str = "") -> str:
        lines = []

        if len(self.ai_client.accumulated_passages) > 1:
            lines.append(f"【注意】本章节共有 {len(self.ai_client.accumulated_passages)} 份材料/音频转写，请根据问题判断使用哪份。")
            lines.append("")

        effective_directions = global_directions
        if not effective_directions and questions:
            effective_directions = questions[0].directions

        if effective_directions:
            lines.append(f"【题目指示】{effective_directions}")
            lines.append("")

        lines.extend(self._vocabulary_block(questions))

        type_counts = {}
        for q in questions:
            type_counts[q.q_type] = type_counts.get(q.q_type, 0) + 1

        if QuestionType.VOCABULARY_TEST in type_counts:
            lines.extend(self._vocabulary_test_hints())

        if QuestionType.BANKED_CLOZE in type_counts:
            lines.extend(self._banked_cloze_hints())

        for q in questions:
            builder_method = self._get_builder_method(q.q_type)
            lines.extend(builder_method(q))

        checks = []
        choice_types = {
            QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE,
            QuestionType.LISTENING_CHOICE, QuestionType.VIDEO_CHOICE,
            QuestionType.TRUE_FALSE,
        }
        if any(kind in type_counts for kind in choice_types):
            checks.append("回到材料核对选项或判断，排除与原文矛盾的表述。")
        if any(q.q_type in (QuestionType.FILL_IN, QuestionType.LISTENING_FILL_IN)
               and not q.banked_options for q in questions):
            checks.append("填空题逐空定位原句并抄写缺失词，不要添加修饰词。")
        if QuestionType.SORTING in type_counts:
            checks.append("排序题按各选项在原文中首次出现的先后排列。")
        if checks:
            lines.extend(["【核对要求】" + "".join(checks), ""])

        lines.extend(self._format_instructions(type_counts))

        return '\n'.join(lines)

    def build_writing(self, questions: List[Question], directions: str) -> str:
        lines = ["【写作任务】", directions]
        lines.extend(f"{q.number}. {q.text}" for q in questions)
        lines.append(
            "按题目指定的语言、体裁、受众和字数写完整正文（如未限定字数，写 220–300 词的完整四至五段英文成稿）。"
            "逐项核对任务要求，结合课程阅读材料给出具体论据，避免编造事实和引文。"
            "如果题目要求写提案，分段清晰交代背景与目标、2–3 项具体可落地措施与实施方式、以及预期成效与结语。"
            "注意满足高校英语写作高分标准：每段有清晰主题句、段间衔接自然（如 Furthermore, In addition, Consequently）、"
            "句式长短结合（含适当定语从句/分词结构/倒装或强调）、词汇精准地道、零拼写与语法错误。"
            "严禁输出 Markdown 符号（如 **、#、- 列表符）或中文说明，仅输出可直接粘贴提交的纯文本成稿。"
        )
        if len(questions) > 1:
            lines.append("按题号分别给出每篇成稿。")
        return '\n'.join(lines)

    def _needs_vocabulary(self, questions: List[Question]) -> bool:
        """题干里有中文括注、或要写口语稿时才需要把 Vocabulary 对照表塞给模型。"""
        for q in questions:
            if q.strict_word_bank:
                continue
            if q.q_type == QuestionType.RECORD:
                return True
            if q.q_type in (QuestionType.FILL_IN, QuestionType.TEXT,
                            QuestionType.BANKED_CLOZE, QuestionType.MY_VOICE_TEXT):
                if extract_cjk_hints(q.text or '') or CJK_CHAR_PATTERN.search(q.text or ''):
                    return True
        return False

    def _vocabulary_block(self, questions: List[Question]) -> List[str]:
        """把 Vocabulary 页收来的中英对照塞进上下文，并要求优先复用。"""
        if not self.vocab_store or not len(self.vocab_store):
            return []
        if not self._needs_vocabulary(questions):
            return []
        lines = ["【Vocabulary 对照表（来自本单元 Vocabulary 页，填空/口语稿的用词以它为准）】"]
        lines.extend(self.vocab_store.prompt_lines())
        lines.append("【要求】中文括注的空位若在下表里出现，用词必须取自表里的英文写法，不要自己意译或换同义词。")
        lines.append("【词形】词形由你按句子语法判断——该用 -ing / -ed / 复数 / 首字母大写时就改，"
                     "但词干必须与表里一致；改完要保证整句读起来通顺。")
        lines.append("")
        return lines

    def _vocabulary_test_hints(self) -> List[str]:
        return [
            "【重要提示】这是词汇测试题，包含以下类型：",
            "- 类型A（英文→中文）：题干是英文单词，选项是中文释义",
            "- 类型B（中文→英文）：题干是中文释义，选项是英文单词",
            "- 类型C（语境填空）：题干是英文句子，选项是单词填入",
            "请仔细分析每道题的具体类型，选择最准确的答案。\n"
        ]

    def _banked_cloze_hints(self) -> List[str]:
        return [
            "【重要提示】这是选词填空题，请从给定的单词/短语列表中选择最合适的填入空白处。\n",
            "【格式要求】只需返回答案本身，不要添加括号注释、不要解释、不要变形说明！",
            "【词形】若 Directions 要求 Change the form where necessary，答案必须是变形后的形式，不是词库原样。"
        ]

    def _get_builder_method(self, q_type: QuestionType) -> Callable[[Question], List[str]]:
        builders = {
            QuestionType.VOCABULARY_TEST: self._build_vocab_test,
            QuestionType.BANKED_CLOZE: self._build_banked_cloze,
            QuestionType.DROPDOWN_SELECT: self._build_dropdown_select,
            QuestionType.SINGLE_CHOICE: self._build_single_choice,
            QuestionType.MULTIPLE_CHOICE: self._build_multiple_choice,
            QuestionType.TRUE_FALSE: self._build_true_false,
            QuestionType.SORTING: self._build_sorting,
            QuestionType.FILL_IN: self._build_fill_in,
            QuestionType.TEXT: self._build_text,
            QuestionType.MY_VOICE_TEXT: self._build_my_voice_text,
            QuestionType.VIDEO: lambda q: [],
            QuestionType.VOCABULARY_FLASHCARD: lambda q: [],
            QuestionType.LISTENING_FILL_IN: self._build_listening_fill_in,
            QuestionType.LISTENING_CHOICE: self._build_listening_choice,
            QuestionType.VIDEO_CHOICE: self._build_video_choice,
            QuestionType.MATCHING: self._build_matching,
            QuestionType.PARAGRAPH_MATCH: self._build_paragraph_match,
            QuestionType.LINK_MATCH: self._build_link_match,
        }
        return builders.get(q_type, self._build_unknown)

    def _build_link_match(self, q: Question) -> List[str]:
        prompts = [item for item in q.link_items if item.get('side') == 'right']
        lines = [
            f"{q.number}. 【连线匹配】把每个提示和它对应的选项连起来",
            "   提示:",
        ]
        for index, item in enumerate(prompts):
            label = PublishQuestionParser._link_label(item.get('text', ''), number=True)
            lines.append(f"     {index + 1}. {label}")
        lines.append("   选项:")
        for opt in q.options:
            lines.append(f"     {opt.letter}. {opt.text}")
        lines.extend([
            "   请判断每个提示对应哪个选项（同一选项不应重复使用），",
            f"   只返回 {{提示序号}}.{{选项字母}}，例如 1.D 2.C 3.A 4.B（共 {len(prompts)} 组）",
            "",
        ])
        return lines

    def _build_matching(self, q: Question) -> List[str]:
        letters = q.banked_options or [opt.letter for opt in q.options]
        lines = [
            f"{q.number}. 【匹配题】把陈述和人物字母配对",
            "   本题的空里只需要填一个字母，不要写单词或句子。",
            f"   可选字母: {' / '.join(letters)}",
        ]
        for item in q.banked_blanks:
            lines.append(f"   空{item['index'] + 1}: {item.get('statement', '')}")
        lines.extend([
            "   请根据音频/视频转写判断每个人说了哪一句，",
            f"   只返回 {{空号}}.{{字母}}，例如 1.A 2.C（共 {len(q.banked_blanks)} 个空）",
            "   同一个字母可以在不同题里重复使用，但如果人数与陈述数相同则不要重复。",
            "",
        ])
        return lines

    def _build_paragraph_match(self, q: Question) -> List[str]:
        letters = q.banked_options or list("ABCDEFGHIJKLMNO")
        lines = [
            f"{q.number}. 【段落信息匹配】阅读带字母的段落，判断每条陈述出自哪一段",
            f"   可选段落字母: {' '.join(letters)}",
            "   【阅读材料】",
            (q.text or "")[:12000],
            "",
            "   【陈述】",
        ]
        for item in q.banked_blanks:
            lines.append(f"   {item['index'] + 1}. {item.get('statement', '')}")
        lines.extend([
            "",
            "   要求：每条陈述只填一个段落字母，不要写单词或句子；同一个段落可以被多条陈述重复选用。",
            f"   只返回 空号.字母，例如 1.{letters[0]} 2.{letters[-1]}（共 {len(q.banked_blanks)} 个空）",
            "",
        ])
        return lines

    def _build_sorting(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【排序题】{q.text}",
            "   请先在材料/转写中定位每个选项的原句，再按原句首次出现的先后排序。",
            "   不要按常识、重要性或选项当前顺序排列。",
        ]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        lines.append("   要求：只返回排序后的选项字母，格式如：B E A D C F")
        lines.append("")
        return lines

    def _build_video_choice(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【视频选择题】{q.text}",
            "   请根据前面提供的视频转写内容选择最佳答案。",
        ]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_listening_choice(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【听力选择题】{q.text}",
            "   请根据前面提供的音频转写内容选择最佳答案。",
        ]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_listening_fill_in(self, q: Question) -> List[str]:
        media_label = "视频填空题" if any(kw in (q.directions or "").lower() for kw in ['watch', 'video', 'clip', 'view']) else "听力填空题"
        lines = [
            f"{q.number}. 【{media_label}】",
            f"{q.text}",
            "",
            "【答题要求】",
            "1. 这是一个音频/视频理解题，请同时根据转写内容和每个空的左右上下文作答",
            "2. 答案优先照抄转写原文里的片段，取刚好能填进空的最短片段，保留原文的冠词、介词、数字和单位写法，"
            "不要同义改写或概括",
            "3. 每空只填写缺失部分，不要重复空格左边或右边已经出现的词",
            "4. 如果左边已有 be going to / I'm going to / we're 等结构，答案要能直接拼进原句并保持语法通顺",
            "5. 表格或列表题可参考行标题、列标题判断该空该填哪一类信息；同一个信息有几种说法时，"
            "选最具体、最贴合标题的那一种",
            "6. 答案形态要与空位一致：行标题本身已经表达的含义（如长度、宽度、时长）不要在答案里再写一遍；"
            "同一信息有形容词和名词两种写法时，选能直接嵌进整行的那一种",
            "7. 不要填整句，不要串用其他空的答案",
            ""
        ]

        for blank in q.banked_blanks:
            lines.append(f"   空{blank['index'] + 1}: {blank['sentence']}")
            if blank.get('label'):
                lines.append(f"      行标题: {blank['label']}")
            if blank.get('left_context') or blank.get('right_context'):
                lines.append(f"      左侧: {blank.get('left_context', '')}")
                lines.append(f"      右侧: {blank.get('right_context', '')}")

        lines.append("")
        return lines

    def _build_vocab_test(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【词汇题】{q.text}"]
        text_clean = re.sub(r"^\d+[.、)\]]\s*", "", q.text).strip()

        if bool(re.match(r"^[a-zA-Z\-]+$", text_clean)) and len(text_clean) <= 20:
            lines.append("   → 选择该英文单词的正确中文释义")
        elif bool(re.search(r"[\u4e00-\u9fff]", text_clean)):
            lines.append("   → 选择该中文释义对应的正确英文表达")
        elif "_" in text_clean or len(text_clean) > 50:
            lines.append("   → 根据句子语境选择最合适的单词")

        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_banked_cloze(self, q: Question) -> List[str]:
        is_phrase = q.is_phrase_mode
        lines = [
            f"{q.number}. 【选词填空】请从以下选项中选择最合适的{'短语' if is_phrase else '单词'}填入空白处：",
            f"   可选{'短语' if is_phrase else '单词'}: {', '.join(q.banked_options)}",
        ]
        # 整段题面（含词库）对选词填空很关键，模型需要看全文才能确定搭配
        material = ""
        for line in (q.text or "").splitlines():
            if line.startswith("【全文】"):
                material = line[len("【全文】"):].strip()
                break
        if material:
            lines.append(f"   全文: {material[:1800]}")
        lines.append("   注意：题干里的词库一般每个词只能用一次，且必须按语法改变词形。")
        if (q.directions or '').strip():
            lines.append(f"   Directions: {q.directions.strip()[:400]}")
        if is_expression_bank_question(q):
            lines.extend(EXPRESSION_BANK_HINTS)

        if is_phrase:
            lines.extend([
                "注意：这是短语填空！请填写完整短语（如 'in advance' 而不是 'advance'）。",
                "必要时需要改变短语的形式（如时态、单复数等）。",
            ])
        else:
            lines.extend([
                " 注意：必要时需要改变单词形式（如时态、单复数等）。",
            ])

        lines.append("")

        for i, blank in enumerate(q.banked_blanks, 1):
            context = blank['context'][:400] + "..." if len(blank['context']) > 400 else blank['context']
            context = re.sub(r'<[^>]+>', '', context)
            lines.append(f"   空{i}: {context}")

        lines.append("")
        lines.append("   要求：")

        if is_phrase:
            lines.append("1. 必须填写完整短语（不要只填部分）")
            lines.append("2. 按顺序给出答案，格式：1.in advance 2.make the most of ...")
        else:
            lines.append("1. 按顺序给出答案，格式：1.word1 2.word2 ...")

        lines.append("")
        return lines

    def _build_single_choice(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【单选】{q.text}"]
        for opt in q.options:
            lines.append(f" {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_multiple_choice(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【多选】{q.text}",
            "   （注意：本题有多个正确答案，请逐条严格核对原文细节——包括数字、身份、地点与限定词，不要漏选也不要多选有细微偏差的干扰项）",
        ]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_true_false(self, q: Question) -> List[str]:
        instruction = (
            "正确时回答 T，错误时用 F | 改正后的完整句子，并根据课程材料核对改正内容。"
            if q.needs_correction else "请根据材料/音频/视频内容判断正误，只回答 T（正确）或 F（错误）。"
        )
        lines = [
            f"{q.number}. 【判断】{q.text}",
            f"   {instruction}",
            "",
        ]
        return lines

    def _build_fill_in(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【填空】{q.text}"]
        if len(q.inputs) > 1:
            lines.append(f"   （共 {len(q.inputs)} 个空）")
        if q.strict_word_bank:
            lines.append(f"   可选词: {', '.join(q.banked_options)}")
            lines.append("   对照每个空的上下文选择词项，使用词库原有拼写与大小写。")
            return lines
        # 静态词库 chip 不会被 BankedClozeStrategy 命中，这类题会落到这里且
        # banked_options 为空；用 Directions 补上短语变形的约束。
        if is_expression_bank_question(q):
            lines.extend(EXPRESSION_BANK_HINTS)
        if q.banked_options:
            lines.append(f"   可选词: {', '.join(q.banked_options)}")
            lines.append("   （只能从可选词中选择；必要时改变词形：单复数/时态/词性/派生词）")
            lines.append("   （若空位位于句子开头，首字母必须大写，否则会扣分）")
        hints = [b for b in q.banked_blanks if b.get('hint')]
        contexts = [b for b in q.banked_blanks if b.get('context')]

        # 题干用「中文括注」提示词义（Exercise 1 的翻译式填空），按 Vocabulary 对照表逐空对齐。
        # 只提供对照（锚定用词），词形该不该变、怎么变由模型按句子语法判断。
        cjk_hints = blank_cjk_hints(q)
        vocab_hits = []
        if cjk_hints and self.vocab_store:
            for i, hint in enumerate(cjk_hints):
                english = self.vocab_store.lookup(hint)
                if english:
                    vocab_hits.append(f"     空{i + 1}: {hint} → {english}")
        if vocab_hits:
            lines.append("   中文括注对应 Vocabulary 里的原词（用词以它为准）:")
            lines.extend(vocab_hits)
            lines.append("   （词形按句子语法自行判断：该用 -ing / -ed / 复数 / 首字母大写时就改，词干不要换词）")
        elif cjk_hints:
            lines.append("   中文括注提示:")
            lines.extend(f"     空{i + 1}: {hint}" for i, hint in enumerate(cjk_hints))
            lines.append("   （答案是从本单元 Vocabulary 学过的英文表达，请用教材里的固定搭配）")

        if hints:
            lines.append("   已给出的字母（题干已印在输入框外面，答案请给完整单词，程序会自动去掉这部分）:")
            lines.append("   优先使用上下文里 Learning 页出现的搭配原词，不要换成同前缀的其它词。")
            for item in hints:
                line = f"     空{item['index'] + 1}: 已给出 {item['hint']}"
                candidates = item.get('candidates') or []
                if candidates:
                    line += f"；课文里以它开头的词: {', '.join(candidates)}"
                lines.append(line)
        if contexts:
            lines.append("   上下文（只给空位附近的一小段）:")
            for item in contexts:
                line = f"     空{item['index'] + 1}: {item['context'][:140]}"
                right = (item.get('right_context') or '').strip()
                if right:
                    line += f"  ___  {right[:80]}"
                lines.append(line)
        lines.append("")
        return lines

    def _build_text(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【简答题】{q.text}"]
        if len(q.inputs) > 1:
            lines.append(f"   （共 {len(q.inputs)} 小题）")
        if re.search(r'translat|翻译', q.directions or '', re.I) or '翻译成中文' in (q.text or ''):
            lines.append("   （翻译题：把原文完整翻译成中文，只输出译文，不要保留英文原文）")
        else:
            lines.append("   （请提供简洁准确的回答，如果不是翻译题，那么只用英文回答）")
        lines.append("")
        return lines

    def _build_my_voice_text(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【My voice文字作答】{q.text}",
            "   要求：只输出一段可直接填写到输入框的英文介绍。",
            "   限制：500字符以内，不要说明自己无法录音或上传。",
            "",
        ]
        return lines

    def build_speaking_script(self, q: Question, directions: str = "",
                              target_seconds: float = 90.0) -> str:
        """没有原声的录音题（主观题）用：让模型写一段可以直接朗读的英文口语稿。"""
        target_seconds = float(target_seconds or 90.0)
        words_low = max(60, int(target_seconds * 2.1))
        words_high = max(words_low + 20, int(target_seconds * 2.6))

        lines = [
            "【口语录音题】页面上只有一个计时录音按钮，没有参考答案，需要你自己说一段英文。",
            f"【题目要求】{(q.text or '').strip()[:900]}",
        ]
        if directions and directions.strip() and directions.strip() not in (q.text or ''):
            lines.append(f"【题目指示】{directions.strip()[:400]}")
        if self.vocab_store and len(self.vocab_store):
            lines.append("【本单元 Vocabulary（尽量自然地用上这些表达，不要硬塞）】")
            lines.extend(self.vocab_store.prompt_lines(24))
        lines.extend([
            "【输出要求】",
            "1. 只输出可以直接朗读的英文正文；不要标题、不要 markdown、不要括号说明、不要中文。",
            f"2. 长度约 {words_low}-{words_high} 词，句子短、口语化，适合朗读。",
            "3. 紧扣题目要求组织内容，并结合相关材料给出具体回答。",
        ])
        return "\n".join(lines)

    def _build_unknown(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【题】{q.text}"]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _format_instructions(self, type_counts: Dict[QuestionType, int]) -> List[str]:
        lines = ["-" * 50, "请按以下格式回答："]

        has_single = (
                QuestionType.SINGLE_CHOICE in type_counts or
                QuestionType.VOCABULARY_TEST in type_counts or
                QuestionType.LISTENING_CHOICE in type_counts or
                QuestionType.VIDEO_CHOICE in type_counts
        )
        has_multiple = QuestionType.MULTIPLE_CHOICE in type_counts

        if has_single and not has_multiple:
            lines.append("单选题: 直接返回选项字母，如：A 或 1.A")
            lines.append("注意：每道题只选一个答案！")

        elif has_multiple and not has_single:
            lines.append("多选题: 返回多个字母，如：AB 或 1.AB")
            lines.append("注意：每道题可能有一个或多个正确答案！")

        elif has_single and has_multiple:
            lines.append("混合题型：")
            lines.append("- 单选题: 返回单个字母，如：A")
            lines.append("- 多选题: 返回多个字母，如：AB")
            lines.append("请仔细判断每道题是单选还是多选！")
            lines.append("判断依据：题目明确标注'多选'或有多个正确选项时选多个，否则单选")

        if QuestionType.BANKED_CLOZE in type_counts or QuestionType.DROPDOWN_SELECT in type_counts:
            lines.append("选词/选择填空: 1.答案1 2.答案2 ...（答案可以是多词短语）")

        if QuestionType.FILL_IN in type_counts:
            lines.append("填空题: 每空一行，按空号编号：\n1. 答案1\n2. 答案2 ...（可多词，保留小数）")

        if QuestionType.LISTENING_FILL_IN in type_counts:
            lines.append("听力/视频填空题: 每空一行，按空号编号：\n1. 答案1\n2. 答案2 ...（照抄转写原文，保留小数）")

        if QuestionType.TRUE_FALSE in type_counts:
            lines.append("判断题: 按题号回答 T 或 F。若题目要求改错，在 F 后以竖线附上改正内容。")

        if QuestionType.TEXT in type_counts:
            lines.append("简答题: 1.答案内容...")

        if QuestionType.MY_VOICE_TEXT in type_counts:
            lines.append("My voice文字作答: 1.英文介绍内容（500字符以内）")

        if QuestionType.SORTING in type_counts:
            lines.append("排序题: 按题号返回排序后的字母，如：1.B E A D C F")

        if QuestionType.MATCHING in type_counts:
            lines.append("匹配题: 每空一个字母，格式：1.B 2.A 3.F（不要写单词）")

        if QuestionType.PARAGRAPH_MATCH in type_counts:
            lines.append("段落信息匹配: 1.H 2.N ...（只填段落字母，同一段可重复）")

        if QuestionType.LINK_MATCH in type_counts:
            lines.append("连线匹配: 提示序号配选项字母，格式：1.D 2.C 3.A 4.B（不要写选项原文）")

        lines.append("-" * 50)
        return lines

    def _build_dropdown_select(self, q: Question) -> List[str]:
        # course-v3 的下拉填空没有旧版的 banked_blanks（上下文按空位拆好），
        # 题面和空号都在 q.text 里，此时退回填空式提示词，否则模型看不到题目。
        if not q.banked_blanks:
            lines = [f"{q.number}. 【选择填空】{q.text}"]
            # Cloze 页编号是 1) 2) 而不是 (1) (2)，两种都要数得出来
            total = (q.answer_count
                     or len(re.findall(r'\(\d+\)|\b\d{1,2}\)', q.text))
                     or 1)
            lines.append(f"   （共 {total} 个空，按空号 1..{total} 顺序作答）")
            if q.banked_options:
                lines.append(f"   可选选项: {', '.join(q.banked_options)}")
                lines.append("   （只能从选项中选择；必要时按语法改变词形，句首记得大写）")
            lines.append("   要求：按顺序给出答案，格式：1.答案1 2.答案2 ...")
            lines.append("")
            return lines

        total_blanks = len(q.banked_blanks)
        per_blank_sets = [tuple(b.get('options') or ()) for b in q.banked_blanks]
        has_distinct_per_blank = any(per_blank_sets) and len(set(s for s in per_blank_sets if s)) > 1

        lines = [
            f"{q.number}. 【选择填空】请从每个空的可选词中选择最合适的词填入空白处"
            f"（共 {total_blanks} 个空，必须按空号 1..{total_blanks} 全部逐一作答，即使同一句话有多个空也要分别编号）：",
        ]
        if q.banked_options and not has_distinct_per_blank:
            lines.append(f"   可选选项: {', '.join(q.banked_options)}")
        lines.append("")
        # Cloze 是一整篇文章，只给空位附近一小段会让模型看不到上下文与编号。
        for line in (q.text or "").splitlines():
            if line.startswith("【全文】"):
                lines.append(f"   全文: {line[len('【全文】'):].strip()[:6000]}")
                break
        lines.append("")

        for i, blank in enumerate(q.banked_blanks, 1):
            context = blank['context'][:400] + "..." if len(blank['context']) > 400 else blank['context']
            context = re.sub(r'<[^>]+>', '', context)
            lines.append(f"   空{i}: {context}")
            if blank.get('options'):
                lines.append(f"      本空可选（只能从其中选一项原样输出）: {' / '.join(blank['options'])}")

        lines.append("")
        lines.append(f"要求：严格按空号 1..{total_blanks} 顺序输出全部 {total_blanks} 个答案，格式：1.do 2.make ...")
        lines.append("")
        return lines


def _strip_markdown_text(text: str) -> str:
    """去掉模型输出里的 Markdown 标记，避免 ** / # / - / ` 这些符号被原样写进作答框。"""
    if not text:
        return ''
    s = text.replace('\r\n', '\n').replace('\r', '\n')
    s = re.sub(r'```[A-Za-z0-9_-]*\n?', '', s).replace('```', '')
    s = re.sub(r'(?m)^\s{0,3}#{1,6}\s*', '', s)
    s = re.sub(r'(?m)^\s{0,3}>\s?', '', s)
    s = re.sub(r'(?m)^\s*[-*+]\s+', '', s)
    s = s.replace('**', '').replace('__', '')
    s = re.sub(r'(?<!\w)\*(?!\s)([^*\n]+)\*(?!\w)', r'\1', s)
    s = re.sub(r'(?<!\w)_([^_\n]+)_(?!\w)', r'\1', s)
    s = s.replace('`', '')
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


class AnswerExecutor:
    """答案执行器 - 执行答案填写"""

    ANSWER_LABEL_PATTERN = (
        r'(?:My voice文字作答|听力/视频填空题|听力选择题|视频选择题|多选题|单选题|选择题|判断题|'
        r'简答题|选词/选择填空|填空题|排序题|排序|参考答案|答案|选词填空|翻译|Answer)'
    )
    NUMBER_PREFIX_PATTERN = r'(?:空\s*)?\d+\s*[.、:：\)\]]|Blank\s*\d+\s*[.、:：\)\]]'
    _REFUSAL_ANSWER_RE = re.compile(
        r'没有看到(?:听力|音频|视频)?(?:转写|原文)|无法(?:实际|直接)?听到(?:音频|录音)|'
        r'请提供(?:听力|音频|视频)?(?:原文|转写)|无法准确回到材料|收到转写后|^(?:答案|空)\s*\d+$',
        re.I,
    )

    @classmethod
    def _is_refusal_answer(cls, text: str) -> bool:
        return bool(text and cls._REFUSAL_ANSWER_RE.search(text.strip()))

    def __init__(self, driver, tts: Optional["TTSEngine"] = None, stop_requested=None):
        self.driver = driver
        self.tts = tts
        self.stop_requested = stop_requested if stop_requested is not None else threading.Event()

    def execute(self, question: Question, answer: str) -> bool:
        executors = {
            QuestionType.SINGLE_CHOICE: self._fill_single_choice,
            QuestionType.LISTENING_CHOICE: self._fill_single_choice,
            QuestionType.VIDEO_CHOICE: self._fill_single_choice,
            QuestionType.VOCABULARY_TEST: self._fill_single_choice,
            QuestionType.MULTIPLE_CHOICE: self._fill_multiple_choice,
            QuestionType.SORTING: self._fill_sorting,
            QuestionType.BANKED_CLOZE: self._fill_banked_cloze,
            QuestionType.DROPDOWN_SELECT: self._fill_dropdown_select,
            QuestionType.FILL_IN: self._fill_fill_in,
            QuestionType.TEXT: self._fill_text,
            QuestionType.MY_VOICE_TEXT: self._fill_text,
            QuestionType.LISTENING_FILL_IN: self._fill_listening_fill_in,
            QuestionType.MATCHING: self._fill_matching,
            QuestionType.PARAGRAPH_MATCH: self._fill_paragraph_match,
            QuestionType.RECORD: self._answer_record,
            QuestionType.ORAL_ALOUD: self._answer_oral_aloud,
        }

        executor = executors.get(question.q_type, self._fill_unknown)
        return executor(question, answer)

    def _fill_single_choice(self, q: Question, answer: str) -> bool:
        valid_letters = [opt.letter or chr(65 + idx) for idx, opt in enumerate(q.options)]
        letters = self._parse_choice_letters(answer, valid_letters)
        if not letters:
            letters = self._letters_from_option_text(answer, q)
            if letters:
                print(f"\t答案不是字母，已按选项原文反查: {letters}")
        if len(letters) != 1:
            print(f"\t单选题需要一个有效选项，收到: {answer}")
            return False
        answer_letter = letters[0]

        print(f"\t寻找选项: {answer_letter}")
        print(f"\t可用选项: {[opt.letter for opt in q.options]}")

        for opt in q.options:
            if opt.letter.upper() == answer_letter.upper():
                print(f"\t点击选项 {opt.letter}: {opt.text[:30]}...")
                return WebDriverHelper.safe_click(self.driver, opt.element)

        try:
            idx = ord(answer_letter.upper()) - ord('A')
            if 0 <= idx < len(q.options):
                opt = q.options[idx]
                print(f"\t通过索引匹配选项 {opt.letter}: {opt.text[:30]}...")
                return WebDriverHelper.safe_click(self.driver, opt.element)
        except:
            pass

        return False

    def _toggle_choice_option_verified(self, opt: Option, should_select: bool) -> bool:
        """点击单/多选选项并验证 DOM 选中态确实发生变化；外层 div 不响应时自动回退点内层复选框/标签。"""
        elem = opt.element
        cur = WebDriverHelper.is_choice_option_selected(self.driver, elem)
        if cur == should_select:
            opt.is_selected = should_select
            return True

        before_snap = WebDriverHelper.option_visual_snapshot(self.driver, elem)
        # 无真实 JS DOM 的单测 mock 场景：沿用 safe_click
        if before_snap is None:
            if not WebDriverHelper.safe_click(self.driver, elem):
                return False
            opt.is_selected = should_select
            return True

        click_targets = [('outer', elem)]
        inner_selectors = [
            ('input', 'input[type="checkbox"], input[type="radio"]'),
            ('label', 'label, .ant-checkbox-wrapper, .ant-checkbox, .ant-radio-wrapper, .ant-radio'),
            ('caption', '.caption, span[class*="index"], .MultipleChoice--checkbox-opt-2F4xY, [class*="checkbox"], [class*="check"]'),
            ('content', '.content, .component-htmlview, p'),
        ]
        seen_elems = {elem}
        for tag, sel in inner_selectors:
            try:
                for child in elem.find_elements(By.CSS_SELECTOR, sel):
                    if child not in seen_elems:
                        seen_elems.add(child)
                        click_targets.append((tag, child))
                        break
            except Exception:
                continue

        for mode, target in click_targets:
            clicked = WebDriverHelper.safe_click(self.driver, target)
            if not clicked:
                continue
            time.sleep(0.12)
            now_sel = WebDriverHelper.is_choice_option_selected(self.driver, elem)
            if now_sel == should_select:
                opt.is_selected = should_select
                return True
            after_snap = WebDriverHelper.option_visual_snapshot(self.driver, elem)
            if after_snap and after_snap != before_snap:
                # 某些自定义样式组件不改标准 class，但背景/边框/图标已变化，视为已切换，避免双击反选
                print(f"\t[提示] 选项 {opt.letter} 点击({mode})后样式已变化（非标准选中 class），按已切换处理")
                opt.is_selected = should_select
                return True

        # 最后尝试 ActionChains 真实鼠标点击
        try:
            WebDriverHelper.scroll_into_view(self.driver, elem, pause=0.05)
            ActionChains(self.driver).move_to_element(elem).click().perform()
            time.sleep(0.15)
            now_sel = WebDriverHelper.is_choice_option_selected(self.driver, elem)
            after_snap = WebDriverHelper.option_visual_snapshot(self.driver, elem)
            if now_sel == should_select or (after_snap and after_snap != before_snap):
                opt.is_selected = should_select
                return True
        except Exception:
            pass

        try:
            cls_info = (elem.get_attribute('class') or '')[:80]
        except Exception:
            cls_info = ''
        print(f"\t选项 {opt.letter} 点击后未检测到状态变化（目标={'选中' if should_select else '取消'}，class='{cls_info}'）")
        return False

    def _fill_multiple_choice(self, q: Question, answer: str) -> bool:
        letters = self._parse_choice_letters(answer, [opt.letter for opt in q.options])
        if not letters:
            letters = self._letters_from_option_text(answer, q)
            if letters:
                print(f"\t答案不是字母，已按选项原文反查: {letters}")
        if not letters:
            print(f"\t多选题答案没有有效的选项组合: {answer}")
            return False
        WebDriverHelper.dismiss_onboarding_tips(self.driver)
        target_set = {letter.upper() for letter in letters}
        has_js_verify = False
        for opt in q.options:
            should_select = opt.letter.upper() in target_set
            if WebDriverHelper.option_visual_snapshot(self.driver, opt.element) is not None:
                has_js_verify = True
            if not self._toggle_choice_option_verified(opt, should_select):
                return False

        if has_js_verify:
            actual_selected = {
                opt.letter.upper() for opt in q.options
                if WebDriverHelper.is_choice_option_selected(self.driver, opt.element)
            }
            if actual_selected != target_set:
                print(f"\t多选题最终校验失败：页面实选 {sorted(actual_selected)}，目标 {sorted(target_set)}")
                return False
            print(f"\t多选题已选: {sorted(actual_selected)}")
        else:
            print(f"\t多选题已选择: {sorted(target_set)}")
        return True

    def _fill_sorting(self, q: Question, answer: str) -> bool:
        order = self._parse_sorting_order(answer, [opt.letter for opt in q.options])
        if not order:
            print(f"\t排序答案解析失败（需要 {len(q.options)} 项）: {(answer or '').strip()[:80]}")
            return False

        print(f"\t排序答案: {' '.join(order)}")
        if self._apply_sorting_by_react(q, order):
            return True

        if self._apply_sorting_by_drag(q, order):
            return True

        if self._apply_sorting_by_js(q, order):
            return True

        return False

    def _parse_sorting_order(self, answer: str, valid_letters: List[str]) -> List[str]:
        valid = [letter.upper() for letter in valid_letters if letter]
        valid_set = set(valid)
        if not valid or not answer:
            return []

        def extract_complete(seq: List[str]) -> List[str]:
            order = []
            for letter in seq:
                letter = letter.upper()
                if letter in valid_set and letter not in order:
                    order.append(letter)
            return order if len(order) == len(valid) else []

        def strip_header(text: str) -> str:
            cleaned = re.sub(
                r'^\s*(?:排序题|排序|顺序|参考答案|答案|Answer)\s*[：:]\s*',
                '',
                text.strip(),
                flags=re.I,
            )
            cleaned = re.sub(r'^\s*\d+\s*[.、:：)\]]\s*', '', cleaned)
            return cleaned.strip()

        normalized = _strip_markdown_text(answer)
        upper_all = normalized.upper()

        # 1. 逐项编号格式（如 1.C 2.B 3.E 4.A 5.D 6.F），仅当构成完整排列时采纳，
        #    避免把 "排序题: 1.C B E A D F" 中的题号 "1.C" 误当成完整答案而丢弃后续字母。
        numbered = re.findall(r'\d+\s*[.、:：)\]]\s*([A-Z])(?![A-Z])', upper_all)
        order = extract_complete(numbered)
        if order:
            return order

        # 2. 逐行匹配纯字母序列（允许带 "排序题: 1." 等前缀及常见分隔符）
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        for raw_line in reversed(lines):
            candidate_line = strip_header(raw_line).upper()
            if not candidate_line:
                continue
            if re.fullmatch(r'[A-Z](?:[\s,，、;；>→\-/|]+[A-Z])+', candidate_line):
                order = extract_complete(re.findall(r'[A-Z]', candidate_line))
                if order:
                    return order
            if re.fullmatch(r'[A-Z]+', candidate_line):
                order = extract_complete(list(candidate_line))
                if order:
                    return order

        # 3. 退化提取：去头部标签与题号后，提取独立字母或紧凑字母串
        stripped_upper = strip_header(normalized).upper()
        order = extract_complete(re.findall(r'(?<![A-Z])([A-Z])(?![A-Z])', stripped_upper))
        if order:
            return order

        for chunk in re.findall(r'[A-Z]+', stripped_upper):
            order = extract_complete(list(chunk))
            if order:
                return order

        return []

    def _get_sorting_items(self, q: Question) -> List[Dict[str, Any]]:
        items = []
        elems = q.element.find_elements(By.CSS_SELECTOR, '.sequence-reply-view-item-text')
        for idx, elem in enumerate(elems):
            text = elem.text.strip()
            letter = ""
            try:
                spans = elem.find_elements(By.TAG_NAME, 'span')
                if spans:
                    raw_letter = re.sub(r'[\s.、):：]+', '', spans[0].text).upper()
                    if re.fullmatch(r'[A-Z]', raw_letter):
                        letter = raw_letter
            except:
                pass
            if not letter:
                match = re.match(r'^([A-Z])\s*[.、):：]', text)
                if match:
                    letter = match.group(1).upper()
            if not letter and idx < len(q.options):
                letter = chr(65 + idx)
            if letter:
                items.append({'letter': letter, 'element': elem})
        return items

    def _current_sorting_order(self, q: Question) -> List[str]:
        return [item['letter'] for item in self._get_sorting_items(q)]

    def _apply_sorting_by_react(self, q: Question, order: List[str]) -> bool:
        js = """
        const root = arguments[0];
        const order = arguments[1];
        const wrapper = root.classList.contains('sortable-list-wrapper')
            ? root
            : root.querySelector('.sortable-list-wrapper');
        if (!wrapper) return false;
        const fiberKey = Object.keys(wrapper).find(
            k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')
        );
        if (!fiberKey) return false;

        let fiber = wrapper[fiberKey];
        let sortable = null;
        let component = null;
        for (let depth = 0; depth < 12 && fiber; depth++, fiber = fiber.return) {
            const props = fiber.memoizedProps;
            const state = fiber.memoizedState;
            if (!sortable && props && typeof props.onSortEnd === 'function') sortable = fiber;
            if (!component && state && Array.isArray(state.options)) component = fiber;
            if (sortable && component) break;
        }
        if (!sortable || !component) return false;

        const currentOptions = () => (
            (component.stateNode && component.stateNode.state && component.stateNode.state.options)
            || component.memoizedState.options
            || []
        );
        let options = currentOptions();
        if (!order.every(letter => options.some(opt => opt.value === letter))) return false;

        for (let i = 0; i < order.length; i++) {
            options = currentOptions();
            const from = options.findIndex(opt => opt.value === order[i]);
            if (from >= 0 && from !== i) sortable.memoizedProps.onSortEnd({oldIndex: from, newIndex: i});
        }
        return true;
        """
        try:
            if not self.driver.execute_script(js, q.element, order):
                return False
            time.sleep(0.4)
            return self._current_sorting_order(q) == order
        except Exception as e:
            print(f"\tReact 排序失败: {str(e)[:60]}")
            return False

    def _apply_sorting_by_drag(self, q: Question, order: List[str]) -> bool:
        try:
            for target_index, target_letter in enumerate(order):
                items = self._get_sorting_items(q)
                current_order = [item['letter'] for item in items]
                if current_order == order:
                    return True
                if target_index >= len(items) or current_order[target_index] == target_letter:
                    continue

                source = next((item['element'] for item in items if item['letter'] == target_letter), None)
                target = items[target_index]['element']
                if not source or not target:
                    return False

                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", source)
                time.sleep(0.2)
                ActionChains(self.driver).click_and_hold(source).pause(0.2).move_to_element(target).pause(0.2).release().perform()
                time.sleep(0.5)

            return self._current_sorting_order(q) == order
        except Exception as e:
            print(f"\t拖拽排序失败: {str(e)[:60]}")
            return False

    def _apply_sorting_by_js(self, q: Question, order: List[str]) -> bool:
        js = """
        const root = arguments[0];
        const order = arguments[1];
        const wrapper = root.classList.contains('sortable-list-wrapper')
            ? root
            : root.querySelector('.sortable-list-wrapper');
        if (!wrapper) return false;

        const children = Array.from(wrapper.children);
        const pairs = [];
        for (let i = 0; i < children.length; i++) {
            const node = children[i];
            if (!node.classList.contains('sequence-reply-view-item-text')) continue;
            const letterText = (node.querySelector('span')?.textContent || node.textContent || '').trim();
            const letter = (letterText.match(/[A-Z]/) || [''])[0];
            const numberNode = i > 0 && children[i - 1].classList.contains('sortable-list-question-no')
                ? children[i - 1]
                : null;
            if (letter) pairs.push({ letter, numberNode, itemNode: node });
        }

        const byLetter = new Map(pairs.map(pair => [pair.letter, pair]));
        if (!order.every(letter => byLetter.has(letter))) return false;

        order.forEach((letter, index) => {
            const pair = byLetter.get(letter);
            if (pair.numberNode) {
                const strong = pair.numberNode.querySelector('strong');
                if (strong) strong.textContent = String(index + 1);
                wrapper.appendChild(pair.numberNode);
            }
            wrapper.appendChild(pair.itemNode);
        });

        ['input', 'change', 'mouseup', 'drop', 'dragend', 'sortupdate'].forEach(type => {
            wrapper.dispatchEvent(new Event(type, { bubbles: true }));
        });

        const reactKey = Object.keys(wrapper).find(key => key.startsWith('__reactProps$'));
        if (reactKey) {
            const props = wrapper[reactKey];
            if (props && typeof props.onChange === 'function') props.onChange(order);
            if (props && typeof props.onSortEnd === 'function') props.onSortEnd({ oldIndex: 0, newIndex: 0 });
        }

        return Array.from(wrapper.querySelectorAll('.sequence-reply-view-item-text'))
            .map(node => ((node.querySelector('span')?.textContent || node.textContent || '').match(/[A-Z]/) || [''])[0])
            .join('') === order.join('');
        """
        try:
            result = self.driver.execute_script(js, q.element, order)
            time.sleep(0.5)
            return bool(result) and self._current_sorting_order(q) == order
        except Exception as e:
            print(f"\tJS排序失败: {str(e)[:60]}")
            return False

    def _fill_banked_cloze(self, q: Question, answer: str) -> bool:
        words = self._parse_banked_answer(answer, len(q.banked_blanks))
        is_phrase_mode = q.is_phrase_mode

        print(f"\t解析答案: {words}")
        print(f"\t填空数量: {len(q.banked_blanks)}")
        print(f"\t模式: {'短语' if is_phrase_mode else '单词'}")

        success_count = 0

        for i, (blank, word) in enumerate(zip(q.banked_blanks, words)):
            if blank['input'] and word:
                try:
                    clean_word = word.strip()
                    clean_word = self._strip_given_prefix(blank.get('hint', ''), clean_word)
                    matched = self._match_to_option(
                        clean_word, q.banked_options, is_phrase_mode,
                        allow_rewrite=not is_expression_bank_question(q))
                    if matched:
                        clean_word = matched
                        print(f"        匹配到选项: {clean_word}")

                    WebDriverHelper.scroll_into_view(self.driver, blank['input'], pause=0.05)

                    # 与 _fill_fill_in 走同一套写入：真实键盘输入 + 回读校验，
                    # 失败时用 React native setter 兜底。原来只 send_keys 再派发
                    # Event('input') 且不回读，输入没生效也会算成功并自动交卷。
                    if not self._fill_text_input_verified(blank['input'], clean_word):
                        print(f"        空{i + 1}: 写入后校验失败，跳过该空")
                        continue

                    print(f"        空{i + 1}: {clean_word}")
                    success_count += 1

                except Exception as e:
                    error_msg = str(e)
                    print(f"      填空 {i + 1} 失败:{error_msg[:50]} ")
                    logger.error(f"详细错误: {error_msg}", exc_info=True)

        return bool(q.banked_blanks) and success_count == len(q.banked_blanks)

    def _match_to_option(self, answer: str, options: List[str], is_phrase_mode: bool,
                         allow_rewrite: bool = True) -> Optional[str]:
        if not answer or not options:
            return None

        answer_lower = answer.lower().strip()

        for opt in options:
            if opt.lower() == answer_lower:
                # 短语变形题要保留模型给出的词形与大小写：词库里是小写的原形，
                # 直接返回 opt 会把 "At the heart of" 降回 "at the heart of"。
                return answer if not allow_rewrite else opt

        if is_phrase_mode:
            for opt in options:
                opt_lower = opt.lower()
                if '...' in opt_lower or '…' in opt_lower or '_' in opt_lower:
                    parts = [p.strip() for p in re.split(r'\.\.\.|…|_', opt_lower) if p.strip()]
                    for part in parts:
                        if len(part) >= 2 and (answer_lower.startswith(part[:3]) or part.startswith(answer_lower[:3])):
                            return answer
                    continue

                # 短语变形题里词库是原形、正确答案是变形形式；把部分匹配吸附回
                # 词库原文会直接把对的改错（equipped with -> be equipped with）。
                if answer_lower in opt_lower and len(answer_lower) >= 4:
                    if not allow_rewrite:
                        continue
                    return opt
                if opt_lower in answer_lower:
                    return answer
        else:
            for opt in options:
                opt_lower = opt.lower()
                if answer_lower.startswith(opt_lower[:3]):
                    if (answer_lower == opt_lower + 's' or
                            answer_lower == opt_lower + 'es' or
                            answer_lower == opt_lower + 'd' or
                            answer_lower == opt_lower + 'ed' or
                            answer_lower == opt_lower + 'ing' or
                            answer_lower == opt_lower[:-1] + 'ies' or
                            answer_lower == opt_lower[:-1] + 'ied' or
                            answer_lower == opt_lower[:-1] + 'ing' or
                            answer_lower == opt_lower + opt_lower[-1] + 'ed' or
                            answer_lower == opt_lower + opt_lower[-1] + 'ing'):
                        return answer

                if opt_lower.startswith(answer_lower[:3]):
                    if (opt_lower == answer_lower + 's' or
                            opt_lower == answer_lower + 'es' or
                            opt_lower == answer_lower + 'd' or
                            opt_lower == answer_lower + 'ed' or
                            opt_lower == answer_lower + 'ing'):
                        return opt

        return None

    def _fill_fill_in(self, q: Question, answer: str) -> bool:
        if self._is_refusal_answer(answer):
            print(f"\t检测到 AI 拒答/缺少材料提示，拒绝写入输入框")
            return False
        answers = self._parse_banked_answer(answer, len(q.inputs))
        if any(self._is_refusal_answer(a) for a in answers):
            print(f"\t检测到占位/拒答内容，拒绝写入输入框: {answers}")
            return False
        print(f"\t解析答案: {answers}")
        print(f"\t输入框数量: {len(q.inputs)}")

        success_count = 0
        for i, inp in enumerate(q.inputs):
            ans = answers[i] if i < len(answers) else ""
            if ans:
                # 题目把首字母/前缀印在输入框外面（un____），输入框里只该填剩下的部分
                hint = ""
                if i < len(q.banked_blanks):
                    hint = q.banked_blanks[i].get('hint') or ''
                stripped = self._strip_given_prefix(hint, ans)
                if stripped != ans:
                    print(f"\t空{i + 1}: 去掉已给出的「{hint}」-> {stripped}")
                    ans = stripped
                print(f"\t空{i + 1}: {ans}")
                if self._fill_text_input_verified(inp, ans):
                    success_count += 1
                else:
                    print(f"\t空{i + 1}: 写入后校验失败")
            else:
                print(f"\t空{i + 1}: (空)")

        if q.inputs and success_count < len(q.inputs):
            print(f"\t只填写 {success_count}/{len(q.inputs)} 个空，不提交")
        return bool(q.inputs) and success_count == len(q.inputs)

    @staticmethod
    def _strip_given_prefix(hint: str, answer: str) -> str:
        """答案里已经包含题干给出的前缀时，只把剩余部分写进输入框。

        例：题干 `un____`，模型答 `unhappy` → 输入框里写 `happy`；
            题干 `per____`，模型答 `personal` → 写 `sonal`。
        """
        text = (answer or "").strip()
        prefix = (hint or "").strip()
        if not text or not prefix:
            return text
        if len(text) > len(prefix) and text[:len(prefix)].lower() == prefix.lower():
            return text[len(prefix):].lstrip()
        return text

    @classmethod
    def _extract_answer_by_number(cls, answer: str, question_number: int) -> str:
        answer = cls._normalize_answer_labels(answer)
        label_prefix = rf'(?:{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?'
        number_prefix = cls._number_prefix_pattern(question_number)
        next_prefix = rf'(?<![$\w])(?:{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{cls.NUMBER_PREFIX_PATTERN})'
        pattern = rf'(?<![$\w]){label_prefix}{number_prefix}\s*(.+?)(?=\s*{next_prefix}\s*|$)'
        match = re.search(pattern, answer, re.DOTALL)
        if match:
            return cls._clean_extracted_answer(match.group(1))

        lines = [l.strip() for l in answer.split('\n') if l.strip()]
        for line in lines:
            clean = re.sub(
                rf'^(?:{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{cls.NUMBER_PREFIX_PATTERN})\s*',
                '',
                line,
                flags=re.I
            ).strip()
            if clean and not re.match(r'^\d', clean):
                if re.match(rf'^(?:{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?{number_prefix}', line, re.I):
                    return cls._clean_extracted_answer(clean)

        return ""

    @classmethod
    def _normalize_answer_labels(cls, answer: str) -> str:
        return re.sub(
            rf'(?<!^)(?<![$\w])\s+((?:{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{cls.NUMBER_PREFIX_PATTERN}))',
            r'\n\1',
            answer.strip(),
            flags=re.I
        )

    @classmethod
    def _clean_extracted_answer(cls, answer: str) -> str:
        if not answer:
            return ""
        answer = re.sub(rf'^{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*', '', answer.strip(), flags=re.I)
        answer = re.sub(rf'^(?:{cls.NUMBER_PREFIX_PATTERN})\s*', '', answer.strip(), flags=re.I)
        answer = re.sub(rf'\s*{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*$', '', answer.strip(), flags=re.I)
        return answer.strip()

    @staticmethod
    def _number_prefix_pattern(number: int) -> str:
        return rf'(?:(?:空\s*)?{number}\s*[.、:：\)\]]|Blank\s*{number}\s*[.、:：\)\]])'

    # ===== 跟读/朗读录音题 =====
    # 录音器（chivox）在开始拉流后的前 ~0.6 秒会丢块并补静音，参考音频前面垫
    # 这么多静音才能让整句落进有效窗口（实测 0.8s：逐词 100/100/100，总分 95）。
    RECORD_LEAD_PAD = 0.8
    RECORD_RETRY_PAD = 1.3
    RECORD_MIN_SCORE = 60
    RECORD_MAX_SECONDS = 105.0  # 限时自由作答（页面写的 02:00）留一点余量
    RECORD_AUDIO_CACHE: Dict[str, bytes] = {}
    RECORD_AUDIO_LOCK = threading.Lock()

    def _prefetch_record_audio(self, items: List[Dict], texts: List[str], idx: int) -> Optional[bytes]:
        item = items[idx]
        url = (item.get('url') or '').strip()
        if url:
            return self._download_record_audio(url)
        text = texts[idx] if texts and idx < len(texts) else ''
        return self._speech_audio(text, idx)

    def _answer_record(self, q: Question, answer: str = "") -> bool:
        """完成录音题：有原声用原声，没原声用 TTS 朗读页面原文（多条时后台并发预取音频）。"""
        items = list(q.record_items or [])
        if not items:
            print("\t没有找到录音条目")
            return False
        if items[0].get('role_play'):
            return self._answer_role_play(q)
        if items[0].get('vocab'):
            return self._answer_vocabulary_read_aloud(q)

        self._dismiss_record_popup()
        probe = self.driver.execute_async_script(RECORD_MIC_JS, "", self.RECORD_LEAD_PAD, 0)
        if not probe or not probe.get("ok"):
            print(f"\t录音注入初始化失败: {probe}")
            return False

        texts = self._record_speech_texts(q, answer)
        if not any((item.get('url') or '').strip() for item in items):
            print("\t录音题没有原声，用 TTS 朗读页面原文")

        # 多条录音（如 6 句视频配音 / 6 词跟读）：后台并发预合成 TTS 或下载原声，
        # 录第 1 条期间后续音频即可全部就绪，省去逐条串行等待网络握手的时间。
        prefetched: Dict[int, Any] = {}
        pool = ThreadPoolExecutor(max_workers=min(3, len(items))) if len(items) > 1 else None
        try:
            if pool is not None:
                for idx in range(len(items)):
                    prefetched[idx] = pool.submit(self._prefetch_record_audio, items, texts, idx)

            for idx in range(len(items)):
                if self.stop_requested.is_set():
                    break
                try:
                    data = prefetched[idx].result() if idx in prefetched else None
                    self._record_one_item(q, idx, texts, prefetched_audio=data)
                except Exception as exc:
                    print(f"\t第 {idx + 1} 条录音失败: {str(exc)[:80]}")
                    logger.error("录音题执行失败", exc_info=True)
        finally:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)

        # 收尾：平台提交时会检查「有没有作答」，漏掉的条目补录一次
        time.sleep(0.35)
        missing = [i for i in range(len(items)) if not self._row_done(q, i)]
        if missing and not self.stop_requested.is_set():
            print(f"\t有 {len(missing)} 条没有录制结果，补录: {[i + 1 for i in missing]}")
            for idx in missing:
                if self.stop_requested.is_set():
                    break
                try:
                    self._record_one_item(q, idx, texts)
                except Exception:
                    logger.error("录音题补录失败", exc_info=True)
            time.sleep(0.35)

        still_missing = [i for i in range(len(items)) if not self._row_done(q, i)]
        done = len(items) - len(still_missing)
        if still_missing:
            print(f"\t仍有未完成条目: {[i + 1 for i in still_missing]}")
        print(f"\t录音题完成 {done}/{len(items)} 条")
        return done == len(items)

    ORAL_ALOUD_COUNTDOWN = 130
    ORAL_ALOUD_TIMEOUT = 420

    def _answer_oral_aloud(self, q: Question, answer: str = "") -> bool:
        """段落朗读题：准备阶段就注入 TTS 音频，强制录音一次录完。"""
        if self.stop_requested.is_set():
            return False
        item = (q.record_items or [{}])[0]
        text = (item.get('text') or '').strip() or self._record_reading_text(q)
        if not text:
            print("\t朗读题没有读到原文，跳过")
            return False
        if not self._tts().enabled:
            print("\t朗读题需要 TTS，TTS 已关闭，跳过")
            return False
        state = self.driver.execute_script(ORAL_ALOUD_STATE_JS) or {}
        if (state.get('score') or '').strip():
            print(f"\t朗读题已有得分 {state.get('score')}，跳过")
            return True
        data = self._speech_audio(text, self.RECORD_MAX_SECONDS)
        if not data:
            return False
        payload = base64.b64encode(data).decode('ascii')
        probe = self.driver.execute_async_script(
            RECORD_MIC_JS, payload, self.RECORD_LEAD_PAD, self.RECORD_MAX_SECONDS)
        if not probe or not probe.get('ok'):
            print(f"\t朗读音频注入失败: {probe}")
            return False
        self._oral_stop_after = float(probe.get('dur') or 0) + 1.0
        print("\t朗读题：开始强制流程（约 4 分钟）")
        if not self._oral_aloud_start(q):
            return False
        score = self._wait_oral_aloud(q)
        if score is None:
            print("\t朗读题等待超时，没有拿到分数")
            return False
        print(f"\t朗读题得分 {score}")
        return True

    def _oral_aloud_start(self, q: Question) -> bool:
        """点 start 进入强制流程；已经在倒计时或录音中就直接用。"""
        state = self.driver.execute_script(ORAL_ALOUD_STATE_JS) or {}
        label = (state.get('btn') or '').strip()
        if state.get('recording') or (label and label != 'start'):
            return True
        try:
            button = q.element.find_element(By.CSS_SELECTOR, '.p-oral-aloud-button .button-start')
        except Exception:
            try:
                button = self.driver.find_element(
                    By.CSS_SELECTOR, '.p-oral-aloud-button .button-start')
            except Exception:
                print("\t找不到朗读题的 start 按钮")
                return False
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'})", button)
            time.sleep(0.3)
            button.click()
        except Exception as exc:
            print(f"\t点击 start 失败: {str(exc)[:60]}")
            return False
        return True

    def _wait_oral_aloud(self, q: Question) -> Optional[float]:
        """等强制录音结束出分：倒计时 → 强制录音 → 主动停止 → 出分。"""
        start = time.time()
        active_start = None
        stopped = False
        while time.time() - start < self.ORAL_ALOUD_TIMEOUT:
            if self.stop_requested.is_set():
                return None
            try:
                state = self.driver.execute_script(ORAL_ALOUD_STATE_JS) or {}
            except Exception:
                state = {}
            raw = (state.get('score') or '').strip()
            if raw:
                try:
                    return float(raw)
                except ValueError:
                    return 0.0
            label = (state.get('timer') or state.get('btn') or '').strip()
            match = re.search(r'(\d{1,2}):(\d{2})', label)
            active = bool(match and (int(match.group(1)) > 0 or int(match.group(2)) > 0))
            if active:
                if active_start is None:
                    active_start = time.time()
                stop_after = getattr(self, '_oral_stop_after', 0.0)
                if not stopped and stop_after and time.time() - active_start >= stop_after:
                    stopped = True
                    try:
                        btn = q.element.find_element(By.CSS_SELECTOR, '.record-icon.button-record')
                        self.driver.execute_script('arguments[0].click()', btn)
                        print("\t朗读录音结束，点击停止")
                    except Exception:
                        try:
                            btn = self.driver.find_element(By.CSS_SELECTOR, '.record-icon.button-record')
                            self.driver.execute_script('arguments[0].click()', btn)
                            print("\t朗读录音结束，点击停止")
                        except Exception:
                            pass
            time.sleep(2)
        return None

    def _answer_vocabulary_read_aloud(self, q: Question) -> bool:
        """词汇跟读页：逐张卡片用页面原声冒充麦克风，不调用 AI。

        swiper 只渲染 active slide，所以每轮重新抓一次卡片信息，录完调
        slideNext()，等待下一张真实内容挂载；整组上传完成后才返回成功。
        """
        if self.stop_requested.is_set():
            return False
        first = (q.record_items or [{}])[0]
        total = int(first.get('vocab_total') or 0) or 0
        if total <= 0:
            print("\t未读到词汇卡片总数，停止以免遗漏卡片")
            return False
        print(f"\t词汇跟读：共 {total} 张卡片，使用页面原声逐张跟读（不调用 AI）")

        self._dismiss_record_popup()
        probe = self.driver.execute_async_script(
            RECORD_MIC_JS, "", self.RECORD_LEAD_PAD, 0)
        if not probe or not probe.get("ok"):
            print(f"\t录音注入初始化失败: {probe}")
            return False

        # 快速处理可能从中间卡片开始；先回第一张，确保前面的卡片也会处理。
        try:
            current = self.driver.execute_script(VOCAB_CARD_JS, q.element) or {}
            if int(current.get('index') or 0) != 1:
                if not self.driver.execute_script(VOCAB_NEXT_JS, q.element, True):
                    print("\t无法返回第一张词汇卡片，停止")
                    return False
        except Exception:
            return False

        done = 0
        for index in range(1, total + 1):
            info = self._wait_vocab_card(q, index)
            if not info:
                print(f"\t第 {index} 张卡片尚未就绪，停止")
                break
            word = (info.get('word') or '').strip()
            ok = self._record_vocab_card(q, info)
            print(f"\t词汇跟读 [{index}/{total}] {word} -> {'OK' if ok else '失败'}")
            if not ok or self.stop_requested.is_set():
                break
            done += 1
            if index == total:
                break

            try:
                moved = self.driver.execute_script(VOCAB_NEXT_JS, q.element)
            except Exception:
                moved = ''
            if not moved:
                print("\t找不到下一张卡片的切换方式，停止")
                break

        print(f"\t词汇跟读完成 {done}/{total} 张")
        return done == total and not self.stop_requested.is_set()

    def _wait_vocab_card(self, q: Question, index: int) -> Optional[Dict]:
        """进度和卡片内容都就绪后才开始录音，避免对懒加载占位作答。"""
        for _ in range(24):
            if self.stop_requested.is_set():
                return None
            try:
                info = self.driver.execute_script(VOCAB_CARD_JS, q.element) or {}
                if (int(info.get('index') or 0) == index
                        and (info.get('word') or '').strip() and info.get('hasButton')
                        and self._vocab_record_buttons(q)):
                    return info
            except Exception:
                pass
            if self.stop_requested.wait(0.25):
                return None
        return None

    def _vocab_record_buttons(self, q: Question) -> List[Any]:
        try:
            slide = q.element.find_element(By.CSS_SELECTOR, '.swiper-slide-active')
            return find_record_buttons(self.driver, slide)
        except Exception:
            return []

    def _wait_vocab_upload(self, q: Question, baseline: Dict,
                           timeout: float = 30.0) -> bool:
        """只接受当前卡片本次上传的新地址，原声、blob 和历史回放均不算完成。"""
        previous = set(baseline.get('recordings') or [])
        for _ in range(max(1, int(timeout * 4))):
            if self.stop_requested.is_set():
                return False
            try:
                info = self.driver.execute_script(VOCAB_CARD_JS, q.element) or {}
                if info and (info.get('index') != baseline.get('index')
                             or info.get('word') != baseline.get('word')):
                    return False
                uploaded = any(src.startswith(('http://', 'https://')) and src not in previous
                               for src in info.get('recordings') or [])
                if uploaded and not info.get('recording'):
                    return True
            except Exception:
                pass
            if self.stop_requested.wait(0.25):
                return False
        return False

    def _record_vocab_card(self, q: Question, info: Dict) -> bool:
        """单张卡片：下载页面原声（无原声时 TTS 读该词）→ 注入 → 点录音/停止。

        录制前保存历史回放地址，停止后等本次上传产生新地址再翻卡。
        """
        if self.stop_requested.is_set():
            return False
        word = (info.get('word') or '').strip()
        raw = info.get('audio') or ''
        url = raw.split('#')[0].strip()
        duration = float(info.get('duration') or 0) or 1.2
        data = (self._download_record_audio(url) if url
                else self._speech_audio(word, 0))
        if not data:
            print(f"\t{word}: {'原声下载失败' if url else 'TTS 生成失败'}")
            return False

        payload = base64.b64encode(data).decode()
        state = self.driver.execute_async_script(
            RECORD_MIC_JS, payload, self.RECORD_LEAD_PAD, 0)
        if not state or not state.get('ok'):
            print(f"\t{word}: 音频注入失败 {state}")
            return False
        clip = float(state.get('dur') or 0) or (duration + self.RECORD_LEAD_PAD)

        try:
            baseline = self.driver.execute_script(VOCAB_CARD_JS, q.element) or {}
        except Exception:
            return False
        if baseline.get('index') != info.get('index') or baseline.get('word') != word:
            return False
        buttons = self._vocab_record_buttons(q)
        if not buttons:
            print(f"\t{word}: 没找到录音按钮")
            return False
        button = buttons[0]
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", button)
        except Exception:
            pass
        if self.stop_requested.wait(0.2):
            return False
        if not WebDriverHelper.safe_click(self.driver, button):
            print(f"\t{word}: 点击录音失败")
            return False
        interrupted = self.stop_requested.wait(clip + 0.8)
        # React 可能替换按钮；停止时重新定位。取消任务也须结束正在进行的录音。
        buttons = self._vocab_record_buttons(q)
        if not buttons or not WebDriverHelper.safe_click(self.driver, buttons[0]):
            print(f"\t{word}: 点击停止失败")
            return False
        if interrupted or self.stop_requested.is_set():
            return False
        if not self._wait_vocab_upload(q, baseline):
            print(f"\t{word}: 未确认本次录音上传完成")
            return False
        return True

    def _record_row(self, item: Dict):
        row_index = (item or {}).get('row_index')
        if not isinstance(row_index, int) or row_index < 0:
            return None
        try:
            rows = self.driver.find_elements(By.CSS_SELECTOR, '.oral-study-sentence')
            if not rows:
                rows = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    '.question-video-dub-pc .question-common-abs-reply > .item')
        except Exception:
            return None
        if row_index >= len(rows):
            return None
        return rows[row_index]

    def _record_button(self, q: Question, idx: int = 0):
        items = q.record_items or []
        buttons = find_record_buttons(self.driver, q.element)
        if len(buttons) == len(items) and idx < len(buttons):
            return buttons[idx]
        row = self._record_row(items[idx] if idx < len(items) else {})
        if row is not None:
            row_buttons = find_record_buttons(self.driver, row)
            if row_buttons:
                return row_buttons[0]
        return buttons[idx] if idx < len(buttons) else None

    def _row_state(self, q: Question, idx: int) -> Optional[dict]:
        try:
            return self.driver.execute_script(
                RECORD_ROW_STATE_JS, q.element, idx, list(record_button_selectors()))
        except Exception:
            # 页面重渲染会让旧 element 失效，换到这一行再读一次
            items = q.record_items or []
            row = self._record_row(items[idx]) if idx < len(items) else None
            if row is None:
                return None
            try:
                return self.driver.execute_script(
                    RECORD_ROW_STATE_JS, row, 0, list(record_button_selectors()))
            except Exception:
                return None

    @staticmethod
    def _row_recorded(state: dict, baseline: Optional[dict] = None) -> bool:
        """这一条是否已经有结果。

        跟读题看分数（`.score_layout`）；自由作答虽然没有参考答案，但平台上传音频后
        同样会给一个分数，同时页面里会出现可回放的 <audio src="http://...mp3">。
        """
        if not state:
            return False
        if state.get('score') is not None:
            return True
        if state.get('recorded'):
            return True
        if baseline:
            return (state.get('blob') or 0) > (baseline.get('blob') or 0) or \
                   (state.get('uploaded') or 0) > (baseline.get('uploaded') or 0)
        return False

    @staticmethod
    def _has_new_record_result(state: Optional[dict], baseline: Optional[dict] = None) -> bool:
        """判断当前行是否已经产生了新的录制结果（且未处于录音中）。"""
        if not state or state.get('stop'):
            return False
        base = baseline or {}
        score = state.get('score')
        base_score = base.get('score')
        if score is not None and (base_score is None or score != base_score):
            return True
        if (state.get('blob') or 0) > (base.get('blob') or 0):
            return True
        if (state.get('uploaded') or 0) > (base.get('uploaded') or 0):
            return True
        if state.get('recorded') and not base.get('recorded'):
            return True
        return False

    def _row_done(self, q: Question, idx: int) -> bool:
        return self._row_recorded(self._row_state(q, idx) or {})

    def _record_reading_text(self, q: Question) -> str:
        """页面上写好的朗读原文：优先各条目的句子，其次题干里的英文行。"""
        parts = [(item.get('text') or '').strip() for item in (q.record_items or [])]
        joined = ' '.join(part for part in parts if part).strip()
        if joined:
            return joined[:600]
        lines = [(line or '').strip() for line in (q.text or '').splitlines()]
        english = [line for line in lines
                   if re.search(r'[A-Za-z]{3}', line)
                   and not CJK_CHAR_PATTERN.search(line)]
        return ' '.join(english)[:600]

    def _record_speech_texts(self, q: Question, answer: str) -> List[str]:
        """每条要朗读的文本：条目自己的原文优先，其次模型稿件，最后题干英文。"""
        count = len(q.record_items or [])
        if count <= 0:
            return []
        text = (answer or '').strip() or self._record_reading_text(q)
        texts = self._split_speech(text, count) if text and count > 1 else [text] * count
        return [(item.get('text') or '').strip() or texts[idx]
                for idx, item in enumerate(q.record_items)]

    @staticmethod
    def _split_speech(text: str, count: int) -> List[str]:
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text or '') if s.strip()]
        if len(sentences) <= 1:
            return [text] + [''] * (count - 1)
        size = max(1, -(-len(sentences) // count))
        chunks = [' '.join(sentences[i:i + size]) for i in range(0, len(sentences), size)]
        chunks = [chunk for chunk in chunks if chunk]
        while len(chunks) < count:
            chunks.append('')
        return chunks[:count]

    def _tts(self) -> TTSEngine:
        if getattr(self, 'tts', None) is None:
            self.tts = TTSEngine()
        return self.tts

    def _speech_audio(self, text: str, idx: int) -> Optional[bytes]:
        text = (text or '').strip()
        if not text:
            print(f"\t第 {idx + 1} 条: 没有可朗读的稿件")
            return None
        engine = self._tts()
        fmt, data = engine.synthesize(text, self.RECORD_MAX_SECONDS)
        if not data:
            print(f"\t第 {idx + 1} 条: TTS 合成失败（{engine.last_error or '未知原因'}）")
            return None
        print(f"\t第 {idx + 1} 条: TTS 生成 {fmt} 音频 {len(data)} 字节")
        return data

    def _record_one_item(self, q: Question, idx: int, texts: Optional[List[str]] = None,
                         prefetched_audio: Optional[bytes] = None) -> bool:
        items = q.record_items or []
        if idx >= len(items):
            return False
        item = items[idx]
        url = (item.get('url') or '').strip()
        max_dur = 0.0
        if url:
            data = prefetched_audio if prefetched_audio is not None else self._download_record_audio(url)
            if not data:
                print(f"\t第 {idx + 1} 条: 参考音频下载失败")
                return False
        else:
            text = texts[idx] if texts and idx < len(texts) else ''
            data = prefetched_audio if prefetched_audio is not None else self._speech_audio(text, idx)
            if not data:
                return False
            max_dur = self.RECORD_MAX_SECONDS

        payload = base64.b64encode(data).decode()
        baseline = self._row_state(q, idx) or {}
        last_state = None
        # 有原声的跟读题失败时可以换更大的静音垫重试；主观题一段要录几十秒，
        # 重录代价太大，只录一次
        pads = (self.RECORD_LEAD_PAD, self.RECORD_RETRY_PAD) if url else (self.RECORD_LEAD_PAD,)
        for pad in pads:
            state = self.driver.execute_async_script(RECORD_MIC_JS, payload, pad, max_dur)
            if not state or not state.get("ok"):
                print(f"\t第 {idx + 1} 条: 音频注入失败 {state}")
                return False
            clip = float(state.get("dur") or 0) or (float(item.get('duration') or 0) + pad)

            button = self._record_button(q, idx)
            if button is None:
                print(f"\t第 {idx + 1} 条: 录音按钮已消失")
                return False
            if not WebDriverHelper.safe_click(self.driver, button):
                print(f"\t第 {idx + 1} 条: 点击录音失败")
                return False

            # 轮询等待本条录音结束：视频配音等按台词时长自动停录的题型若已提前自动停止，
            # 绝不能再盲目点一次按钮，否则会误触发第二次录音并在切到下一条时冲掉本条成绩。
            saw_btn_change = False
            saw_mic_live = False
            auto_stopped = False
            base_sig = baseline.get('btnSig') or ''
            base_calls = baseline.get('micCalls') or 0
            rec_start = time.time()
            rec_deadline = rec_start + clip + 1.0
            while time.time() < rec_deadline:
                if self.stop_requested.is_set():
                    break
                cur = self._row_state(q, idx) or {}
                if base_sig and cur.get('btnSig') and cur.get('btnSig') != base_sig:
                    saw_btn_change = True
                if (cur.get('micCalls') or 0) > base_calls and cur.get('micLive'):
                    saw_mic_live = True
                if time.time() - rec_start >= 1.2:
                    if self._has_new_record_result(cur, baseline):
                        auto_stopped = True
                        break
                    if saw_btn_change and cur.get('btnSig') == base_sig and not cur.get('stop'):
                        auto_stopped = True
                        break
                    if saw_mic_live and not cur.get('micLive') and not cur.get('stop'):
                        auto_stopped = True
                        break
                time.sleep(min(0.35, max(0.05, rec_deadline - time.time())))

            post_state = self._row_state(q, idx) or {}
            if not auto_stopped:
                if (self._has_new_record_result(post_state, baseline)
                        or (saw_btn_change and post_state.get('btnSig') == base_sig
                            and not post_state.get('stop'))
                        or (saw_mic_live and not post_state.get('micLive')
                            and not post_state.get('stop'))):
                    auto_stopped = True

            if auto_stopped:
                print(f"\t第 {idx + 1} 条: 录音已自动结束，跳过点击停止")
            else:
                stop_button = self._record_button(q, idx)
                if stop_button is None or not WebDriverHelper.safe_click(
                        self.driver, stop_button):
                    print(f"\t第 {idx + 1} 条: 点击停止失败")
                if self.stop_requested.is_set():
                    return False

            # 停止后平台要上传音频再评分，长录音要多等一会儿
            timeout = max(30.0, min(120.0, clip * 1.2))
            last_state = self._wait_record_finish(q, idx, timeout=timeout, baseline=baseline)
            score = (last_state or {}).get('score')
            if score is None:
                if last_state and self._row_recorded(last_state, baseline):
                    print(f"\t第 {idx + 1} 条: 已录制（{clip:.1f}s，未返回分数）")
                    time.sleep(0.4)
                    return True
                print(f"\t第 {idx + 1} 条: 未拿到录制结果（可能录音未被识别）")
            else:
                print(f"\t第 {idx + 1} 条: 得分 {score}")
                if int(score) >= self.RECORD_MIN_SCORE:
                    time.sleep(0.4)
                    return True
            time.sleep(0.6)
            if last_state is None:
                break
        return bool(last_state) and self._row_recorded(last_state, baseline)

    def _wait_record_finish(self, q: Question, idx: int, timeout: float = 25.0,
                            baseline: Optional[dict] = None) -> Optional[dict]:
        """等这一条录音出结果；若发现还停在录音状态，补一次停止点击。"""
        deadline = time.time() + timeout
        extra_stops = 0
        while time.time() < deadline:
            if self.stop_requested.is_set():
                return None
            # 重渲染后旧元素会失效，按行号重新定位。
            state = self._row_state(q, idx)
            if state and not state.get('stop') and (
                    state.get('score') is not None
                    or self._row_recorded(state, baseline)):
                return state
            if state and state.get('stop') and extra_stops < 2:
                # 还在录：说明停止那一下没点中，补点一次
                extra_stops += 1
                try:
                    button = self._record_button(q, idx)
                    if button is not None:
                        WebDriverHelper.safe_click(self.driver, button)
                        print(f"\t第 {idx + 1} 条: 检测到仍在录音，补一次停止点击")
                except Exception:
                    pass
                time.sleep(1.5)
                continue
            time.sleep(0.5)
        return None

    def _answer_role_play(self, q: Question) -> bool:
        self._dismiss_record_popup()
        roles = [e for e in q.element.find_elements(By.CSS_SELECTOR, '.role-list label.label')
                 if e.is_displayed()]
        if roles and not WebDriverHelper.safe_click(self.driver, roles[0]):
            return False
        try:
            WebDriverWait(self.driver, 10).until(
                lambda d: any(e.is_displayed() for e in q.element.find_elements(
                    By.CSS_SELECTOR, '.role-reply .record-seat')))
            info = self.driver.execute_script("""
                const row = arguments[0].querySelector('.role-reply .list-item-review');
                if (!row) return null;
                let fiber = row[Object.keys(row).find(k => k.startsWith('__reactFiber'))];
                while (fiber && !(fiber.memoizedProps || {}).question) fiber = fiber.return;
                const question = fiber && fiber.memoizedProps.question;
                const selected = arguments[0].querySelector('.role-reply .score:not(.hide)');
                const role = selected && selected.closest('.list-item-review').querySelector('.role');
                if (!question || !role) return null;
                const children = question.children || [];
                return {lines: children.filter(x => x.role && x.role.name === role.textContent.trim())
                    .map(x => ({url: (x.contents || []).find(c => c.type === 1 && c.path)?.path || ''})),
                    seconds: children.reduce((s, x) => s + ((x.config || {}).recordTime || 0) / 1000, 0)};
            """, q.element)
            if not info or not info.get('lines'):
                print('\t无法读取角色台词')
                return False
            clips = []
            for line in info['lines']:
                data = self._download_record_audio((line.get('url') or '').split('#')[0])
                if not data:
                    print('\t角色台词缺少参考音频')
                    return False
                clips.append(base64.b64encode(data).decode())
            prepared = self.driver.execute_async_script("""
                const done = arguments[arguments.length - 1], api = window.__ucRec;
                if (!api) { done(false); return; }
                (async () => {
                    const queue = [];
                    for (const b64 of arguments[0]) {
                        const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
                        queue.push(await api.ctxOf().decodeAudioData(bytes.buffer));
                    }
                    api.rolePlayStream = api.stream;
                    api.rolePlayQueue = queue;
                    api.stream = function () {
                        api.buf = api.rolePlayQueue.shift() || null;
                        return api.rolePlayStream();
                    };
                    done(true);
                })().catch(() => done(false));
            """, clips)
            if not prepared:
                print('\t角色台词音频初始化失败')
                return False
            started = False
            try:
                seat = q.element.find_element(By.CSS_SELECTOR, '.role-reply .record-seat')
                if not WebDriverHelper.safe_click(self.driver, seat):
                    return False
                started = True
                deadline = time.time() + max(90, min(600, info.get('seconds', 0) * 2 + 60))
                while time.time() < deadline and not self.stop_requested.is_set():
                    state = self.driver.execute_script("""
                        const root = arguments[0], api = window.__ucRec;
                        const rows = [...root.querySelectorAll('.role-play-quiz .list-item-review')];
                        const scores = [...root.querySelectorAll('.role-play-quiz .score:not(.hide)')];
                        const audio = root.querySelector('.role-reply audio');
                        return {left: api.rolePlayQueue.length,
                            scores: scores.map(x => x.textContent.trim()),
                            finished: rows.length && rows[rows.length - 1].classList.contains('active') &&
                                (!audio || audio.paused || audio.ended)};
                    """, q.element)
                    if (state['left'] == 0 and len(state['scores']) == len(clips)
                            and all(state['scores']) and state['finished']):
                        print(f"\t角色扮演录音完成，得分: {', '.join(state['scores'])}")
                        started = False
                        return True
                    self.stop_requested.wait(0.5)
                print('\t角色扮演录音未完成')
                return False
            finally:
                if started:
                    buttons = q.element.find_elements(By.CSS_SELECTOR, '.role-reply .pre-page-btn')
                    if buttons:
                        WebDriverHelper.safe_click(self.driver, buttons[0])
                self.driver.execute_script("""
                    const api = window.__ucRec;
                    if (api && api.rolePlayStream) {
                        api.stream = api.rolePlayStream;
                        delete api.rolePlayStream;
                        delete api.rolePlayQueue;
                        api.buf = null;
                    }
                """)
        except Exception as exc:
            print(f'\t角色扮演处理失败: {str(exc)[:100]}')
            return False

    def _download_record_audio(self, url: str) -> Optional[bytes]:
        if not url:
            return None
        with self.RECORD_AUDIO_LOCK:
            cached = self.RECORD_AUDIO_CACHE.get(url)
        if cached:
            return self._tts().convert_reference_audio(cached)
        try:
            request = urllib.request.Request(url, headers={
                'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                               'AppleWebKit/537.36 (KHTML, like Gecko) '
                               'Chrome/120.0 Safari/537.36'),
                'Referer': 'https://ucontent.unipus.cn/',
            })
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read()
            if data:
                with self.RECORD_AUDIO_LOCK:
                    if len(self.RECORD_AUDIO_CACHE) >= 64:
                        self.RECORD_AUDIO_CACHE.clear()
                    self.RECORD_AUDIO_CACHE[url] = data
                return self._tts().convert_reference_audio(data)
        except Exception as exc:
            print(f"\t参考音频下载失败: {str(exc)[:80]}")
        return None

    def _dismiss_record_popup(self):
        try:
            self.driver.execute_script("""
                const selectors = ['.system-info-cloud-ok-button',
                                   '.know-box .iKnow',
                                   '.ant-modal-close',
                                   '.ant-modal-confirm-btns .ant-btn-primary'];
                for (const sel of selectors) {
                    const btn = document.querySelector(sel);
                    if (btn) { btn.click(); }
                }
            """)
            time.sleep(0.4)
        except Exception:
            pass

    def _fill_text(self, q: Question, answer: str) -> bool:
        if not q.inputs:
            return False

        expected_count = len(q.inputs)

        if expected_count == 1:
            ans = self._extract_answer_by_number(answer, q.number)
            if not ans and q.q_type == QuestionType.TEXT:
                # 翻译/总结这类整段作答：模型常常直接给一整段，不能按题号切分而丢内容
                direct = self._clean_direct_text_answer(answer)
                if len(direct) > len(ans):
                    ans = direct
            if not ans:
                answers = self._parse_banked_answer(answer, expected_count)
                ans = answers[0] if answers else ""
        else:
            answers = self._parse_banked_answer(answer, expected_count)
            success_count = 0
            for idx, (inp, ans) in enumerate(zip(q.inputs, answers), 1):
                if not ans:
                    print(f"\t题{idx}: (空)")
                    continue

                ans = _strip_markdown_text(ans)
                if not ans:
                    continue
                print(f"\t题{idx}: {ans[:60]}...")
                if self._fill_text_input_verified(inp, ans):
                    success_count += 1
                else:
                    print(f"\t题{idx}: 写入后校验失败")

            return success_count > 0

        ans = _strip_markdown_text(ans)
        if ans and q.q_type == QuestionType.MY_VOICE_TEXT:
            ans = self._limit_text_answer(ans, 500)

        print(f"\t题{q.number}: {ans[:60]}..." if ans else f"\t题{q.number}: (空)")

        if ans:
            inp = q.inputs[0]
            if not self._fill_text_input_verified(inp, ans):
                return False
            if q.q_type == QuestionType.MY_VOICE_TEXT:
                if not self._upload_my_voice_answer_file(q, ans):
                    return False
            return True

        return False

    def _fill_text_input_verified(self, inp, ans: str) -> bool:
        WebDriverHelper.simulate_typing(self.driver, inp, ans)
        time.sleep(0.08)
        if self._input_value_matches(inp, ans):
            return True

        print("\t输入框为受控组件，使用 JS 同步值")
        self.driver.execute_script("""
            const el = arguments[0];
            const value = arguments[1];
            const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                cancelable: true,
                inputType: 'insertText',
                data: value
            }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        """, inp, ans)
        time.sleep(0.12)
        return self._input_value_matches(inp, ans)

    @staticmethod
    def _input_value_matches(inp, ans: str) -> bool:
        try:
            current = inp.get_attribute('value') or ''
            return current.strip() == ans.strip()
        except:
            return False

    def _upload_my_voice_answer_file(self, q: Question, answer: str) -> bool:
        try:
            if self._my_voice_has_uploaded_file(q):
                print("\tMy voice 已存在上传文件，跳过自动上传")
                return True

            file_input = q.element.find_element(By.CSS_SELECTOR, '.question-multi-file-upload input[type="file"]')
            file_path = self._create_my_voice_pdf(answer)
            self.driver.execute_script("""
                arguments[0].style.display = 'block';
                arguments[0].style.visibility = 'visible';
                arguments[0].style.opacity = 1;
                arguments[0].style.width = '1px';
                arguments[0].style.height = '1px';
            """, file_input)
            file_input.send_keys(file_path)
            print(f"\tMy voice 已上传附件: {os.path.basename(file_path)}")
            return self._wait_my_voice_upload_ready(q)
        except Exception as e:
            print(f"\tMy voice 附件上传失败: {str(e)[:80]}")
            return False

    def _my_voice_has_uploaded_file(self, q: Question) -> bool:
        try:
            media_list = q.element.find_element(By.CSS_SELECTOR, '.question-multi-file-upload .media-list')
            return bool(media_list.find_elements(By.XPATH, './*')) or bool(media_list.text.strip())
        except:
            return False

    def _wait_my_voice_upload_ready(self, q: Question, timeout: int = 12) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self._my_voice_has_uploaded_file(q):
                return True
            time.sleep(0.5)
        print("\tMy voice 上传后未检测到文件列表变化，继续尝试提交")
        return True

    def _create_my_voice_pdf(self, answer: str) -> str:
        filename = f"unipus_my_voice_{int(time.time())}.pdf"
        path = os.path.join(tempfile.gettempdir(), filename)
        lines = self._wrap_pdf_text(answer, 82)[:24]
        if not lines:
            lines = ["My voice answer"]

        text_ops = ["BT", "/F1 12 Tf", "72 760 Td", "16 TL"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            text_ops.append(f"({escaped}) Tj")
            text_ops.append("T*")
        text_ops.append("ET")
        stream = "\n".join(text_ops).encode("latin-1", errors="replace")

        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        ]

        content = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for idx, obj in enumerate(objects, 1):
            offsets.append(len(content))
            content.extend(f"{idx} 0 obj\n".encode("ascii"))
            content.extend(obj)
            content.extend(b"\nendobj\n")

        xref_offset = len(content)
        content.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        content.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        content.extend(
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
        )

        with open(path, "wb") as f:
            f.write(content)
        return path

    @staticmethod
    def _wrap_pdf_text(text: str, width: int) -> List[str]:
        words = re.sub(r'\s+', ' ', text).strip().split(' ')
        lines = []
        current = ""
        for word in words:
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current += " " + word
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    @staticmethod
    def _clean_direct_text_answer(answer: str) -> str:
        answer = _strip_markdown_text(answer)
        answer = re.sub(r'^(My voice文字作答|简答题|答案|Answer)[：:]\s*', '', answer.strip(), flags=re.I)
        answer = re.sub(r'^\d+\s*[.、)\]]\s*', '', answer).strip()
        return answer

    @staticmethod
    def _limit_text_answer(answer: str, max_chars: int) -> str:
        answer = re.sub(r'\s+', ' ', answer).strip()
        if len(answer) <= max_chars:
            return answer

        clipped = answer[:max_chars].rstrip()
        sentence_end = max(clipped.rfind('.'), clipped.rfind('!'), clipped.rfind('?'))
        if sentence_end >= max_chars * 0.6:
            return clipped[:sentence_end + 1]
        return clipped

    def _fill_matching(self, q: Question, answer: str) -> bool:
        """匹配题：每个空只写一个字母（A-F）。"""
        total = len(q.inputs) or len(q.banked_blanks)
        letters_pool = [opt.letter.upper() for opt in q.options] or list("ABCDEF")
        letters = self._parse_matching_letters(answer, total, letters_pool)
        if not letters:
            print(f"\t匹配题没有解析出字母: {answer[:80]}")
            return False

        print(f"\t匹配答案: {letters}")
        success = 0
        for idx, inp in enumerate(q.inputs):
            letter = letters[idx] if idx < len(letters) else ""
            if not letter:
                continue
            try:
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", inp)
                time.sleep(0.1)
            except Exception:
                pass
            if self._fill_text_input_verified(inp, letter):
                success += 1
            else:
                print(f"\t空{idx + 1}: 写入 {letter} 失败")
        return success == total or (success > 0 and success == len(q.inputs))

    def _fill_paragraph_match(self, q: Question, answer: str) -> bool:
        """段落信息匹配：每个输入框只写一个段落字母。"""
        total = len(q.inputs) or len(q.banked_blanks)
        pool = [str(x).upper() for x in (q.banked_options or [])] or list("ABCDEFGHIJKLMNO")
        letters = self._parse_paragraph_letters(answer, total, pool)
        if not any(letters):
            print(f"\t段落匹配没有解析出字母: {answer[:80]}")
            return False
        print(f"\t段落匹配答案: {letters}")
        success = 0
        for idx, inp in enumerate(q.inputs):
            letter = letters[idx] if idx < len(letters) else ""
            if not letter:
                continue
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", inp)
                time.sleep(0.1)
            except Exception:
                pass
            if self._fill_text_input_verified(inp, letter):
                success += 1
            else:
                print(f"\t空{idx + 1}: 写入 {letter} 失败")
        return success == total

    @staticmethod
    def _parse_paragraph_letters(answer: str, total: int, pool: List[str]) -> List[str]:
        """解析 1.H 2.N 形式的答案；字母池由段落数决定，支持 A-Z 与重复使用。"""
        text = (answer or "").upper()
        valid = set(pool)
        found = {}
        for num, letter in re.findall(
                r'(?:^|[\s,;，；])(\d{1,2})\s*[.、:：)）=\-]?\s*([A-Z])(?![A-Z])', text):
            idx = int(num) - 1
            if 0 <= idx < total and letter in valid:
                found.setdefault(idx, letter)
        if found:
            return [found.get(i, "") for i in range(total)]
        seq = [c for c in re.findall(r'(?<![A-Z])([A-Z])(?![A-Z])', text)
               if c in valid]
        return (seq + [""] * total)[:total]

    @staticmethod
    def _parse_matching_letters(answer: str, total: int, pool: List[str]) -> List[str]:
        """把模型输出解析成按空号排序的字母列表。"""
        text = (answer or "").upper()
        valid = set(pool) if pool else set("ABCDEFGH")

        numbered = {}
        for num, letter in re.findall(r'(?:^|[\s,;，；])(\d{1,2})\s*[.、:：)）=\-]?\s*([A-H])(?![A-Z])', text):
            idx = int(num) - 1
            if 0 <= idx < total and letter in valid:
                numbered.setdefault(idx, letter)
        if numbered:
            return [numbered.get(i, "") for i in range(total)]

        seq = [ch for ch in re.findall(r'[A-H]', text) if ch in valid]
        # 去掉题目里常见的 A-F 说明文字造成的重复
        if len(seq) >= total:
            seq = seq[:total]
        return seq or []

    def _fill_unknown(self, q: Question, answer: str) -> bool:
        return False

    @staticmethod
    def _button_text(element) -> str:
        text = element.text or element.get_attribute('aria-label') or ''
        return re.sub(r'\s+', '', text).lower()

    @classmethod
    def _is_navigation_button(cls, element) -> bool:
        text = cls._button_text(element)
        return any(k in text for k in ['上一题', '下一题', '上一页', '下一页', 'prev', 'previous', 'next', 'nextquestion'])

    @staticmethod
    def _is_disabled_button(element) -> bool:
        try:
            if not element.is_enabled():
                return True
            if element.get_attribute('disabled') is not None:
                return True
            if (element.get_attribute('aria-disabled') or '').lower() == 'true':
                return True
            cls_name = (element.get_attribute('class') or '').lower()
            if 'disabled' in cls_name:
                return True
        except Exception:
            pass
        return False

    @classmethod
    def _is_submit_button(cls, element) -> bool:
        text = cls._button_text(element)
        if cls._is_navigation_button(element):
            return False
        if any(bad in text for bad in ['未完成', '已完成', '取消', '返回', '重做', '再做一次']):
            return False
        if any(k in text for k in ['提交', '确认提交', '完成', 'submit', 'done', 'finish']):
            return True
        tag_name = (element.tag_name or '').lower()
        element_type = (element.get_attribute('type') or '').lower()
        return tag_name == 'button' and element_type == 'submit'

    def submit(self) -> bool:
        priority_selectors = [
            '.submit-bar-pc--btn-1_Xvo',
            'button[type="submit"]',
            'button.submit-btn',
            '.question-common-course-page a.btn',
            '.question-common-course-page .btn',
            'a.btn',
        ]
        for selector in priority_selectors:
            try:
                btns = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in btns:
                    if (btn.is_displayed() and not self._is_disabled_button(btn)
                            and self._is_submit_button(btn)):
                        label = self._button_text(btn) or selector
                        if WebDriverHelper.safe_click(self.driver, btn):
                            print(f"\t点击提交按钮: {label}")
                            return True
            except:
                continue

        for selector in Selectors.SUBMIT_BUTTON:
            try:
                btns = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in btns:
                    if (btn.is_displayed() and not self._is_disabled_button(btn)
                            and self._is_submit_button(btn)):
                        label = self._button_text(btn) or selector
                        if WebDriverHelper.safe_click(self.driver, btn):
                            print(f"\t点击提交按钮: {label}")
                            return True
            except:
                continue

        # 老播放器底部「提 交」是个 div.btn（不是 button），上面的选择器容易漏掉；
        # 这里按可见文本兜底点一次，否则整页做完也不会交卷。
        try:
            fallback = self.driver.execute_script("""
                const clean = t => (t || '').replace(/\\s+/g, '');
                const cands = [...document.querySelectorAll('button, a, div, span')];
                for (const el of cands) {
                  if (!el.getClientRects().length) continue;
                  const cls = (el.className || '').toString().toLowerCase();
                  if (cls.includes('disabled') || el.disabled) continue;
                  const text = clean(el.innerText);
                  if (text !== '提交' && text !== '交卷' && text !== '确认提交') continue;
                  if (el.querySelector('button, a, div')) continue;
                  return el;
                }
                return null;
            """)
            if fallback is not None:
                print("\t通过文本兜底点击提交按钮")
                return WebDriverHelper.safe_click(self.driver, fallback)
        except Exception:
            pass

        return False

    @classmethod
    def _parse_choice_letters(cls, answer: str, valid_letters: List[str]) -> List[str]:
        answer = cls._clean_extracted_answer(answer).rstrip('.。').upper()
        if not re.fullmatch(r'[A-Z][A-Z\s,，、/;&+]*', answer):
            return []
        letters = list(dict.fromkeys(re.findall(r'[A-Z]', answer)))
        valid = {letter.upper() for letter in valid_letters}
        return letters if set(letters) <= valid else []

    @staticmethod
    def _letters_from_option_text(answer: str, question: Question) -> List[str]:
        """模型把选项原文写回来时，反查对应的选项字母（修复「该填字母却填单词」）。"""
        text = (answer or '').strip()
        if not text:
            return []
        lowered = text.lower()
        hits = []
        for opt in question.options:
            body = (opt.text or '').strip().lower()
            if len(body) < 6:
                continue
            body = re.sub(r'\s+', ' ', body)
            if body and body[:40] in re.sub(r'\s+', ' ', lowered):
                hits.append(opt.letter.upper())
        return list(dict.fromkeys(hits))

    @staticmethod
    def _split_numbered_answers(answer: str, expected_count: int) -> List[str]:
        """按编号从左往右顺序切分答案，避免把 3.5% 的 3. 当成新编号。"""
        text = re.sub(
            rf'^{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*', '', answer.strip(), flags=re.I
        )
        token_re = re.compile(
            r'(?<![\d.])(?:(?:空|Blank)\s*)?(\d{1,2})\s*(?:\.(?!\d)|\.(?=\d+\.\d)|[、:：\)\]])', re.I
        )
        tokens = [(int(m.group(1)), m.start(), m.end()) for m in token_re.finditer(text)]
        if not tokens:
            return []

        chosen = {}
        cursor = 0
        for number in range(1, expected_count + 1):
            candidates = [t for t in tokens if t[0] == number and t[1] >= cursor]
            if not candidates:
                return []
            chosen[number] = candidates[0]
            cursor = candidates[0][2]

        result = []
        for number in range(1, expected_count + 1):
            start = chosen[number][2]
            end = chosen[number + 1][1] if number < expected_count else len(text)
            raw_val = text[start:end].strip()
            if number == expected_count:
                # 去掉最后一个空后面附带的模型说明/注释（如 "\n\n说明：..."、"\n注：..."）
                raw_val = re.split(
                    r'\n\s*(?:说明|注|注意|备注|解析|提示|补充说明|Note)\s*[：:]',
                    raw_val,
                    maxsplit=1,
                    flags=re.I,
                )[0].strip()
                raw_val = re.split(r'\n\s*\n', raw_val, maxsplit=1)[0].strip()
            value = re.sub(r'[\s,，;；]+$', '', raw_val)
            if not value:
                return []
            result.append(value)
        return result

    @staticmethod
    def _parse_banked_answer(answer: str, expected_count: int) -> List[str]:
        result = [''] * expected_count
        answer = AnswerExecutor._normalize_answer_labels(answer)
        answer = re.sub(rf'^{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*', '', answer.strip(), flags=re.I)
        numbered = AnswerExecutor._split_numbered_answers(answer, expected_count)
        if numbered:
            return numbered
        print(f"    [调试] 清理后答案前200字: {answer[:200]}...")

        matched_any = False
        label_prefix = rf'(?:{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?'
        next_prefix = rf'(?<![$\w])(?:{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{AnswerExecutor.NUMBER_PREFIX_PATTERN})'

        for i in range(1, expected_count + 1):
            number_prefix = AnswerExecutor._number_prefix_pattern(i)
            pattern = rf'{label_prefix}{number_prefix}\s*(.*?)(?=\s*{next_prefix}\s*|$)'
            match = re.search(pattern, answer, re.DOTALL)

            if match:
                clean_ans = AnswerExecutor._clean_extracted_answer(match.group(1)).replace('\n', ' ')
                result[i - 1] = clean_ans
                matched_any = True
                print(f"    [调试] 成功提取空 {i}: '{clean_ans}'")
            else:
                print(f"    [调试] 题号 {i} 匹配失败或为空")

        if matched_any:
            return result

        print(f"    [调试] 题号匹配完全失效，启动降级切分模式")
        lines = [line.strip() for line in answer.split('\n') if line.strip()]
        content_lines = []

        for line in lines:
            clean = re.sub(
                rf'^(?:{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{AnswerExecutor.NUMBER_PREFIX_PATTERN})\s*',
                '',
                line,
                flags=re.I
            ).strip()
            if clean and not re.match(r'^\d+$', clean):
                content_lines.append(AnswerExecutor._clean_extracted_answer(clean))

        for i, content in enumerate(content_lines[:expected_count]):
            result[i] = content

        return result

    def _find_visible_dropdown_options(self, trigger=None, expected_options: Optional[List[str]] = None) -> List[Any]:
        """查找当前真正可见的下拉选项元素。

        避开 Ant Design 常见陷阱：第 1 个空收起后其 .ant-dropdown-hidden 门户仍留在 DOM 最前面，
        Selenium 的 EC.visibility_of_element_located 只检查第一个匹配节点，导致空 2~N 必然 2s 超时。
        这里逐层检查祖先可见性，并优先选取与本空候选项重合度最高、且离触发器最近的可见浮层。
        """
        js = r"""
        const trigger = arguments[0];
        const expected = (arguments[1] || []).map(s => String(s || '').trim().toLowerCase()).filter(Boolean);
        const isReallyVisible = (el) => {
            if (!el || !el.getClientRects || !el.getClientRects().length) return false;
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return false;
            let cur = el;
            while (cur && cur.nodeType === 1) {
                const cls = String(cur.className || '').toLowerCase();
                if (cls.includes('ant-dropdown-hidden') || cls.includes('ant-select-dropdown-hidden')) {
                    return false;
                }
                const st = window.getComputedStyle(cur);
                if (!st || st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') === 0) {
                    return false;
                }
                cur = cur.parentElement;
            }
            return true;
        };
        const containerSels = [
            '.ant-dropdown:not(.ant-dropdown-hidden)',
            '.ant-select-dropdown:not(.ant-select-dropdown-hidden)',
            '[role="listbox"]',
            '[role="menu"]',
            '[class*="dropdown"]:not([class*="hidden"])',
        ];
        const itemSel = '.ant-dropdown-menu-item, .ant-select-item-option, [role="option"], [role="menuitem"], li';
        const groups = [];
        const seenContainers = new Set();
        for (const csel of containerSels) {
            let containers;
            try { containers = document.querySelectorAll(csel); } catch (e) { continue; }
            for (const c of containers) {
                if (seenContainers.has(c) || !isReallyVisible(c)) continue;
                seenContainers.add(c);
                const items = Array.from(c.querySelectorAll(itemSel)).filter(it => {
                    if (!isReallyVisible(it)) return false;
                    const txt = (it.innerText || it.textContent || it.getAttribute('title') || '').trim();
                    return !!txt;
                });
                if (!items.length) continue;
                let score = 0;
                if (expected.length) {
                    const labels = items.map(it =>
                        (it.innerText || it.textContent || it.getAttribute('title') || '').trim().toLowerCase()
                    );
                    const overlap = expected.filter(e => labels.includes(e)).length;
                    score += overlap * 1000;
                }
                if (trigger && trigger.getBoundingClientRect) {
                    const tr = trigger.getBoundingClientRect();
                    const cr = c.getBoundingClientRect();
                    const dist = Math.hypot((cr.left - tr.left), (cr.top - tr.bottom));
                    score -= Math.min(dist, 900);
                }
                groups.push({items, score});
            }
        }
        if (groups.length) {
            groups.sort((a, b) => b.score - a.score);
            return groups[0].items;
        }
        const fallback = [];
        const seen = new Set();
        for (const el of document.querySelectorAll(
            '.ant-dropdown-menu-item, .ant-select-item-option, [role="option"], [role="menuitem"], li[class*="option"]'
        )) {
            if (!seen.has(el) && isReallyVisible(el)) {
                seen.add(el);
                fallback.push(el);
            }
        }
        return fallback;
        """
        try:
            res = self.driver.execute_script(js, trigger, expected_options or [])
            if isinstance(res, list):
                return res
        except Exception:
            pass
        option_selector = (
            '.ant-dropdown:not(.ant-dropdown-hidden) .ant-dropdown-menu-item, '
            '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option, '
            '[role="option"], li[class*="option"]'
        )
        visible = []
        try:
            for opt in self.driver.find_elements(By.CSS_SELECTOR, option_selector):
                try:
                    if opt.is_displayed():
                        visible.append(opt)
                except Exception:
                    continue
        except Exception:
            pass
        return visible

    def _wait_for_visible_dropdown_options(self, trigger=None, expected_options: Optional[List[str]] = None,
                                           timeout: float = 1.4) -> List[Any]:
        deadline = time.time() + max(0.1, float(timeout))
        while time.time() < deadline:
            opts = self._find_visible_dropdown_options(trigger, expected_options)
            if opts:
                return opts
            time.sleep(0.08)
        return self._find_visible_dropdown_options(trigger, expected_options)

    def _close_open_dropdown(self) -> None:
        """若页面上仍有前一个空留下的可见下拉浮层，先干净收起，避免下一个空读串菜单。"""
        if not self._find_visible_dropdown_options():
            return
        try:
            ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            pass
        deadline = time.time() + 0.35
        while time.time() < deadline:
            if not self._find_visible_dropdown_options():
                return
            time.sleep(0.07)
        # Escape 对部分 ant-dropdown(trigger='click') 不生效，点一下页面空白标题区触发外部失焦收起
        try:
            self.driver.execute_script("""
                const safe = document.querySelector('.layout-direction-container, .abs-direction, header') || document.body;
                if (safe) {
                    ['mousedown', 'mouseup', 'click'].forEach(ev => {
                        safe.dispatchEvent(new MouseEvent(ev, {bubbles: true, cancelable: true, view: window}));
                    });
                }
            """)
            time.sleep(0.12)
        except Exception:
            pass

    @staticmethod
    def _read_dropdown_display_text(select_wrapper, trigger) -> Tuple[str, str]:
        """读取下拉框当前真实显示文本与触发器 class（不写入 DOM）。"""
        def _safe_str(val) -> str:
            return val.strip() if isinstance(val, str) else ''

        for sel in ('.user-answer-text', '.ant-select-selection-item', '.ant-select-selection-selected-value'):
            try:
                elem = select_wrapper.find_element(By.CSS_SELECTOR, sel)
                text = _safe_str(getattr(elem, 'text', None)) or _safe_str(elem.get_attribute('textContent'))
                if text:
                    cls = _safe_str(trigger.get_attribute('class')) if trigger is not None else ''
                    return text, cls
            except Exception:
                continue
        try:
            text = _safe_str(getattr(trigger, 'text', None)) or _safe_str(trigger.get_attribute('textContent'))
            cls = _safe_str(trigger.get_attribute('class'))
            return text, cls
        except Exception:
            return '', ''

    def _select_native_dropdown(self, select_wrapper, target_text: str) -> Optional[bool]:
        """如果是原生 <select>，直接选中并触发标准 React change 事件；非原生返回 None。"""
        try:
            raw_tag = getattr(select_wrapper, 'tag_name', '')
            tag = raw_tag.lower() if isinstance(raw_tag, str) else ''
            native = select_wrapper if tag == 'select' else None
            if native is None:
                found = select_wrapper.find_elements(By.TAG_NAME, 'select')
                native = next((s for s in found if s.is_displayed()), None)
            if native is None:
                return None
            ok = self.driver.execute_script("""
                const sel = arguments[0];
                const want = String(arguments[1] || '').trim().toLowerCase();
                let hit = null;
                for (const opt of sel.options) {
                    const t = (opt.textContent || opt.label || '').trim().toLowerCase();
                    if (t === want) { hit = opt; break; }
                    if (!hit && want && t.includes(want)) hit = opt;
                }
                if (!hit) return false;
                const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
                if (setter) setter.call(sel, hit.value); else sel.value = hit.value;
                sel.dispatchEvent(new Event('input', {bubbles: true}));
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            """, native, target_text)
            return ok if isinstance(ok, bool) else None
        except Exception:
            return None

    def _fill_dropdown_select(self, q: Question, answer: str) -> bool:
        answers = self._parse_banked_answer(answer, len(q.banked_blanks))
        print(f"      解析答案: {answers}")
        print(f"      填空数量: {len(q.banked_blanks)}")

        WebDriverHelper.dismiss_onboarding_tips(self.driver)
        success_count = 0

        for i, (blank, ans) in enumerate(zip(q.banked_blanks, answers)):
            if not ans:
                continue

            try:
                print(f"      空{i + 1}: '{ans}'")
                select_wrapper = blank['element']
                expected_opts = blank.get('options') or []
                target_norm = re.sub(r'\s+', ' ', ans).strip().lower()
                WebDriverHelper.scroll_into_view(self.driver, select_wrapper, pause=0.06)

                native_res = self._select_native_dropdown(select_wrapper, ans)
                if native_res is True:
                    print(f"         原生下拉已选中: {ans[:20]}")
                    success_count += 1
                    continue
                if native_res is False:
                    print(f"         原生下拉未找到选项 '{ans}'")
                    continue

                try:
                    trigger = select_wrapper.find_element(
                        By.CSS_SELECTOR,
                        '.ant-dropdown-trigger, .ant-select-selector, [role="combobox"]')
                except NoSuchElementException:
                    # 通用探测拿到的 blank['element'] 本身就是触发器
                    trigger = select_wrapper

                # 防止前一个空的下拉浮层仍未收起导致串空
                self._close_open_dropdown()

                # 如果本空（例如二次修正时）已经真实填成目标答案且非占位符，直接通过校验
                cur_text, cur_cls = self._read_dropdown_display_text(select_wrapper, trigger)
                cur_norm = re.sub(r'\s+', ' ', cur_text).strip().lower()
                if (cur_norm and cur_norm != CLOZE_DROPDOWN_PLACEHOLDER.lower()
                        and cur_norm == target_norm and 'empty' not in cur_cls.lower()):
                    print(f"         已是目标答案，跳过重选: {cur_text[:20]}")
                    success_count += 1
                    continue

                # 多种方式尝试打开本空真正可见的下拉菜单（实测 ucontent 下拉对 safe_click 最稳，优先执行）
                visible_opts: List[Any] = []
                open_attempts = [
                    ('safe_click', lambda: WebDriverHelper.safe_click(self.driver, trigger)),
                    ('action', lambda: ActionChains(self.driver).move_to_element(trigger).click().perform()),
                    ('js_click', lambda: self.driver.execute_script("""
                        const t = arguments[0];
                        const sub = t.querySelector && t.querySelector('.user-answer-text, .ant-select-arrow, span, a');
                        (sub || t).click();
                    """, trigger)),
                ]
                for attempt_idx, (mode, opener) in enumerate(open_attempts):
                    try:
                        opener()
                    except Exception:
                        pass
                    if attempt_idx == 0:
                        print(f"         点击触发器打开下拉")
                    else:
                        print(f"         重试打开下拉（{mode}）")
                    visible_opts = self._wait_for_visible_dropdown_options(
                        trigger, expected_opts, timeout=0.9 if attempt_idx == 0 else 0.55
                    )
                    if visible_opts:
                        break

                if not visible_opts:
                    print(f"         下拉菜单未出现，尝试备选方案")
                    if self._force_select_by_js(select_wrapper, ans):
                        success_count += 1
                    continue

                option_clicked = False
                exact_match = None
                partial_match = None
                visible_option_labels = []
                for opt in visible_opts:
                    try:
                        raw_t = getattr(opt, 'text', None)
                        label = raw_t.strip() if isinstance(raw_t, str) and raw_t.strip() else ''
                        if not label:
                            tc = opt.get_attribute('textContent')
                            label = tc.strip() if isinstance(tc, str) and tc.strip() else ''
                        if not label:
                            tt = opt.get_attribute('title')
                            label = tt.strip() if isinstance(tt, str) and tt.strip() else ''
                        if label:
                            visible_option_labels.append(label)
                        low = re.sub(r'\s+', ' ', label).strip().lower()
                        if low == target_norm:
                            exact_match = (opt, label)
                            break
                        if partial_match is None and target_norm and (target_norm in low or low in target_norm):
                            partial_match = (opt, label)
                    except Exception:
                        continue

                chosen = exact_match or partial_match
                if chosen is not None:
                    opt_elem, label = chosen
                    try:
                        ActionChains(self.driver).move_to_element(opt_elem).click().perform()
                        option_clicked = True
                    except Exception:
                        option_clicked = WebDriverHelper.safe_click(self.driver, opt_elem)
                    if option_clicked:
                        print(f"         点击选项: {label[:25]}")

                if option_clicked:
                    verified = False
                    displayed_text = ''
                    verify_deadline = time.time() + 0.65
                    while time.time() < verify_deadline:
                        displayed_text, trigger_class = self._read_dropdown_display_text(select_wrapper, trigger)
                        disp_norm = re.sub(r'\s+', ' ', displayed_text).strip().lower()
                        if (disp_norm and disp_norm != CLOZE_DROPDOWN_PLACEHOLDER.lower()
                                and (target_norm in disp_norm or disp_norm in target_norm)):
                            verified = True
                            break
                        time.sleep(0.08)

                    # 若点击一次后菜单仍在且文本未变，再尝试 JS 直接点击该选项元素一次
                    if not verified:
                        try:
                            if chosen is not None:
                                self.driver.execute_script("arguments[0].click();", chosen[0])
                                time.sleep(0.25)
                                displayed_text, trigger_class = self._read_dropdown_display_text(select_wrapper, trigger)
                                disp_norm = re.sub(r'\s+', ' ', displayed_text).strip().lower()
                                if (disp_norm and disp_norm != CLOZE_DROPDOWN_PLACEHOLDER.lower()
                                        and (target_norm in disp_norm or disp_norm in target_norm)):
                                    verified = True
                        except Exception:
                            pass

                    if verified:
                        print(f"         验证成功，显示文本: {displayed_text[:25]}")
                        success_count += 1
                    else:
                        print(f"         点击选项后显示文本未更新（当前: '{displayed_text[:25]}'），尝试 React 同步")
                        if self._force_select_by_js(select_wrapper, ans):
                            success_count += 1
                    self._close_open_dropdown()
                else:
                    print(f"         未找到选项 '{ans}'（可见菜单项: {visible_option_labels[:6]}）")
                    if visible_option_labels:
                        # 菜单已正常展开并列出了真实候选项，说明答案不在本空可选列表内，
                        # 收起菜单并让外层触发 _repair_questions 修正，不要强塞无效文本。
                        self._close_open_dropdown()
                    elif self._force_select_by_js(select_wrapper, ans):
                        success_count += 1

            except Exception as e:
                print(f"        处理空{i + 1}失败: {str(e)[:50]}")
                logger.error(f"详细错误: {str(e)}", exc_info=True)
                self._close_open_dropdown()
                continue

        if success_count < len(q.banked_blanks):
            print(f"      下拉填空仅完成 {success_count}/{len(q.banked_blanks)} 空")
        return bool(q.banked_blanks) and success_count == len(q.banked_blanks)

    def _fill_listening_fill_in(self, q: Question, answer: str) -> bool:
        if self._is_refusal_answer(answer):
            print("\t检测到 AI 拒答/缺少听力材料提示，拒绝写入输入框")
            return False
        answers = self._parse_banked_answer(answer, len(q.inputs))
        if any(self._is_refusal_answer(a) for a in answers):
            print(f"\t检测到占位/拒答内容，拒绝写入输入框: {answers}")
            return False

        print(f"\t解析答案: {answers}")
        print(f"\t输入框数量: {len(q.inputs)}")

        success_count = 0
        for i, (blank_info, ans) in enumerate(zip(q.banked_blanks, answers)):
            if ans and blank_info['input']:
                original_ans = ans
                ans = self._normalize_listening_blank_answer(blank_info, ans)
                if ans != original_ans:
                    print(f"\t空{i + 1}: {original_ans} -> {ans}")
                else:
                    print(f"\t空{i + 1}: {ans}")
                if self._fill_text_input_verified(blank_info['input'], ans):
                    success_count += 1
                else:
                    print(f"\t空{i + 1}: 写入后校验失败")

        expected = len(q.inputs) or len(q.banked_blanks)
        if expected and success_count < expected:
            print(f"\t听力填空仅填写 {success_count}/{expected} 个空，不提交")
        return bool(expected) and success_count == expected

    def _normalize_listening_blank_answer(self, blank_info: Dict[str, Any], answer: str) -> str:
        ans = self._clean_extracted_answer(answer)
        ans = re.sub(r'\s+', ' ', ans).strip()
        left = (blank_info.get('left_context') or '').lower()
        right = (blank_info.get('right_context') or '').lower()

        if re.search(r"\b(to|for|at|in|on)\s*$", left):
            prep = re.search(r"\b(to|for|at|in|on)\s*$", left).group(1)
            ans = re.sub(rf"^{prep}\s+", "", ans, flags=re.I).strip()

        if right.startswith("at ") or right.startswith("at the "):
            ans = re.sub(r"\s+at\s+.+$", "", ans, flags=re.I).strip()
        if right.startswith("in ") or right.startswith("in the "):
            ans = re.sub(r"\s+in\s+.+$", "", ans, flags=re.I).strip()

        if len(ans.split()) > 8:
            for connector in [' at ', ' in ', ' to ', ' and ']:
                if connector in ans.lower():
                    ans = re.split(connector, ans, flags=re.I)[0].strip()
                    break

        if self._listening_left_expects_ing(left):
            ans = self._to_present_participle_phrase(ans)

        return ans

    @staticmethod
    def _listening_left_expects_ing(left_context: str) -> bool:
        left = left_context.strip().lower()
        return bool(re.search(r"\b(?:i'm|we're|you're|they're|he's|she's|is|are|am)\s*$", left))

    @staticmethod
    def _to_present_participle_phrase(answer: str) -> str:
        replacements = {
            'go': 'going',
            'have': 'having',
            'get': 'getting',
            'meet': 'meeting',
            'visit': 'visiting',
            'watch': 'watching',
            'see': 'seeing',
            'start': 'starting',
        }
        match = re.match(r"^([A-Za-z]+)\b(.*)$", answer.strip())
        if not match:
            return answer

        verb = match.group(1)
        rest = match.group(2)
        lower = verb.lower()
        if lower.endswith('ing'):
            return answer
        if lower in replacements:
            replacement = replacements[lower]
            if verb[:1].isupper():
                replacement = replacement[:1].upper() + replacement[1:]
            return replacement + rest
        return answer

    def _force_select_by_js(self, select_wrapper, value: str) -> bool:
        """备选 JS 同步：仅尝试触发真实 React 回调，严禁手动改写 .user-answer-text 欺骗自身校验。"""
        try:
            native_res = self._select_native_dropdown(select_wrapper, value)
            if native_res is not None:
                return native_res

            js = r"""
            const wrapper = arguments[0];
            const value = String(arguments[1] || '').trim();
            const trigger = wrapper.querySelector('.ant-dropdown-trigger, .ant-select-selector, [role="combobox"]') || wrapper;
            if (!trigger) return 'no_trigger';

            // 严禁手动改写 .user-answer-text 的 innerHTML/textContent，否则会自欺欺人误判“已填入”，
            // 实际 React 表单状态仍为空，提交后平台不给分。只尝试调用真实 React fiber 回调。
            const tryFiber = (node) => {
                if (!node) return '';
                const reactKey = Object.keys(node).find(k => k.startsWith('__react'));
                if (!reactKey) return '';
                let fiber = node[reactKey];
                for (let depth = 0; depth < 16 && fiber; depth++) {
                    const props = fiber.memoizedProps;
                    if (props) {
                        if (typeof props.onSelect === 'function') {
                            try { props.onSelect(value); return 'react_onSelect'; } catch (e) {}
                        }
                        if (typeof props.onChange === 'function') {
                            try { props.onChange(value); return 'react_onChange'; } catch (e) {}
                        }
                    }
                    fiber = fiber.return || fiber._debugOwner;
                }
                return '';
            };

            const res = tryFiber(trigger) || tryFiber(wrapper);
            return res || 'no_react_handler';
            """

            result = self.driver.execute_script(js, select_wrapper, value)
            print(f"        JS备选同步结果: {result}")

            target_norm = re.sub(r'\s+', ' ', value).strip().lower()
            try:
                trigger = select_wrapper.find_element(
                    By.CSS_SELECTOR,
                    '.ant-dropdown-trigger, .ant-select-selector, [role="combobox"]')
            except Exception:
                trigger = select_wrapper

            deadline = time.time() + 0.45
            while time.time() < deadline:
                displayed_text, trigger_class = self._read_dropdown_display_text(select_wrapper, trigger)
                disp_norm = re.sub(r'\s+', ' ', displayed_text).strip().lower()
                if (disp_norm and disp_norm != CLOZE_DROPDOWN_PLACEHOLDER.lower()
                        and (target_norm in disp_norm or disp_norm in target_norm)
                        and 'empty' not in trigger_class.lower()):
                    print(f"        JS备选同步已由页面渲染确认: {displayed_text[:25]}")
                    return True
                time.sleep(0.08)

            print(f"        JS备选同步后页面未真实更新为 '{value}'，判定本空未填入")
            return False

        except Exception as e:
            print(f"        JS备选同步失败: {str(e)[:50]}")
            return False

    def _sync_react_state(self, select_wrapper, value: str) -> bool:
        try:
            js = """
            var wrapper = arguments[0];
            var value = arguments[1];

            wrapper.setAttribute('data-selected-value', value);

            var hiddenInput = wrapper.querySelector('input[type="hidden"]');
            if (hiddenInput) {
                hiddenInput.value = value;
                hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
            }

            if (!window.__formData) window.__formData = {};
            var scoopIndex = wrapper.closest('[data-scoop-index]')?.getAttribute('data-scoop-index');
            if (scoopIndex) {
                window.__formData[scoopIndex] = value;
            }

            return true;
            """
            return self.driver.execute_script(js, select_wrapper, value)
        except:
            return False


# ============================================================================
# 新版 ipublish 播放器（course-v3 教材）适配
# ----------------------------------------------------------------------------
# 新农科大学英语等 course-v3 教材的题目不在顶层文档里：
#
#   ucontent.unipus.cn/_ipublishpc_default/pc.html      ← 播放器外壳（顶层文档）
#     └─ iframe#ipublish-pc-book-easy-iframe
#          └─ ipub.unipus.cn/learner/textbook/book/...  ← 题目 DOM（内容层）
#
# 旧版 .layoutBody-container / .question-common-abs-* 在顶层文档里全部失效，
# 内容层改用 data-ipublish-* 标记：
#
#   [data-ipublish-question-group-type="fill_blanks_group"]      题目组（提交单位）
#   [data-question-id][data-ipublish-question-type]              单题
#   span[data-dig-empty-id] > input[class*="answer_answerInput"] 填空空位
#   [data-option-value]                                          选词候选
#   div[class*="strongItem"]                                     单选选项字母
#   div[class*="answer_optionTF"]                                判断 T/F 选项
#   [data-ipublish-event-type="question-submit"]                 组内提交（已作答后变「重新作答」）
#
# CSS Modules 类名带 __hash 后缀（甚至含 +），只能按片段匹配，
# 例如 input[class*="answer_answerInput"]，不能写 input.answer_answerInput。
# ============================================================================


class PublishPlayer:
    """course-v3 ipublish 播放器：外壳/内容层切换与题目组状态判断。"""

    FRAME_CSS = (
        'iframe#ipublish-pc-book-easy-iframe',
        'iframe[id*="ipublish"][id*="iframe"]',
        'iframe[src*="ipub.unipus.cn"]',
    )
    SHELL_HOST_HINT = 'ucontent.unipus.cn'
    SHELL_PATH_HINT = 'publishpc_default'

    GROUP_CSS = '[data-ipublish-question-group-type]'
    QUESTION_CSS = '[data-question-id]'
    SUBMIT_CSS = '[data-ipublish-event-type="question-submit"]'

    SUBMITTED_KEYWORDS = (
        '重新作答', '再次作答', '重新提交', '再次提交',
        '已提交', '查看答案', '已批改', '修改作答', '重新编辑', '待批阅', '待评分',
    )
    SUBMISSION_STATUS_TEXT = frozenset(('已提交', '提交成功', '已批改', '待批阅', '待评分'))
    SUBMIT_ACTIONS = frozenset(('提交', '确认提交', '提交答案', '提交作答', '交卷', 'Submit'))

    @classmethod
    def has_submission_status(cls, text: str) -> bool:
        return any(
            line.strip().rstrip('。！.!') in cls.SUBMISSION_STATUS_TEXT
            or bool(re.fullmatch(r'(?:已提交|提交成功)[，,]\s*等待(?:教师)?(?:批阅|评分)[。]?', line.strip()))
            for line in text.splitlines()
        )

    @classmethod
    def group_has_submission_receipt(cls, group, button) -> bool:
        if button is not None:
            if any(word in cls.button_text(button) for word in cls.SUBMITTED_KEYWORDS):
                return True
            if button.is_displayed():
                return False
        if any(preview.is_displayed() for preview in
               group.find_elements(By.CSS_SELECTOR, '[class*="answerPreview"]')):
            return True
        return cls.has_submission_status(group.text or '')

    @staticmethod
    def reset(driver) -> None:
        """回到顶层文档；没有 iframe 或已被清理时静默忽略。"""
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

    @classmethod
    def find_frame(cls, driver):
        """在顶层文档定位内容 iframe，找不到返回 None。"""
        cls.reset(driver)
        for css in cls.FRAME_CSS:
            try:
                frames = driver.find_elements(By.CSS_SELECTOR, css)
            except Exception:
                continue
            if frames:
                return frames[0]
        return None

    @classmethod
    def is_publish_page(cls, driver) -> bool:
        """当前页面是否为新版 ipublish 播放器（course-v3）。

        判定条件（满足其一）：
        1. 地址是播放器外壳（ucontent.unipus.cn + _ipublishpc_default）；
        2. 内容 iframe 里确实有 ipublish 的题目组/提交按钮标记。

        第 2 条要求真的看到标记，避免旧教材页面上恰好嵌了 ipub 域名的
        iframe 时被误判成 course-v3，把旧版解析流程顶掉。
        """
        try:
            url = driver.current_url or ''
        except Exception:
            url = ''
        if cls.SHELL_HOST_HINT in url and cls.SHELL_PATH_HINT in url:
            return True

        frame = cls.find_frame(driver)
        if frame is None:
            return False
        try:
            driver.switch_to.frame(frame)
            try:
                return bool(driver.find_elements(By.CSS_SELECTOR, cls.GROUP_CSS)
                            or driver.find_elements(By.CSS_SELECTOR, cls.SUBMIT_CSS))
            finally:
                cls.reset(driver)
        except Exception:
            cls.reset(driver)
            return False

    @staticmethod
    def button_text(element) -> str:
        """读取按钮/标签文本，兼容只有内部 span 有文字的实现。"""
        text = ''
        try:
            text = element.text or ''
        except Exception:
            text = ''
        if not text:
            try:
                text = element.get_attribute('innerText') or ''
            except Exception:
                text = ''
        if not text:
            try:
                # 折叠面板中的已完成组没有 innerText，仍应识别「重新作答」。
                text = element.get_attribute('textContent') or ''
            except Exception:
                text = ''
        return re.sub(r'\s+', '', text)


class PublishFrameContext:
    """进入 ipublish 内容 iframe 的上下文管理器，退出时回到顶层文档。"""

    def __init__(self, driver):
        self.driver = driver
        self.entered = False

    def __enter__(self):
        self.entered = self.enter()
        return self

    def __exit__(self, *_exc):
        PublishPlayer.reset(self.driver)
        return False

    def enter(self) -> bool:
        for _ in range(3):
            frame = PublishPlayer.find_frame(self.driver)
            if frame is None:
                return False
            try:
                self.driver.switch_to.frame(frame)
                self.entered = True
                return True
            except Exception:
                time.sleep(0.5)
        return False


class PublishScopeChanged(ValueError):
    """当前文档或锚点已失效，需要重新核对任务。"""


@dataclass(frozen=True)
class PublishTaskScope:
    """同一 iframe 中当前目录任务的正文范围；保存锚点以兼容批改后的 DOM 重建。"""

    document_url: str
    start_anchor: str = ''
    end_anchor: str = ''
    title: str = ''
    block_only: bool = False

    @classmethod
    def capture_visible(cls, driver):
        """快速处理只取视口中的题块；iframe 初始锚点不随手动滚动更新。"""
        state = driver.execute_script("""
            const selector = '[data-lexical-node-type="question-block"]';
            const blocks = [...document.querySelectorAll(selector + ', [data-ipublish-question-group-type]')]
                .filter(el => el.matches(selector) || !el.closest(selector));
            const candidates = blocks.map(el => {
                const r = el.getBoundingClientRect();
                let top = Math.max(0, r.top), bottom = Math.min(innerHeight, r.bottom);
                let left = Math.max(0, r.left), right = Math.min(innerWidth, r.right);
                for (let p = el.parentElement; p; p = p.parentElement) {
                    const style = getComputedStyle(p), box = p.getBoundingClientRect();
                    if (/auto|scroll|hidden|clip/.test(style.overflowY)) {
                        top = Math.max(top, box.top); bottom = Math.min(bottom, box.bottom);
                    }
                    if (/auto|scroll|hidden|clip/.test(style.overflowX)) {
                        left = Math.max(left, box.left); right = Math.min(right, box.right);
                    }
                }
                const anchor = el.dataset.lexicalNodeAnchor || el.id;
                const visible = el.getClientRects().length && right > left && bottom > top;
                return {el, anchor, height: visible ? bottom - top : 0};
            }).filter(item => item.anchor && item.height > 0)
              .sort((a, b) => b.height - a.height);
            if (!candidates.length) return {error: '当前视口没有题目，请先滚动到需要处理的题块'};
            const {el, anchor} = candidates[0];
            const root = el.closest('.editor-content') || document.body;
            const headings = [...root.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(head =>
                !head.closest('[data-ipublish-question-group-type]') &&
                (head.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
            const heading = headings[headings.length - 1];
            return {document_url: location.href.split('#')[0], start_anchor: anchor,
                    title: heading ? (heading.innerText || heading.textContent || '').trim() : '当前题块',
                    block_only: true};
        """)
        if not state or state.get('error'):
            raise ValueError((state or {}).get('error', '无法定位当前可见题块'))
        return cls(**state)

    @classmethod
    def capture(cls, driver, anchor: str = '', *, task_path=(), require_task_path=False):
        """调用前进入内容层。无法确认任务边界时抛错，禁止退回整单元作答。"""
        state = driver.execute_script(r"""
            let anchor = arguments[0] || decodeURIComponent(location.hash.slice(1));
            const url = location.href.split('#')[0];
            const find = id => document.getElementById(id) ||
                [...document.querySelectorAll('[data-lexical-node-anchor]')]
                    .find(el => el.dataset.lexicalNodeAnchor === id);
            const taskPath = arguments[1] || [];
            if (taskPath.length) {
                const normalize = text => (text || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const expected = taskPath.map(normalize);
                const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
                    .filter(el => !el.closest('[data-ipublish-question-group-type]'));
                const ancestors = [], matches = [];
                for (const el of headings) {
                    const rank = Number(el.tagName.slice(1));
                    while (ancestors.length && ancestors.at(-1).rank >= rank) ancestors.pop();
                    ancestors.push({rank, label: normalize(el.innerText || el.textContent)});
                    const path = ancestors.slice(-expected.length);
                    if (path.length === expected.length && path.every((part, i) => part.label === expected[i]))
                        matches.push(el);
                }
                const match = matches.length === 1 ? matches[0] : null;
                const matchedAnchor = match && (match.dataset.lexicalNodeAnchor || match.id);
                if (matchedAnchor)
                    anchor = matchedAnchor;
                else if (arguments[2] || !anchor || !find(anchor) || matches.length > 1)
                    return {error: '无法唯一匹配当前任务的完整小节标题'};
            }
            if (!anchor) {
                if (document.querySelector('[data-lexical-node-type="heading"], [data-marker-id*="#"]'))
                    return {error: '没有当前任务锚点'};
                return {document_url: url};
            }
            const start = find(anchor);
            if (!start) return {error: '找不到当前任务正文锚点: ' + anchor};
            const title = (start.innerText || start.textContent || '').trim();
            if (!/^H[1-6]$/.test(start.tagName)) {
                if (start.matches('[data-lexical-node-type="question-block"], ' +
                                  '[data-ipublish-question-group-type]'))
                    return {document_url: url, start_anchor: anchor, title, block_only: true};
                return {error: '当前任务锚点不是可识别的章节标题或题块'};
            }
            const root = start.closest('.editor-content') || document.body;
            const rank = Number(start.tagName.slice(1));
            const end = [...root.querySelectorAll('h1,h2,h3,h4,h5,h6')].find(el =>
                !el.closest('[data-ipublish-question-group-type]') &&
                Number(el.tagName.slice(1)) <= rank &&
                (start.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
            const endAnchor = end && (end.dataset.lexicalNodeAnchor || end.id);
            if (end && !endAnchor) return {error: '下一任务标题缺少定位锚点'};
            return {document_url: url, start_anchor: anchor, end_anchor: endAnchor || '', title};
        """, anchor, list(task_path), require_task_path)
        if not state or state.get('error'):
            raise ValueError((state or {}).get('error', '无法读取当前任务范围'))
        return cls(**state)

    def select(self, driver, groups: List[Dict]) -> List[Dict]:
        """校验文档与边界后筛选组；忽略视口位置，折叠题也按正文所属任务判断。"""
        mask = self._filter_nodes(driver, [state['element'] for state in groups])
        return [state for state, included in zip(groups, mask) if included]

    def query(self, driver, selector: str) -> List[Any]:
        """在同一次 DOM 读取中定位并过滤候选，减少传输无关或已卸载的节点。"""
        return self._filter_nodes(driver, selector)

    def _filter_nodes(self, driver, nodes_or_selector):
        result = driver.execute_script("""
            const [url, startId, endId, blockOnly, nodesOrSelector] = arguments;
            const find = id => document.getElementById(id) ||
                [...document.querySelectorAll('[data-lexical-node-anchor]')]
                    .find(el => el.dataset.lexicalNodeAnchor === id);
            if (location.href.split('#')[0] !== url) return null;
            const start = startId ? find(startId) : null, end = endId ? find(endId) : null;
            if ((startId && !start) || (endId && !end)) return null;
            if (end && !(start.compareDocumentPosition(end) & Node.DOCUMENT_POSITION_FOLLOWING))
                return null;
            const inScope = node => {
                if (!node.isConnected) return false;
                if (!start) return true;
                if (blockOnly) return start === node || start.contains(node);
                return (start === node || start.contains(node) ||
                    !!(start.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING)) &&
                    (!end || !!(node.compareDocumentPosition(end) & Node.DOCUMENT_POSITION_FOLLOWING));
            };
            if (typeof nodesOrSelector === 'string')
                return Array.from(document.querySelectorAll(nodesOrSelector)).filter(inScope);
            return nodesOrSelector.map(inScope);
        """, self.document_url, self.start_anchor, self.end_anchor, self.block_only, nodes_or_selector)
        if result is None:
            raise PublishScopeChanged('当前任务文档或边界已变化，停止作答以免越界')
        return result


class PublishQuestionParser:
    """course-v3 播放器题目解析：按 data-ipublish-* 标记读取题目组。"""

    GROUP_KINDS = {
        'single_choice_group': 'choice',
        'multiple_choice_group': 'choice',
        'multi_choice_group': 'choice',
        'evaluation_group': 'choice',
        'true_false_group': 'true_false',
        'fill_blanks_group': 'fill',
        'fill_blanks_choice_group': 'fill',
        'fill_blanks_dropdown_group': 'dropdown',
        'short_answer_group': 'text',
        'translation_group': 'text',
        'writing_group': 'text',
        'multi_media_upload_group': 'upload',
        'ai_oral_practice_group': 'ai_oral',
        'rich_text_read_group': 'skip',
        'discussion_group': 'skip',
        'text_match_group': 'link_match',
        'oral_personal_state_group': 'record',
        'oral_sentence_scoop_group': 'record',
    }

    QUESTION_TYPE_KINDS = {
        'single_choice': 'choice',
        'multiple_choice': 'choice',
        'evaluation': 'choice',
        'true_false': 'true_false',
        'fill_blanks': 'fill',
        'fill_blanks_choice': 'fill',
        'fill_blanks_dropdown': 'dropdown',
        'short_answer': 'text',
        'translation': 'text',
        'writing': 'text',
        'multi_media_upload': 'upload',
        'oral_personal_state': 'record',
        'oral_sentence_scoop': 'record',
    }
    CHOICE_HEADER_SKIP = 'strongListView'
    EXPLANATION_PLACEHOLDERS = ('请输入',)
    TITLE_NOISE_WORDS = ('自动判题', 'AI判题', 'AI 判题', '计分', '已提交', '重新作答', '提交')
    TRUE_FALSE_LABELS = {'T': ('T', 'TRUE', '正确'), 'F': ('F', 'FALSE', '错误')}
    WORD_FORM_CHANGE = re.compile(
        r"\bchange\s+(?:(?:the|its|their)\s+)?(?:word\s+)?forms?\b|"
        r"\b(?:appropriate|correct|proper|suitable)\s+(?:word\s+)?forms?\b|"
        r"(?:改变|变换|调整)词形|(?:适当|正确)的?词形", re.I,
    )
    WORD_FORM_UNCHANGED = re.compile(
        r"\b(?:do not|don['’]t|never|without)\s+chang(?:e|ing)\s+"
        r"(?:(?:the|its|their)\s+)?(?:word\s+)?forms?\b|"
        r"\bin\s+(?:the|its|their)\s+original\s+forms?\b|"
        r"(?:无需|不要|不得|不必)(?:改变|变换|调整)词形", re.I,
    )

    def __init__(self, driver):
        self.driver = driver
        self.unsupported: List[str] = []
        self.passages: List[str] = []

    # ---- 题目组 ---------------------------------------------------------
    @classmethod
    def classify_group(cls, group_type: str) -> str:
        return cls.GROUP_KINDS.get((group_type or '').strip(), 'unknown')

    def collect_groups(self) -> List[Dict[str, Any]]:
        """读取内容层全部题目组（调用前需已进入内容 iframe）。"""
        groups = []
        try:
            elements = self.driver.find_elements(By.CSS_SELECTOR, PublishPlayer.GROUP_CSS)
        except Exception:
            return groups
        for element in elements:
            group_type = (element.get_attribute('data-ipublish-question-group-type') or '').strip()
            if not group_type:
                continue
            try:
                visible = element.is_displayed()
            except Exception:
                visible = False
            groups.append({
                'element': element,
                'type': group_type,
                'kind': self.classify_group(group_type),
                'visible': visible,
            })
        return groups

    def submit_button(self, group):
        try:
            buttons = group.find_elements(By.CSS_SELECTOR, PublishPlayer.SUBMIT_CSS)
        except Exception:
            return None
        for button in buttons:
            try:
                if button.is_displayed():
                    return button
            except Exception:
                continue
        return buttons[0] if buttons else None

    def group_submitted(self, group) -> bool:
        """已批改/已提交的题目组不再重复作答。"""
        try:
            return PublishPlayer.group_has_submission_receipt(group, self.submit_button(group))
        except WebDriverException:
            return False

    def group_key(self, group) -> str:
        """题目组指纹：组 id + 题目 id，用于去重。"""
        group_id = group.get_attribute('id') or ''
        group_type = group.get_attribute('data-ipublish-question-group-type') or ''
        question_ids = []
        for node in self._question_nodes(group):
            try:
                question_ids.append(node.get_attribute('data-question-id') or '')
            except Exception:
                continue
        if not group_id and not question_ids:
            return f'{group_type}:{self.element_text(group, 120)}'
        return f'{group_type}:{group_id}:{",".join(question_ids)}'

    # ---- 文本工具 -------------------------------------------------------
    @staticmethod
    def clean_text(text: str, limit: Optional[int] = 4000) -> str:
        text = re.sub(r'[ \t]+', ' ', text or '')
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()[:limit]

    def element_text(self, element, limit: Optional[int] = 4000) -> str:
        text = ''
        try:
            text = element.text or ''
        except Exception:
            text = ''
        if not text.strip():
            try:
                text = self.driver.execute_script(
                    'return arguments[0] && arguments[0].innerText ? arguments[0].innerText : "";',
                    element) or ''
            except Exception:
                text = ''
        return self.clean_text(text, limit)

    def _stem_text(self, node, skip_class: str = '') -> str:
        """题目正文：Lexical 只读编辑器里的文本块（跳过选项区/理由框）。"""
        try:
            contents = node.find_elements(By.CSS_SELECTOR, '.editor-content')
        except Exception:
            contents = []
        parts = []
        for content in contents:
            try:
                if content.find_elements(By.XPATH, 'ancestor::*[contains(@class, "q-answer-and-analysis")]'):
                    continue
            except Exception:
                pass
            if skip_class:
                try:
                    ancestor = content.find_elements(
                        By.XPATH, f'ancestor::*[contains(@class, "{skip_class}")]')
                    if ancestor:
                        continue
                except Exception:
                    pass
            text = self.element_text(content, limit=None)
            if text and text not in parts:
                parts.append(text)
        return self.clean_text('\n\n'.join(parts), limit=None) if parts else self._fallback_text(node)

    def _fallback_text(self, node) -> str:
        return self._dom_text(node, limit=None)

    def _dom_text(self, node, numbered: bool = False, limit: Optional[int] = 4000) -> str:
        """剔除作答 UI，保留段落/表格边界；脱离文档的 clone.innerText 会粘连文本。"""
        script = """
        var node = arguments[0];
        if (!node) { return ""; }
        var clone = node.cloneNode(true);
        if (arguments[1]) {
            clone.querySelectorAll('span[data-dig-empty-id]').forEach((blank, i) => {
                blank.replaceWith(document.createTextNode('(' + (i + 1) + ')'));
            });
        }
        var drop = '[class*="options"], [class*="ListView"], [class*="shortAnswerWrap"], ' +
                   'textarea, input, button, [data-ipublish-event-type], ' +
                   '[class*="answerPreview"], [class*="upload"], [class*="title-content"], ' +
                   '[data-option-value], [class*="answerContent"], audio, video, ' +
                   'script, style, [aria-hidden="true"]';
        clone.querySelectorAll(drop).forEach(function (el) { el.remove(); });
        function text(el) {
            if (el.nodeType === Node.TEXT_NODE) return el.textContent;
            if (el.nodeType !== Node.ELEMENT_NODE) return '';
            if (el.tagName === 'BR') return '\\n';
            const children = Array.from(el.childNodes).map(text).join('');
            if (/^(TD|TH)$/.test(el.tagName)) return children.trim() + '\\t';
            if (/^(P|DIV|H[1-6]|LI|TR|TABLE|UL|OL|BLOCKQUOTE)$/.test(el.tagName))
                return '\\n' + children + '\\n';
            return children;
        }
        return text(clone);
        """
        try:
            text = self.driver.execute_script(script, node, numbered) or ''
        except Exception:
            text = ''
        # 表格的单元格使用分隔符，避免列标题和答案空位粘在一起。
        text = re.sub(r'\t[ \t]*\n', '\n', text).replace('\t', ' | ')
        return self.clean_text(text, limit)

    def group_direction(self, group) -> str:
        """收集组标题及小题外的公共题面，不混入各题选项或已答内容。"""
        text = ''
        try:
            titles = group.find_elements(
                By.CSS_SELECTOR, '[class*="title-content"], [class*="titleContent"]')
        except Exception:
            titles = []
        for title in titles:
            text = self.element_text(title, 300)
            if text:
                break
        for word in self.TITLE_NOISE_WORDS:
            text = text.replace(word, '')
        text = re.sub(r'得分[：:]\s*\d+(?:\.\d+)?\s*分', '', text)
        text = re.sub(r'小题\s*\d+', '', text)
        text = re.sub(r'^\s*\d+\s*分\s*$', '', text)
        title = re.sub(r'\s+', ' ', text).strip()
        try:
            introductions = self.driver.execute_script("""
                const group = arguments[0];
                return Array.from(group.querySelectorAll('.editor-content'))
                    .filter(el => !el.closest('[data-question-id]') &&
                        el.closest('[data-ipublish-question-group-type]') === group)
                    .map(el => el.innerText || '').filter(text => text.trim());
            """, group) or []
        except Exception:
            introductions = []
        preceding = self._preceding_direction(group)
        parts = list(dict.fromkeys([part for part in [preceding, title, *introductions] if part]))
        return self.clean_text('\n'.join(parts), 12000)

    def _preceding_direction(self, group) -> str:
        """Lexical 题块前连续的正文/提示块也是题面，不能只读组内的作答占位语。"""
        try:
            blocks = self.driver.execute_script("""
                const block = arguments[0].closest('[data-lexical-node-type="question-block"]');
                const parts = [];
                for (let el = block && block.previousElementSibling; el && parts.length < 12;
                     el = el.previousElementSibling) {
                    const type = el.getAttribute('data-lexical-node-type') || '';
                    if (!/^(base-paragraph|paragraph|highlight-block|table|list)$/.test(type) &&
                        !/^(P|TABLE|UL|OL|BLOCKQUOTE)$/.test(el.tagName)) break;
                    if (el.querySelector('[data-ipublish-question-group-type]')) break;
                    parts.unshift(el);
                }
                return parts;
            """, group) or []
            return '\n\n'.join(filter(None, (self._dom_text(block, limit=12000) for block in blocks)))
        except Exception:
            return ''

    # ---- 题目节点 -------------------------------------------------------
    def _question_nodes(self, group) -> List[Any]:
        """组内最外层题目节点（排除带 data-option-value 的候选项）。"""
        nodes = []
        try:
            candidates = group.find_elements(By.CSS_SELECTOR, PublishPlayer.QUESTION_CSS)
        except Exception:
            return nodes
        for node in candidates:
            try:
                if node.get_attribute('data-option-value'):
                    continue
                if node.find_elements(
                        By.XPATH, 'ancestor::*[@data-question-id][not(@data-option-value)]'):
                    continue
            except Exception:
                continue
            nodes.append(node)
        return nodes

    @staticmethod
    def _question_type(node) -> str:
        try:
            tagged = node.find_elements(By.CSS_SELECTOR, '[data-ipublish-question-type]')
        except Exception:
            tagged = []
        for element in tagged:
            value = (element.get_attribute('data-ipublish-question-type') or '').strip()
            if value:
                return value
        try:
            return (node.get_attribute('data-ipublish-question-type') or '').strip()
        except Exception:
            return ''

    # ---- 解析入口 -------------------------------------------------------
    def parse_group(self, group, group_type: str, start_number: int = 1,
                    *, directions: Optional[str] = None) -> List[Question]:
        """解析单个题目组；不支持的题型记入 unsupported 并返回空列表。"""
        kind = self.classify_group(group_type)
        if kind == 'unknown':
            # 播放器新增了未登记的组类型时，退回题目自身的 data-ipublish-question-type
            kind = self._kind_from_questions(group)
        if kind == 'choice':
            return self._parse_choices(group, group_type, start_number)
        if kind == 'true_false':
            return self._parse_true_false(group, start_number)
        if kind == 'fill':
            if group_type != 'fill_blanks_choice_group':
                return self._parse_fill_blanks(group, start_number)
            if directions is None:
                directions = self.group_direction(group)
            change_form = bool(self.WORD_FORM_CHANGE.search(directions)
                               and not self.WORD_FORM_UNCHANGED.search(directions))
            return self._parse_fill_blanks(group, start_number, directions=directions,
                                           strict_word_bank=not change_form)
        if kind == 'dropdown':
            return self._parse_fill_blanks(group, start_number, dropdown=True)
        if kind == 'text':
            return self._parse_text(group, start_number)
        if kind == 'upload':
            return self._parse_media_upload(group, start_number)
        if kind == 'link_match':
            return self._parse_link_match(group, start_number)
        if kind == 'record':
            return self._parse_record(group, start_number, group_type)
        if kind != 'skip':
            self.unsupported.append(group_type)
        return []

    def _kind_from_questions(self, group) -> str:
        for node in self._question_nodes(group):
            kind = self.QUESTION_TYPE_KINDS.get(self._question_type(node))
            if kind:
                return kind
        return 'unknown'

    def collect_passages(self, groups: List[Dict[str, Any]]) -> List[str]:
        """收集阅读材料，作为 AI 上下文。"""
        passages = []
        for state in groups:
            if state['type'] != 'rich_text_read_group':
                continue
            text = self._dom_text(state['element'], limit=None)
            if len(text) >= 80:
                passages.append(text)
        self.passages = passages
        return passages

    # ---- 各题型 ---------------------------------------------------------
    def _parse_choices(self, group, group_type: str, start_number: int) -> List[Question]:
        questions = []
        multiple = 'multiple' in group_type or 'multi_choice' in group_type
        nodes = self._question_nodes(group)
        for node in nodes:
            text = self._stem_text(node, skip_class=self.CHOICE_HEADER_SKIP)
            if not text and len(nodes) == 1:
                text = self.group_direction(group)
            options = self._choice_options(node)
            if not text or not options:
                continue
            questions.append(Question(
                number=start_number + len(questions),
                text=text,
                q_type=QuestionType.MULTIPLE_CHOICE if multiple else QuestionType.SINGLE_CHOICE,
                element=node,
                options=options,
                group=group,
            ))
        return questions

    def _parse_record(self, group, start_number: int, group_type: str = '') -> List[Question]:
        questions = []
        nodes = self._question_nodes(group)
        for node in nodes:
            buttons = node.find_elements(
                By.CSS_SELECTOR,
                '[class*="i-record_record-wave-wrapper"], [class*="result_re-record-wrapper"], '
                '[class*="i-oral-reply_oral-base-re-record-wrapper"]')
            text = self._stem_text(node)
            if not text and len(nodes) == 1:
                text = self.group_direction(group)
            if not buttons or not text:
                continue
            samples = node.find_elements(By.CSS_SELECTOR, '[class*="answer_sample-audio-wrapper"] audio')
            url = samples[0].get_attribute('src') if samples else ''
            if (group_type == 'oral_sentence_scoop_group' or
                    self._question_type(node) == 'oral_sentence_scoop') and not url:
                continue
            questions.append(Question(
                number=start_number + len(questions), text=text,
                q_type=QuestionType.RECORD, element=node, group=group,
                record_items=[{'url': url or '', 'element': buttons[0], 'text': text,
                               'sample': samples[0] if samples else None}],
            ))
        return questions

    def _choice_options(self, node) -> List[Option]:
        options = []
        if self._question_type(node) == 'evaluation':
            radios = node.find_elements(By.CSS_SELECTOR, '[role="radio"][aria-posinset]')
            labels = node.find_elements(By.CSS_SELECTOR, '[class*="q-rates_title__"]')
            for index, radio in enumerate(radios):
                value = radio.get_attribute('aria-posinset')
                if len(labels) == 2 and index in (0, len(radios) - 1):
                    value += f' ({self.element_text(labels[0 if index == 0 else 1])})'
                options.append(Option(
                    letter=chr(65 + index), text=value, element=radio,
                    is_selected=radio.get_attribute('aria-checked') == 'true',
                ))
            return options
        try:
            rows = node.find_elements(By.CSS_SELECTOR, '[class*="strongListView"]')
        except Exception:
            rows = []
        for row in rows:
            try:
                letter_element = row.find_element(By.CSS_SELECTOR, '[class*="strongItem"]')
            except Exception:
                continue
            letter = self.element_text(letter_element, 8).strip().strip('.、):：')
            option_text = ''
            for content in row.find_elements(By.CSS_SELECTOR, '.editor-content'):
                option_text = self.element_text(content)
                if option_text:
                    break
            if not letter:
                letter = chr(65 + len(options))
            try:
                classes = ((letter_element.get_attribute('class') or '') + ' ' +
                           (row.get_attribute('class') or '')).lower()
            except Exception:
                classes = ''
            options.append(Option(
                letter=letter.upper(),
                text=option_text,
                element=letter_element,
                is_selected='selected' in classes or 'sele' in classes,
            ))
        return options

    def _parse_true_false(self, group, start_number: int) -> List[Question]:
        questions = []
        for node in self._question_nodes(group):
            text = self._stem_text(node, skip_class='answer_shortAnswerWrap')
            options = self._true_false_options(node)
            if not text or not options:
                continue
            questions.append(Question(
                number=start_number + len(questions),
                text=text,
                q_type=QuestionType.TRUE_FALSE,
                element=node,
                options=options,
                group=group,
            ))
        return questions

    def _true_false_options(self, node) -> List[Option]:
        options = []
        try:
            rows = node.find_elements(By.CSS_SELECTOR, 'div[class*="answer_option"]')
        except Exception:
            rows = []
        for row in rows:
            label = self.element_text(row, 12).strip().upper()
            letter = ''
            for candidate, labels in self.TRUE_FALSE_LABELS.items():
                if label in labels:
                    letter = candidate
                    break
            if not letter:
                continue
            if any(option.letter == letter for option in options):
                continue
            try:
                classes = (row.get_attribute('class') or '').lower()
            except Exception:
                classes = ''
            options.append(Option(
                letter=letter,
                text='True' if letter == 'T' else 'False',
                element=row,
                is_selected='select' in classes or 'active' in classes,
            ))
        return options

    def _parse_fill_blanks(self, group, start_number: int, dropdown: bool = False,
                           *, directions: str = '', strict_word_bank: bool = False) -> List[Question]:
        nodes = self._question_nodes(group) or [group]
        questions = []
        for node in nodes:
            text = self._numbered_stem_text(node) or self._stem_text(node)
            if not text:
                continue
            options = self._banked_options(node)
            questions.append(Question(
                number=start_number + len(questions),
                text=text,
                q_type=QuestionType.DROPDOWN_SELECT if dropdown else QuestionType.FILL_IN,
                element=node,
                inputs=self._blank_inputs(node),
                banked_options=options,
                group=group,
                blank_count=len(node.find_elements(By.CSS_SELECTOR, 'span[data-dig-empty-id]')),
                directions=directions,
                strict_word_bank=strict_word_bank and bool(options),
            ))
        return questions

    def _numbered_stem_text(self, node) -> str:
        """填空/下拉题：把每个空位替换成 (N) 标记，保证 AI 能对上空号。"""
        return self._dom_text(node, numbered=True, limit=None)

    # ---- 连线匹配 -------------------------------------------------------
    LINK_NODE_WAIT_SECONDS = 12.0

    LINK_NODES_JS = """
    const group = arguments[0];
    const nodes = [...group.querySelectorAll('.react-flow__node')];
    return nodes.map((node, index) => {
      const handle = node.querySelector('.react-flow__handle');
      return {
        i: index,
        id: node.getAttribute('data-id') || '',
        side: handle ? (handle.getAttribute('data-handlepos') || '') : '',
        text: (node.innerText || '').replace(/\\s+/g, ' ').trim(),
      };
    });
    """

    def _link_nodes(self, group) -> List[Dict[str, Any]]:
        """读取画布节点。

        连线题是 React Flow 画布：左列提示节点的连接点在自己的**右**侧
        （data-handlepos=right），右列选项节点的连接点在**左**侧（left）。
        节点文字跟正文一样是懒渲染的，没等齐就取会拿到空串，AI 只能瞎配，
        所以这里等到全部有字再返回。
        """
        deadline = time.time() + self.LINK_NODE_WAIT_SECONDS
        nodes: List[Dict[str, Any]] = []
        while True:
            try:
                raw = self.driver.execute_script(self.LINK_NODES_JS, group)
            except Exception:
                raw = None
            nodes = [item for item in (raw or []) if isinstance(item, dict)]
            if nodes and all((item.get('text') or '').strip() for item in nodes):
                return nodes
            if time.time() >= deadline:
                return nodes
            time.sleep(0.8)

    def _parse_link_match(self, group, start_number: int) -> List[Question]:
        """连线匹配：一个题目组 = 一张画布，作为单题交给 AI 配对。"""
        nodes = self._link_nodes(group)
        prompts = [item for item in nodes if item.get('side') == 'right']
        options = [item for item in nodes if item.get('side') == 'left']
        if not prompts or not options:
            self.unsupported.append('text_match_group')
            return []
        prompt_lines = [f"{index + 1}. {self._link_label(item.get('text', ''), number=True)}"
                        for index, item in enumerate(prompts)]
        return [Question(
            number=start_number,
            text='\n'.join(prompt_lines),
            q_type=QuestionType.LINK_MATCH,
            element=group,
            options=[Option(letter=chr(65 + index),
                            text=self._link_label(item.get('text', ''), number=False),
                            element=None)
                     for index, item in enumerate(options)],
            group=group,
            directions=self.group_direction(group),
            link_items=nodes,
        )]

    @staticmethod
    def _link_label(text: str, number: bool = True) -> str:
        """去掉节点自带的「1.」「A.」前缀，避免和我们的编号重复。

        画布节点文字本身就带序号（提示是 `1. Paras. 1-2`，选项是 `A. The role …`），
        直接拼会变成「1. 1. Paras. 1-2」。只在前缀后跟空白时才剥，避免误伤
        「5G」「A-level」这类正文。
        """
        pattern = r'^\s*\d{1,2}\s*[.、:：)）]\s+' if number else r'^\s*[A-Za-z]\s*[.、:：)）]\s+'
        return re.sub(pattern, '', text or '').strip()

    def _parse_text(self, group, start_number: int) -> List[Question]:
        questions = []
        for node in self._question_nodes(group):
            # TEXT 组的第一段 .editor-content 往往只是「请在此处作答」，
            # 真正题面在组内其他块里，所以优先取整组文本（已剔除按钮/输入框）。
            text = self._fallback_text(node) or self._stem_text(node)
            areas = self._answer_textareas(node)
            if not text or not areas:
                continue
            questions.append(Question(
                number=start_number + len(questions),
                text=text,
                q_type=QuestionType.TEXT,
                element=node,
                inputs=areas,
                group=group,
            ))
        return questions

    def _parse_media_upload(self, group, start_number: int) -> List[Question]:
        """解析新版 multi_media_upload_group（圆桌讨论 / 多媒体附件 / 文本+附件混合上传题）。"""
        questions = []
        nodes = self._question_nodes(group) or [group]
        direction = self.group_direction(group)
        for node in nodes:
            text = self._fallback_text(node) or self._stem_text(node) or direction
            areas = self._answer_textareas(node)
            if not areas:
                try:
                    areas = [
                        el for el in node.find_elements(By.CSS_SELECTOR, '[contenteditable="true"]')
                        if el.is_displayed()
                    ]
                except Exception:
                    areas = []
            try:
                file_inputs = node.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
            except Exception:
                file_inputs = []
            if not file_inputs and node is not group:
                try:
                    file_inputs = group.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
                except Exception:
                    file_inputs = []
            if not file_inputs:
                try:
                    block_inputs = self.driver.execute_script("""
                        const g = arguments[0];
                        const block = g && g.closest ? g.closest('[data-lexical-node-type="question-block"]') : null;
                        return block ? Array.from(block.querySelectorAll('input[type="file"]')) : [];
                    """, group)
                    if isinstance(block_inputs, list) and block_inputs:
                        file_inputs = block_inputs
                except Exception:
                    pass
            try:
                rec_buttons = node.find_elements(
                    By.CSS_SELECTOR,
                    '[class*="i-record_record-wave-wrapper"], [class*="result_re-record-wrapper"], '
                    '[class*="i-oral-reply_oral-base-re-record-wrapper"]')
            except Exception:
                rec_buttons = []

            if not areas and not file_inputs and not rec_buttons:
                continue

            accepts = []
            for fi in file_inputs:
                try:
                    acc = (fi.get_attribute('accept') or '').strip()
                    if acc:
                        accepts.append(acc)
                except Exception:
                    pass
            upload_meta = {
                'file_inputs': file_inputs,
                'accept': ','.join(accepts),
                'has_record_btn': bool(rec_buttons),
                'direction': direction,
            }
            questions.append(Question(
                number=start_number + len(questions),
                text=text or 'Please complete the speaking or writing response for this task.',
                q_type=QuestionType.TEXT,
                element=node,
                inputs=areas,
                group=group,
                directions=direction,
                needs_upload=True,
                record_items=[upload_meta],
            ))
        return questions

    def _blank_inputs(self, node) -> List[Any]:
        inputs = []
        try:
            blanks = node.find_elements(By.CSS_SELECTOR, 'span[data-dig-empty-id]')
        except Exception:
            blanks = []
        for blank in blanks:
            try:
                inputs.extend(blank.find_elements(By.CSS_SELECTOR, 'input'))
            except Exception:
                continue
        if inputs and len(inputs) >= len(blanks):
            return inputs
        # 空位里的输入框没有被完整识别（结构被重排或空位里没有 input）时，
        # 按 DOM 顺序直接取所有填空框，避免少数空位漏填。
        try:
            all_inputs = node.find_elements(
                By.CSS_SELECTOR, 'input[class*="answer_answerInput"]')
        except Exception:
            all_inputs = []
        return all_inputs or inputs

    def _banked_options(self, node) -> List[str]:
        options = []
        try:
            chips = node.find_elements(By.CSS_SELECTOR, '[data-option-value]')
        except Exception:
            chips = []
        for chip in chips:
            try:
                value = (chip.get_attribute('data-option-value') or '').strip()
            except Exception:
                value = ''
            if value and value not in options:
                options.append(value)
        return options

    def _answer_textareas(self, node) -> List[Any]:
        try:
            areas = node.find_elements(By.CSS_SELECTOR, 'textarea')
        except Exception:
            return []
        answer_areas = []
        for area in areas:
            try:
                placeholder = (area.get_attribute('placeholder') or '').strip()
            except Exception:
                placeholder = ''
            if placeholder in self.EXPLANATION_PLACEHOLDERS:
                continue
            answer_areas.append(area)
        return answer_areas or areas


class PublishAnswerExecutor(AnswerExecutor):
    """course-v3 作答执行器：复用基础填写，补充判断题、下拉填空与组内提交。"""

    RULE_POPUP_TEXTS = ('我知道了', '知道了', '确定')
    last_rejection = ''  # 最近一次提交被平台拒绝时的提示文案

    def __init__(self, driver, tts=None, stop_requested=None):
        super().__init__(driver, tts=tts, stop_requested=stop_requested)

    def _fill_single_choice(self, q: Question, answer: str) -> bool:
        if q.options and q.element.find_elements(By.CSS_SELECTOR, '[data-ipublish-question-type="evaluation"]'):
            letters = self._parse_choice_letters(answer, [opt.letter for opt in q.options])
            if len(letters) != 1:
                return False
            index = next(i for i, opt in enumerate(q.options) if opt.letter == letters[0])
            selected = [opt.element.get_attribute('aria-checked') == 'true' for opt in q.options]
            if selected[index] and (index == len(selected) - 1 or not selected[index + 1]):
                return True
            if not WebDriverHelper.safe_click(self.driver, q.options[index].element):
                return False
            selected = [opt.element.get_attribute('aria-checked') == 'true' for opt in q.options]
            return selected[index] and (index == len(selected) - 1 or not selected[index + 1])
        return super()._fill_single_choice(q, answer)

    def play_material(self, media, timeout: float = 20.0) -> bool:
        """实际播放材料至少三秒；短音频须播放到结束。"""
        try:
            start = self.driver.execute_script("""
                const media = arguments[0];
                media.currentTime = 0;
                const start = media.currentTime || 0;
                media.muted = true;
                const playing = media.play();
                if (playing && playing.catch) playing.catch(() => {});
                return start;
            """, media)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not self.stop_requested.is_set():
                state = self.driver.execute_script("""
                    const media = arguments[0];
                    return {time: media.currentTime || 0, ended: media.ended,
                            error: !!media.error};
                """, media) or {}
                elapsed = float(state.get('time') or 0) - float(start or 0)
                if elapsed >= 3 or (state.get('ended') and elapsed > 0):
                    return True
                if state.get('error') or self.stop_requested.wait(0.25):
                    break
        except Exception as exc:
            print(f"    材料播放失败: {str(exc)[:80]}")
        finally:
            try:
                self.driver.execute_script('arguments[0].pause();', media)
            except Exception:
                pass
        return False

    def _answer_record(self, q: Question, answer: str = "") -> bool:
        """新版口语题按准备时间、真实录音和上传状态逐题处理。"""
        item = q.record_items[0]
        url = (item.get('url') or '').strip()
        if url and item.get('sample') and not self.play_material(item['sample']):
            print("\t样音播放失败")
            return False
        if url:
            data = self._download_record_audio(url)
        else:
            text = (item.get('text') or '').strip() or self._record_reading_text(q) or answer
            data = self._speech_audio(text, 0)
        if not data or self.stop_requested.is_set():
            return False
        started = False
        try:
            prep = self.driver.execute_script(r"""
                const boxes = arguments[0].querySelectorAll('[class*="i-record-count-down_title-wrapper"]');
                for (const box of boxes) {
                    const text = (box.innerText || '').replace(/\s/g, '');
                    const match = text.match(/准备时长(\d{2}):(\d{2})/);
                    if (match) return Number(match[1]) * 60 + Number(match[2]);
                }
                return 3;
            """, q.element)
            prep = max(0.0, min(float(prep), 30.0))
            state = self.driver.execute_async_script(
                RECORD_MIC_JS, base64.b64encode(data).decode(),
                prep + self.RECORD_LEAD_PAD, self.RECORD_MAX_SECONDS)
            if not state or not state.get('ok') or not state.get('dur'):
                print("\t录音注入初始化失败")
                return False
            before = self._publish_record_state(q)
            if not self._click_publish_record(q):
                return False
            started = True
            deadline = time.monotonic() + prep + 15
            while time.monotonic() < deadline and not self.stop_requested.is_set():
                current = self._publish_record_state(q)
                if current.get('calls', 0) > before.get('calls', 0):
                    break
                if self.stop_requested.wait(0.25):
                    break
            else:
                current = {}
            if not current or current.get('calls', 0) <= before.get('calls', 0):
                print("\t录音器未启动，停止本题")
                return False
            if self.stop_requested.wait(float(state['dur']) + 1.0):
                return False
            if not self._click_publish_record(q):
                return False
            started = False
            deadline = time.monotonic() + max(30.0, min(120.0, float(state['dur'])))
            previous = set(before.get('uploaded') or [])
            while time.monotonic() < deadline and not self.stop_requested.is_set():
                current = self._publish_record_state(q)
                if set(current.get('uploaded') or []) - previous:
                    print("\t录音已上传")
                    return True
                if self.stop_requested.wait(0.5):
                    break
            print("\t未能确认录音上传完成，不提交")
        except Exception as exc:
            print(f"\t录音失败: {str(exc)[:80]}")
        finally:
            if started:
                try:
                    self._click_publish_record(q)
                except Exception:
                    pass
        return False

    def _click_publish_record(self, q: Question) -> bool:
        buttons = q.element.find_elements(
            By.CSS_SELECTOR,
            '[class*="i-record_record-wave-wrapper"], [class*="result_re-record-wrapper"], '
            '[class*="i-oral-reply_oral-base-re-record-wrapper"]')
        return bool(buttons and WebDriverHelper.safe_click(self.driver, buttons[0]))

    def _publish_record_state(self, q: Question) -> Dict[str, Any]:
        return self.driver.execute_script("""
            const root = arguments[0];
            return {
                calls: (window.__ucRec && window.__ucRec.calls) || 0,
                uploaded: Array.from(root.querySelectorAll('audio[src]'))
                    .filter(audio => !audio.closest('[class*="answer_sample-audio-wrapper"], ' +
                        '[class*="sample-audio"], [data-lexical-node-type="insert-audio"]'))
                    .map(audio => audio.getAttribute('src') || '')
                    .filter(src => /^https?:/i.test(src))
            };
        """, q.element) or {}

    def publish_record_uploaded(self, q: Question) -> bool:
        return bool(self._publish_record_state(q).get('uploaded'))

    def reveal_group(self, group, stop_requested: Optional[threading.Event] = None) -> bool:
        """从外向内点击折叠标题（兼容 Lexical collapsible / ant-collapse / details），使隐藏题块真正可见。"""
        try:
            contents = self.driver.execute_script("""
                const contents = [];
                for (let el = arguments[0].parentElement; el; el = el.parentElement) {
                    if (el.matches('[data-lexical-node-type="collapsible-content"], .ant-collapse-content, details'))
                        contents.unshift(el);
                }
                return contents;
            """, group) or []
            for content in contents:
                if stop_requested is not None and stop_requested.is_set():
                    return False
                head = self.driver.execute_script("""
                    const content = arguments[0];
                    if (content.tagName === 'DETAILS') {
                        return content.open ? null : content.querySelector(':scope > summary');
                    }
                    const style = getComputedStyle(content);
                    const collapsed = content.hasAttribute('hidden') || style.display === 'none' ||
                        style.contentVisibility === 'hidden' ||
                        content.classList.contains('ant-collapse-content-inactive') ||
                        (content.parentElement && content.parentElement.classList.contains('ant-collapse-item') &&
                         !content.parentElement.classList.contains('ant-collapse-item-active'));
                    if (!collapsed) return null;
                    const parent = content.parentElement;
                    if (!parent) return null;
                    return Array.from(parent.children).find(el =>
                        el.matches('[data-lexical-node-type="collapsible-head"], .ant-collapse-header, summary, [aria-expanded="false"]')) || null;
                """, content)
                if head is None:
                    continue
                if not WebDriverHelper.safe_click(self.driver, head):
                    return False
                WebDriverWait(self.driver, 2.5).until(lambda driver: driver.execute_script("""
                    const el = arguments[0];
                    if (el.tagName === 'DETAILS') return !!el.open;
                    const s = getComputedStyle(el);
                    return !el.hasAttribute('hidden') && s.display !== 'none' &&
                        s.contentVisibility !== 'hidden' &&
                        !el.classList.contains('ant-collapse-content-inactive');
                """, content))
            return True
        except Exception:
            return False

    def execute(self, question: Question, answer: str) -> bool:
        if question.needs_upload:
            return self._fill_media_upload(question, answer)
        if question.q_type == QuestionType.TRUE_FALSE:
            return self._fill_true_false(question, answer)
        if question.q_type == QuestionType.DROPDOWN_SELECT:
            return self._fill_publish_dropdown(question, answer)
        if question.q_type == QuestionType.LINK_MATCH:
            return self._fill_link_match(question, answer)
        return super().execute(question, answer)

    def _fill_media_upload(self, q: Question, answer: str) -> bool:
        """自动完成新版 multi_media_upload_group：支持文本区填写、音色克隆音频/文档/图片/视频附件上传与在线录音。"""
        clean_answer = _strip_markdown_text(answer or '').strip()
        if not clean_answer:
            print("\t多媒体上传题缺少 AI 作答内容")
            return False

        meta = (q.record_items or [{}])[0]
        file_inputs = list(meta.get('file_inputs') or [])
        accept = str(meta.get('accept') or '')
        has_record_btn = bool(meta.get('has_record_btn'))
        prompt_hint = f"{meta.get('direction') or ''} {q.text or ''}"

        # 1. 若有文字作答框（textarea 或 contenteditable），先填入作答内容
        text_filled = False
        if q.inputs:
            for inp in q.inputs:
                try:
                    tag = (inp.tag_name or '').lower()
                except Exception:
                    tag = ''
                if tag in ('textarea', 'input'):
                    if self._fill_text_input_verified(inp, clean_answer):
                        text_filled = True
                else:
                    try:
                        self.driver.execute_script("""
                            const el = arguments[0], val = arguments[1];
                            el.focus();
                            el.innerText = val;
                            el.dispatchEvent(new InputEvent('input', {bubbles: true, data: val}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        """, inp, clean_answer)
                        text_filled = True
                    except Exception:
                        pass

        # 2. 若有在线录音按钮且无文件上传控件，复用新版录音流程（自动联动「我的音色」）
        if not file_inputs and has_record_btn:
            rec_q = Question(
                number=q.number, text=q.text, q_type=QuestionType.RECORD,
                element=q.element, group=q.group,
                record_items=[{'url': '', 'text': clean_answer, 'sample': None}],
            )
            return self._answer_record(rec_q, clean_answer)

        # 3. 若有文件上传控件，自动生成对应类型附件（音频优先走「我的音色」）并上传
        if file_inputs:
            before_files = self._upload_file_count(q.group or q.element)
            if before_files > 0:
                print(f"\t检测到已有 {before_files} 个已上传附件，保留现有附件")
                return True
            from unipus_helper.upload_builder import build_upload_attachment
            file_path = None
            try:
                kind, file_path = build_upload_attachment(
                    clean_answer,
                    accept=accept,
                    prompt_text=prompt_hint,
                    title=f"Response_{q.number}",
                    tts_engine=self._tts(),
                )
                print(f"\t已自动生成 {kind.upper()} 附件准备上传: {file_path.name} ({file_path.stat().st_size:,} 字节)")
                # TTS/附件生成耗时数秒到二十余秒，期间 React 可能重渲染导致旧 file_input 失效（stale），
                # 上传前按「当前 group -> 当前 element -> 页面内 multi_media_upload_group」重新定位最新节点。
                def _fresh_upload_target():
                    roots = []
                    for r in (q.group, q.element):
                        if r is not None:
                            roots.append(r)
                    for r in roots:
                        try:
                            found = r.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
                            if found:
                                return found[0], r
                            # 向上查一层 question-block
                            block = self.driver.execute_script(
                                "return arguments[0].closest('[data-lexical-node-type=\"question-block\"]');", r)
                            if block is not None:
                                found = block.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
                                if found:
                                    return found[0], block
                        except Exception:
                            continue
                    # 兜底：若原 group/element 均已 stale，在当前内容层重新查找 multi_media_upload_group
                    try:
                        PublishFrameContext(self.driver).enter()
                        for sel in (
                            '[data-ipublish-question-group-type="multi_media_upload_group"] input[type="file"]',
                            '[data-lexical-node-type="question-block"] input[type="file"]',
                            'input[type="file"]',
                        ):
                            found = self.driver.find_elements(By.CSS_SELECTOR, sel)
                            if found:
                                inp = found[0]
                                grp = self.driver.execute_script(
                                    "return arguments[0].closest('[data-ipublish-question-group-type], [data-lexical-node-type=\"question-block\"]') || arguments[0].parentElement;",
                                    inp)
                                return inp, grp
                    except Exception:
                        pass
                    return file_inputs[0], (q.group or q.element)

                target_input, upload_root = _fresh_upload_target()
                resolved_path = str(file_path.resolve())
                try:
                    target_input.send_keys(resolved_path)
                except Exception:
                    # 某些隐藏 input[type=file] 需要短暂解除 display:none，或在重试前再重取一次防 stale
                    target_input, upload_root = _fresh_upload_target()
                    self.driver.execute_script(
                        "arguments[0].style.display='block'; arguments[0].style.visibility='visible';",
                        target_input)
                    target_input.send_keys(resolved_path)

                if self._wait_upload_complete(upload_root, before_files, timeout=30.0):
                    print(f"\t{kind.upper()} 附件上传完成")
                    return True
                print("\t等待附件上传完成超时")
                return text_filled
            except Exception as exc:
                print(f"\t多媒体附件生成或上传失败: {str(exc)[:100]}")
                return text_filled
            finally:
                if file_path is not None:
                    try:
                        if file_path.exists():
                            file_path.unlink()
                    except OSError:
                        pass

        return text_filled

    def _upload_file_count(self, root) -> int:
        try:
            return int(self.driver.execute_script("""
                const root = arguments[0];
                if (!root) return 0;
                const items = root.querySelectorAll(
                    '.ant-upload-list-item-done, .ant-upload-list-item:not(.ant-upload-list-item-uploading):not(.ant-upload-list-item-error), ' +
                    '[class*="upload-item"], [class*="file-item"], [class*="fileItem"], [class*="media-item"], ' +
                    'audio[src]:not([class*="sample"]), video[src]'
                );
                let count = 0;
                for (const el of items) {
                    if (el.closest('[class*="sample-audio"], [data-lexical-node-type="insert-audio"], [data-lexical-node-type="insert-video"]')) continue;
                    count++;
                }
                return count;
            """, root) or 0)
        except Exception:
            return 0

    def _wait_upload_complete(self, root, before_count: int, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        saw_activity = False
        while time.monotonic() < deadline and not self.stop_requested.is_set():
            try:
                state = self.driver.execute_script("""
                    const root = arguments[0];
                    if (!root) return {count: 0, loading: false, hasDel: false};
                    const loading = !!root.querySelector('.ant-upload-list-item-uploading, .ant-spin-spinning, [class*="uploading"]');
                    const hasDel = !!root.querySelector('[class*="delete"], [aria-label="delete"], .anticon-delete, .ant-upload-list-item-actions');
                    const files = root.querySelectorAll(
                        '.ant-upload-list-item-done, .ant-upload-list-item:not(.ant-upload-list-item-uploading):not(.ant-upload-list-item-error), ' +
                        '[class*="upload-item"], [class*="file-item"], [class*="fileItem"], [class*="media-item"], ' +
                        'audio[src], video[src]'
                    );
                    let count = 0;
                    for (const el of files) {
                        if (el.closest('[class*="sample-audio"], [data-lexical-node-type="insert-audio"], [data-lexical-node-type="insert-video"]')) continue;
                        count++;
                    }
                    return {count, loading, hasDel};
                """, root) or {}
                if state.get('loading'):
                    saw_activity = True
                if not state.get('loading') and (
                    int(state.get('count') or 0) > before_count or (saw_activity and state.get('hasDel'))
                ):
                    return True
            except Exception:
                pass
            if self.stop_requested.wait(0.5):
                return False
        return self._upload_file_count(root) > before_count

    def _fill_fill_in(self, q: Question, answer: str) -> bool:
        self._last_bank_mismatch = None
        if q.blank_count and q.blank_count != len(q.inputs):
            print("\t空位与输入框数量不一致，不填写该题")
            return False
        answers = self._parse_banked_answer(answer, len(q.inputs))
        if q.strict_word_bank:
            bank = list(q.banked_options or [])
            def _clean_w(s: str) -> str:
                return re.sub(r'^[\s\W_]+|[\s\W_]+$', '', str(s or '')).strip()
            def _stem(s: str) -> str:
                w = _clean_w(s).casefold()
                if len(w) <= 3:
                    return w
                w = re.sub(r'(?:ies|ied)$', 'y', w)
                w = re.sub(r'(?:ing|ed|es|s)$', '', w)
                return w

            unmatched_indices = []
            for index, value in enumerate(answers):
                if value in bank:
                    continue
                cleaned = _clean_w(value)
                matches = [w for w in bank if _clean_w(w).casefold() == cleaned.casefold()]
                if len(matches) == 1:
                    answers[index] = matches[0]
                    continue
                # 词形/词干唯一匹配（如 flourish -> flourishing / flourishes）
                v_stem = _stem(cleaned)
                stem_matches = [w for w in bank if v_stem and _stem(w) == v_stem]
                if len(stem_matches) == 1:
                    answers[index] = stem_matches[0]
                    print(f"\t空{index + 1}: 词形自动对齐词库 '{value}' -> '{stem_matches[0]}'")
                    continue
                unmatched_indices.append(index)

            # 若词库大小等于空位数且仅 1 空未匹配、其余 N-1 空已唯一占用 N-1 个不同词，直接锁定唯一剩余词
            if len(unmatched_indices) == 1 and len(bank) == len(answers):
                used = {answers[i] for i in range(len(answers)) if i != unmatched_indices[0] and answers[i] in bank}
                unused = [w for w in bank if w not in used]
                if len(unused) == 1:
                    idx = unmatched_indices[0]
                    print(f"\t空{idx + 1}: 词库唯一剩余项自动对齐 '{answers[idx]}' -> '{unused[0]}'")
                    answers[idx] = unused[0]
                    unmatched_indices.clear()

            if unmatched_indices:
                idx = unmatched_indices[0]
                self._last_bank_mismatch = {
                    'indices': [i + 1 for i in unmatched_indices],
                    'values': [answers[i] for i in unmatched_indices],
                    'bank': bank,
                }
                print(f"\t空{idx + 1}: 答案 {answers[idx]!r} 无法对应唯一词库选项（词库: {bank}），准备纠错重选")
                return False
        return self._fill_required_inputs(q.inputs, answers)

    def _fill_text(self, q: Question, answer: str) -> bool:
        if len(q.inputs) <= 1:
            return super()._fill_text(q, answer)
        answers = self._parse_banked_answer(answer, len(q.inputs))
        return self._fill_required_inputs(q.inputs, [_strip_markdown_text(value) for value in answers])

    # ---- 连线匹配 -------------------------------------------------------
    # 画布是 React Flow：把手势模拟成真实鼠标才连得上。两个坑都踩过：
    #   1. 不能用 ActionChains / CDP 的绝对坐标——阅读器 iframe 上方压着播放器外壳的
    #      score-column 浮层，真实鼠标事件会被顶层文档接走，iframe 里一条都收不到；
    #   2. 必须派发 MouseEvent 而不是 PointerEvent——把手的 React props 只挂了
    #      onMouseDown / onTouchStart / onClick，没有 pointerdown。
    # 所以这里在内容层文档里直接派发合成 MouseEvent，完全不碰坐标和命中测试。
    LINK_STUB_JS = """
    if (!window.__unipusLinkStub) {
      window.__unipusLinkStub = true;
      const noop = function () { return false; };
      ['setPointerCapture', 'releasePointerCapture'].forEach((name) => {
        try { Element.prototype[name] = noop; } catch (e) {}
      });
      try { Element.prototype.hasPointerCapture = function () { return true; }; } catch (e) {}
    }
    return true;
    """

    LINK_ONCE_JS = """
    const group = arguments[0];
    const sourceId = arguments[1];
    const targetId = arguments[2];
    const steps = 12;
    const nodeOf = (id) => group.querySelector('.react-flow__node[data-id="' + id + '"]');
    const sourceNode = nodeOf(sourceId);
    const targetNode = nodeOf(targetId);
    if (!sourceNode || !targetNode) return 'no-node';
    const sourceHandle = sourceNode.querySelector('.react-flow__handle');
    const targetHandle = targetNode.querySelector('.react-flow__handle');
    if (!sourceHandle || !targetHandle) return 'no-handle';
    const centerOf = (el) => {
      const box = el.getBoundingClientRect();
      return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
    };
    const from = centerOf(sourceHandle);
    const to = centerOf(targetHandle);
    const fire = (el, type, point, buttons) => {
      el.dispatchEvent(new MouseEvent(type, {
        bubbles: true, cancelable: true, composed: true,
        clientX: point.x, clientY: point.y,
        button: 0, buttons: buttons, view: window,
      }));
    };
    fire(sourceHandle, 'mousedown', from, 1);
    for (let step = 1; step <= steps; step++) {
      fire(sourceHandle, 'mousemove', {
        x: from.x + (to.x - from.x) * step / steps,
        y: from.y + (to.y - from.y) * step / steps,
      }, 1);
    }
    fire(targetHandle, 'mousemove', to, 1);
    fire(targetHandle, 'mouseup', to, 0);
    return 'ok';
    """

    LINK_EDGES_JS = "return arguments[0].querySelectorAll('.react-flow__edge').length;"

    def _link_stub(self) -> None:
        try:
            self.driver.execute_script(self.LINK_STUB_JS)
        except Exception:
            pass

    def _link_edge_count(self, group) -> int:
        try:
            return int(self.driver.execute_script(self.LINK_EDGES_JS, group) or 0)
        except Exception:
            return 0

    @staticmethod
    def _parse_link_answer(answer: str, prompt_count: int,
                           option_count: int) -> List[Tuple[int, str]]:
        """把「1.D 2.C 3.A」解析成 [(0, 'D'), (1, 'C'), (2, 'A')]；非法项直接丢弃。"""
        pairs: List[Tuple[int, str]] = []
        seen = set()
        for number, letter in re.findall(r'(\d+)\s*[.\-:：)）]?\s*([A-Za-z])', answer or ''):
            index = int(number) - 1
            position = ord(letter.upper()) - 65
            if not (0 <= index < prompt_count and 0 <= position < option_count):
                continue
            if index in seen:
                continue
            seen.add(index)
            pairs.append((index, letter.upper()))
        return pairs

    def _fill_link_match(self, q: Question, answer: str) -> bool:
        """按 AI 给出的配对逐条连线；画布上连线数与提示数一致才算成功。"""
        group = q.group or q.element
        prompts = [item for item in q.link_items if item.get('side') == 'right']
        options = [item for item in q.link_items if item.get('side') == 'left']
        if not prompts or not options:
            print("\t连线匹配：画布节点不完整，跳过")
            return False
        letter_to_item = {chr(65 + index): item for index, item in enumerate(options)}
        pairs = self._parse_link_answer(answer, len(prompts), len(options))
        if not pairs:
            print("\t连线匹配：未能从回答里解析出配对，跳过")
            return False
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", group)
        except Exception:
            pass
        self._link_stub()
        before = self._link_edge_count(group)
        linked = 0
        for index, letter in pairs:
            source = prompts[index]
            target = letter_to_item.get(letter)
            if not target:
                continue
            try:
                result = self.driver.execute_script(
                    self.LINK_ONCE_JS, group, source.get('id'), target.get('id'))
            except Exception as exc:
                print(f"\t连线匹配：{index + 1}→{letter} 异常 {type(exc).__name__}")
                continue
            time.sleep(0.35)
            now = self._link_edge_count(group)
            if result == 'ok' and now > before:
                linked += 1
                before = now
            else:
                print(f"\t连线匹配：{index + 1}→{letter} 未生成连线（{result}）")
        after = self._link_edge_count(group)
        print(f"\t连线匹配：连上 {linked} 组，画布共 {after} 条连线（提示 {len(prompts)} 条）")
        return after >= len(prompts)

    def _fill_required_inputs(self, inputs: List[Any], answers: List[str]) -> bool:
        """整题的所有空位均有答案且实际写入成功，才能交给上层提交。"""
        if not inputs or len(answers) != len(inputs) or not all(answers):
            print("\t答案不完整，不填写该题")
            return False
        for index, (inp, answer) in enumerate(zip(inputs, answers), 1):
            print(f"\t空{index}: {answer[:80]}")
            if not self._fill_text_input_verified(inp, answer):
                print(f"\t空{index}: 写入后校验失败")
                return False
        return True

    def _fill_text_input_verified(self, inp, ans: str) -> bool:
        """course-v3 填空必须用真实键盘输入才会计分。

        真机上直接 JS 赋值虽然能通过回读校验，但平台评分读到的是 0 分；
        所以键盘输入失败时重试一次，仍失败则阻止整组提交。
        """
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", inp)
            time.sleep(0.2)
        except Exception:
            pass

        for _ in range(2):
            if self._type_by_keyboard(inp, ans):
                return True
        print("\t真实键盘输入失败，不提交该题目组")
        return False

    def _type_by_keyboard(self, inp, ans: str) -> bool:
        """聚焦输入框，用真实按键清空后逐字输入。"""
        try:
            self.driver.execute_script("arguments[0].focus();", inp)
            time.sleep(0.2)
            inp.send_keys(Keys.CONTROL, 'a')
            inp.send_keys(Keys.DELETE)
            current = inp.get_attribute('value') or ''
            for _ in range(len(current)):
                inp.send_keys(Keys.BACKSPACE)
            inp.send_keys(ans)
            inp.send_keys(Keys.TAB)
            time.sleep(0.2)
            return self._input_value_matches(inp, ans)
        except Exception:
            return False

    # ---- 判断题 ---------------------------------------------------------
    @staticmethod
    def normalize_true_false(answer: str) -> str:
        """把 T/F、True/False、正确/错误 统一成 T 或 F。"""
        raw = (answer or '').strip()
        if '正确' in raw or raw in ('对', '是', '√'):
            return 'T'
        if '错误' in raw or raw in ('错', '否', '×', '不对'):
            return 'F'
        text = re.sub(r'^[\d\s.、):：\]]+', '', raw.upper())
        # 只认独立出现的 T/F，避免把 "IT IS CHEAP" 里的 T 当成答案
        match = re.search(r'(?<![A-Z])(TRUE|FALSE|T|F)(?![A-Z])', text)
        if not match:
            return ''
        return 'T' if match.group(1) in ('TRUE', 'T') else 'F'

    def _fill_true_false(self, question: Question, answer: str) -> bool:
        parts = re.split(r'[|｜]', answer or '', maxsplit=1)
        letter = self.normalize_true_false(parts[0])
        if not letter:
            print(f"\t判断题答案无法识别: {str(answer)[:40]!r}")
            return False
        for option in question.options:
            if option.letter.upper() == letter:
                print(f"\t选择 {option.letter}（{option.text}）")
                if not question.needs_correction:
                    return WebDriverHelper.safe_click(self.driver, option.element)
                if letter == 'F' and (len(parts) != 2 or not parts[1].strip()):
                    print("\t判断并改错题缺少改正内容")
                    return False
                if not self._toggle_choice_option_verified(option, True):
                    return False
                if letter == 'T':
                    return True
                try:
                    editors = WebDriverWait(self.driver, 3, poll_frequency=0.2).until(
                        lambda _driver: [el for el in question.element.find_elements(
                            By.CSS_SELECTOR, '[class*="shortAnswerWrap"] textarea, '
                            '[class*="shortAnswerWrap"] input:not([type]), '
                            '[class*="shortAnswerWrap"] input[type="text"], '
                            '[class*="shortAnswerWrap"] [contenteditable="true"], textarea, '
                            '[contenteditable="true"]'
                        ) if el.is_displayed() and el.is_enabled()
                        and not el.get_attribute('readonly')])
                except TimeoutException:
                    print("\t未找到可填写改正内容的输入框")
                    return False
                return len(editors) == 1 and self._fill_text_input_verified(editors[0], parts[1].strip())
        return False

    # ---- 下拉填空 -------------------------------------------------------
    def _fill_publish_dropdown(self, question: Question, answer: str) -> bool:
        blanks = self._dropdown_blanks(question)
        if not blanks:
            return self._fill_fill_in(question, answer)
        answers = self._parse_banked_answer(answer, len(blanks))
        if len(answers) != len(blanks) or not all(answers):
            print("\t下拉填空答案不完整，不填写该题")
            return False
        filled = 0
        for blank, value in zip(blanks, answers):
            if value and self._select_dropdown_value(blank, value):
                filled += 1
        if filled < len(blanks):
            print(f"\t下拉填空仅选中 {filled}/{len(blanks)} 个")
            return False
        return True

    def _dropdown_blanks(self, question: Question) -> List[Any]:
        try:
            return question.element.find_elements(
                By.CSS_SELECTOR, 'span[data-dig-empty-id]')
        except Exception:
            return []

    def _select_dropdown_value(self, blank, value: str) -> bool:
        """一个空位：可输入就直接写入，否则点开下拉弹层按文本选择。

        course-v3 的下拉空位是 .digEmptySelectView_selectTrigger，
        选项要点开触发器后才会渲染到弹层里。
        """
        trigger = self._dropdown_trigger(blank)
        if trigger is not None:
            if self._select_trigger_option(trigger, value):
                return True

        optional_inputs = []
        try:
            elements = blank.find_elements(By.CSS_SELECTOR, 'input, textarea, select')
        except Exception:
            elements = []
        for element in elements:
            try:
                tag = (element.tag_name or '').lower()
                readonly = (element.get_attribute('readonly') or '').strip().lower()
                disabled = (element.get_attribute('disabled') or '').strip().lower()
            except Exception:
                continue
            if tag == 'select':
                if self._select_native_option(element, value):
                    return True
                continue
            if readonly in ('true', 'readonly') or disabled == 'true':
                continue
            optional_inputs.append(element)

        for element in optional_inputs:
            if self._fill_text_input_verified(element, value):
                return True

        if not WebDriverHelper.safe_click(self.driver, trigger or blank):
            return False
        time.sleep(0.6)
        if self._click_dropdown_option(value):
            return True
        for element in optional_inputs:
            if self._fill_text_input_verified(element, value):
                return True
        try:
            ActionChains(self.driver).send_keys('\ue00c').perform()  # ESC 收起弹层
        except Exception:
            pass
        return False

    @staticmethod
    def _dropdown_trigger(blank):
        """course-v3 下拉空位的可点击触发器。"""
        selectors = (
            '[class*="selectTrigger"]',
            '[class*="digEmptySelectView"]',
            '[role="combobox"]',
            '[class*="select"]',
        )
        for css in selectors:
            try:
                candidates = blank.find_elements(By.CSS_SELECTOR, css)
            except Exception:
                continue
            for element in candidates:
                try:
                    if element.is_displayed():
                        return element
                except Exception:
                    continue
        return None

    DROPDOWN_POPUP_CSS = ('[class*="digEmptySelectView_dropdownContent"], '
                          '[class*="digEmptySelectView_dropdown"]')

    def _visible_dropdown_popup(self):
        """当前可见的下拉弹层容器。"""
        try:
            candidates = self.driver.find_elements(By.CSS_SELECTOR,
                                                   self.DROPDOWN_POPUP_CSS)
        except Exception:
            return None
        for element in candidates:
            try:
                rect = element.rect
                if element.is_displayed() and rect.get('height', 0) > 10:
                    return element
            except Exception:
                continue
        return None

    def _nearest_dropdown_trigger(self, popup):
        """离弹层最近的下拉触发器（用于点开关闭弹层）。"""
        try:
            popup_rect = popup.rect
            triggers = self.driver.find_elements(
                By.CSS_SELECTOR, '[class*="selectTrigger"], [role="combobox"]')
        except Exception:
            return None
        best = None
        best_distance = None
        for trigger in triggers:
            try:
                if not trigger.is_displayed():
                    continue
                rect = trigger.rect
                distance = (abs(rect['x'] - popup_rect['x'])
                            + abs(rect['y'] - popup_rect['y']))
            except Exception:
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = trigger, distance
        return best

    def _wait_dropdown_popup(self, timeout: float = 1.6):
        """轮询等待下拉弹层出现。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            popup = self._visible_dropdown_popup()
            if popup is not None:
                return popup
            time.sleep(0.12)
        return None

    def _dropdown_chevron(self, trigger):
        try:
            return trigger.find_element(By.CSS_SELECTOR,
                                        '[class*="close-icon"], [class*="icon"]')
        except Exception:
            return None

    def _js_mouse_click(self, element) -> None:
        """用 JS 派发完整鼠标事件序列（原生点击被遮挡时兜底）。"""
        self.driver.execute_script("""
        const el = arguments[0];
        ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function (type) {
            el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
        });
        """, element)

    def _close_visible_dropdowns(self, max_rounds: int = 4) -> None:
        """收起当前打开的下拉弹层。真机上 ESC 无效，只能再点一次触发器。"""
        for round_index in range(max_rounds):
            popup = self._visible_dropdown_popup()
            if popup is None:
                return
            trigger = self._nearest_dropdown_trigger(popup)
            if trigger is None:
                return
            if DEBUG_MODE:
                try:
                    text = PublishPlayer.button_text(trigger)
                except Exception:
                    text = ''
                print(f" [dd] 关闭弹层 第{round_index + 1}轮: trigger={text!r} "
                      f"h={popup.rect.get('height')}")
            try:
                ActionChains(self.driver).move_to_element(trigger).pause(0.15).click().perform()
            except Exception:
                WebDriverHelper.safe_click(self.driver, trigger)
            time.sleep(0.55)
            if self._visible_dropdown_popup() is None:
                return
            # ActionChains 被遮挡时用 JS 事件再切换一次
            try:
                self._js_mouse_click(trigger)
            except Exception:
                return
            time.sleep(0.55)

    def _open_dropdown(self, trigger) -> bool:
        """点开下拉弹层：多种点击方式轮试，每次都验证弹层是否真的出现。"""
        self._close_visible_dropdowns()
        chevron = self._dropdown_chevron(trigger)
        targets = [trigger]
        if chevron is not None:
            targets.append(chevron)
        for target in targets:
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
                    target)
                time.sleep(0.2)
            except Exception:
                pass
            strategies = (
                ('actionchains', lambda t=target: ActionChains(self.driver)
                    .move_to_element(t).pause(0.2).click().perform()),
                ('native', lambda t=target: t.click()),
                ('js-events', lambda t=target: self._js_mouse_click(t)),
            )
            for name, action in strategies:
                try:
                    action()
                except Exception as exc:
                    if DEBUG_MODE:
                        print(f" [dd] {name} 点击失败: {str(exc).splitlines()[0][:60]}")
                    continue
                if self._wait_dropdown_popup(1.3) is not None:
                    if DEBUG_MODE:
                        print(f" [dd] 弹层已打开（{name}）")
                    return True
        if DEBUG_MODE:
            print(" [dd] 所有打开方式都没能打开弹层")
        return False

    def _dropdown_options_from_popup(self) -> List[str]:
        popup = self._visible_dropdown_popup()
        if popup is None:
            return []
        options = []
        try:
            items = popup.find_elements(By.CSS_SELECTOR, '[class*="optionItem"]')
        except Exception:
            items = []
        for item in items:
            try:
                if not item.is_displayed():
                    continue
                text = re.sub(r'\s+', ' ', item.text or '').strip()
            except Exception:
                continue
            if text and 2 < len(text) <= 160 and text not in options:
                options.append(text)
        return options

    def collect_dropdown_options(self, blank) -> List[str]:
        """点开一个下拉空位，抓取弹层容器里的候选选项。

        真机确认：弹层是 body 下的 .digEmptySelectView_dropdownContent，
        每个空位给出的是同一组候选；ESC 收不起来，只能再点触发器。
        """
        trigger = self._dropdown_trigger(blank)
        if trigger is None:
            if DEBUG_MODE:
                print(" [dd] 没找到下拉触发器")
            return []
        popup = self._visible_dropdown_popup()
        if popup is None:
            if not self._open_dropdown(trigger):
                return []
            popup = self._visible_dropdown_popup()
        options = self._dropdown_options_from_popup() if popup is not None else []
        if DEBUG_MODE:
            print(f" [dd] 候选数量: {len(options)}")
        self._close_visible_dropdowns()
        return options[:20]

    def _select_trigger_option(self, trigger, value: str) -> bool:
        """点开下拉触发器，在弹层里选择与答案文本匹配的选项，并校验回显。"""
        target = re.sub(r'\s+', '', value or '').lower()
        if not target:
            return False
        try:
            current = re.sub(r'\s+', '', PublishPlayer.button_text(trigger)).lower()
        except Exception:
            current = ''
        if current == target:
            return True

        if not self._open_dropdown(trigger):
            return False
        popup = self._visible_dropdown_popup()
        if popup is None:
            return False
        items = []
        try:
            items = popup.find_elements(By.CSS_SELECTOR, '[class*="optionItem"]')
        except Exception:
            items = []

        match = None
        for item in items:
            try:
                if not item.is_displayed():
                    continue
                text = re.sub(r'\s+', '', PublishPlayer.button_text(item)).lower()
            except Exception:
                continue
            if not text:
                continue
            if text == target:
                match = item
                break
        chosen = match
        if chosen is None:
            self._close_visible_dropdowns()
            return False

        clicked = False
        try:
            ActionChains(self.driver).move_to_element(chosen).pause(0.15).click().perform()
            clicked = True
        except Exception:
            clicked = WebDriverHelper.safe_click(self.driver, chosen)
        time.sleep(0.5)
        try:
            updated = re.sub(r'\s+', '', PublishPlayer.button_text(trigger)).lower()
        except Exception:
            updated = ''
        if not clicked or updated != target:
            # 选项点击被遮挡时用 JS 事件兜底
            try:
                self._js_mouse_click(chosen)
                clicked = True
            except Exception:
                pass
            time.sleep(0.5)
            try:
                updated = re.sub(r'\s+', '', PublishPlayer.button_text(trigger)).lower()
            except Exception:
                updated = ''
        self._close_visible_dropdowns()
        if not clicked:
            return False
        return updated == target

    @staticmethod
    def _select_native_option(select, value: str) -> bool:
        target = re.sub(r'\s+', '', value or '').lower()
        if not target:
            return False
        try:
            options = select.find_elements(By.TAG_NAME, 'option')
        except Exception:
            return False
        for option in options:
            text = re.sub(r'\s+', '', PublishPlayer.button_text(option)).lower()
            if not text:
                continue
            if text == target:
                try:
                    option.click()
                    return True
                except Exception:
                    continue
        return False

    def _click_dropdown_option(self, value: str) -> bool:
        target = re.sub(r'\s+', '', (value or '')).lower()
        if not target:
            return False
        selectors = (
            '[role="option"]',
            '.ant-select-item-option',
            '[class*="option"]',
            '[class*="OptionItem"]',
            'li',
        )
        for css in selectors:
            try:
                candidates = self.driver.find_elements(By.CSS_SELECTOR, css)
            except Exception:
                continue
            for element in candidates[:300]:
                try:
                    if not element.is_displayed():
                        continue
                    text = re.sub(r'\s+', '', PublishPlayer.button_text(element)).lower()
                except Exception:
                    continue
                if text == target:
                    if WebDriverHelper.safe_click(self.driver, element):
                        return True
        return False

    # ---- 组内提交 -------------------------------------------------------
    def find_submit_button(self, group):
        if group is None:
            return None
        try:
            buttons = group.find_elements(By.CSS_SELECTOR, PublishPlayer.SUBMIT_CSS)
        except Exception:
            return None
        for button in buttons:
            try:
                if button.is_displayed():
                    return button
            except Exception:
                continue
        return buttons[0] if buttons else None

    @staticmethod
    def button_is_submitted(button) -> bool:
        text = PublishPlayer.button_text(button)
        return any(keyword in text for keyword in PublishPlayer.SUBMITTED_KEYWORDS)

    @staticmethod
    def button_allows_action(button, labels) -> bool:
        return (button is not None and button.is_displayed() and button.is_enabled()
                and button.get_attribute('aria-disabled') != 'true'
                and PublishPlayer.button_text(button) in labels)

    REJECTION_HINTS = (
        '正确率未达到', '正确率不足', '正确率未达', '未达到60%', '未达到 60%',
        '请上传', '请先上传', '至少上传', '不能为空', '字数不能', '请先完成', '请先作答',
        '请全部作答完成后再提交', '提交失败',
    )

    def _dismiss_publish_confirm_modal(self) -> str:
        """处理新版 ipublish 提交时可能弹出的二次确认框（确定/确认提交），严禁误触「单元学习规则」或「取消」。"""
        script = r"""
        const clean = t => (t || '').replace(/\s+/g, '').trim();
        const visible = el => {
            if (!el || !el.getClientRects || !el.getClientRects().length) return false;
            const st = getComputedStyle(el);
            return !!(st && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0');
        };
        const confirmTexts = new Set(['确定', '确认', '确认提交', '继续提交', '仍要提交', '提交', 'OK', 'Confirm']);
        const modals = document.querySelectorAll('.ant-modal-wrap, .ant-modal, [role="dialog"], .ipublish-modal, [class*="modal"]');
        for (const m of modals) {
            if (!visible(m)) continue;
            const bodyText = clean(m.innerText || m.textContent);
            if (!bodyText || bodyText.includes('单元学习规则')) continue;
            const btns = m.querySelectorAll('button, [role="button"], a.btn, [class*="btn"]');
            for (const b of btns) {
                if (!visible(b) || b.disabled) continue;
                const cls = String(b.className || '').toLowerCase();
                if (cls.includes('disabled')) continue;
                const t = clean(b.innerText || b.textContent);
                if (confirmTexts.has(t) || cls.includes('ant-btn-primary')) {
                    if (t === '取消' || t === '关闭' || t === '返回') continue;
                    b.click();
                    return t || 'primary';
                }
            }
        }
        return '';
        """
        hit = ''
        try:
            hit = self.driver.execute_script(script) or ''
        except Exception:
            hit = ''
        if not hit:
            in_content = False
            try:
                in_content = self.driver.execute_script('return window !== window.top;') is True
                PublishPlayer.reset(self.driver)
                hit = self.driver.execute_script(script) or ''
            except Exception:
                hit = ''
            finally:
                if in_content:
                    PublishFrameContext(self.driver).enter()
        if hit:
            print(f"\t已确认新版提交弹窗: {hit}")
        return hit

    def _publish_submit_diagnostic(self, current) -> str:
        """新版提交超时时的诊断摘要：记录组内按钮文本与可见 toast/弹窗提示。"""
        parts = []
        try:
            btn = self.find_submit_button(current)
            parts.append(f"按钮={PublishPlayer.button_text(btn)!r}" if btn is not None else "按钮=None")
        except Exception:
            pass
        toast_js = r"""
        const out = [];
        for (const el of document.querySelectorAll('.ant-message, .ant-notification, .ant-alert, [role="alert"], [class*="toast"], .ant-modal-body')) {
            if (!el.getClientRects || !el.getClientRects().length) continue;
            const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
            if (t && !t.includes('单元学习规则') && !out.includes(t)) out.push(t.slice(0, 80));
            if (out.length >= 3) break;
        }
        return out;
        """
        toasts = []
        try:
            toasts = self.driver.execute_script(toast_js) or []
        except Exception:
            toasts = []
        if not toasts:
            in_content = False
            try:
                in_content = self.driver.execute_script('return window !== window.top;') is True
                PublishPlayer.reset(self.driver)
                toasts = self.driver.execute_script(toast_js) or []
            except Exception:
                toasts = []
            finally:
                if in_content:
                    PublishFrameContext(self.driver).enter()
        if toasts:
            parts.append(f"提示={toasts}")
        return '，'.join(parts)

    def rejection_message(self) -> str:
        """提交后被平台拒绝时的顶部提示（如「客观题正确率未达到60%」）。

        注意：只认否定句（未达到/不足），不能把「本单元学习规则」弹窗里的
        「客观题正确率60%」过关条件误判成拒绝。
        """
        script = """
        const hints = arguments[0];
        const out = [];
        const notices = '.ant-message, .ant-notification, .ant-alert, [role="alert"], ' +
            '[class*="toast"], [class*="Toast"], [class*="messageNotice"], ' +
            '[class*="i-message"], .ant-modal-body, [role="dialog"], .ipublish-modal';
        for (const el of document.querySelectorAll(notices)) {
            const t = (el.innerText || '').trim();
            if (!t || t.length > 250 || t.includes('单元学习规则')) continue;
            if (!hints.some(h => t.indexOf(h) >= 0)) continue;
            const s = getComputedStyle(el);
            if (s.visibility !== 'visible' || s.display === 'none' || Number(s.opacity) === 0) continue;
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) continue;
            out.push(t.replace(/\\s+/g, ' ').slice(0, 120));
            if (out.length >= 2) break;
        }
        return out;
        """
        messages = []
        try:
            messages = self.driver.execute_script(script, list(self.REJECTION_HINTS)) or []
        except Exception:
            messages = []
        if not messages:
            # 提示也可能挂在外壳层
            in_content = False
            try:
                in_content = self.driver.execute_script('return window !== window.top;') is True
                PublishPlayer.reset(self.driver)
                messages = self.driver.execute_script(script, list(self.REJECTION_HINTS)) or []
            except Exception:
                messages = []
            finally:
                if in_content:
                    PublishFrameContext(self.driver).enter()
        return ' | '.join(messages)

    def submit(self, group=None, timeout: float = 20.0, resolver=None) -> bool:
        """提交一个 course-v3 题目组，并等待进入已批改状态。

        平台在客观题正确率不足 60% 时会拒绝提交并弹 toast，
        这种情况记录到 self.last_rejection，由上层决定是否继续。
        resolver: 可选的「按 key 重新定位题目组」回调；批改后 DOM 会重渲染，
        提交前的元素引用会失效，需要用新元素判断按钮是否变成「重新作答」。
        """
        self.last_rejection = ''
        if group is None:
            return False
        button = self.find_submit_button(group)
        if not self.button_allows_action(button, PublishPlayer.SUBMIT_ACTIONS):
            print("\t当前题目组没有可用的提交按钮")
            return False
        print("\t点击「提交」...")
        if not WebDriverHelper.safe_click(self.driver, button):
            print("\t点击提交按钮失败")
            return False
        return self.wait_graded(group, timeout, resolver)

    def wait_graded(self, group, timeout: float = 20.0, resolver=None) -> bool:
        """等待当前题组的提交回执。"""
        deadline = time.time() + timeout
        current = group
        while time.time() < deadline:
            if self.stop_requested.is_set():
                return False
            self._dismiss_publish_confirm_modal()
            if resolver is not None:
                try:
                    # 批改可能重载 iframe；resolver 必须始终在内容层执行。
                    PublishFrameContext(self.driver).enter()
                    fresh = resolver()
                    if fresh is not None:
                        current = fresh
                except Exception:
                    pass
            rejection = self.rejection_message()
            if rejection:
                self.last_rejection = rejection
                print(f"\t平台拒绝提交：{rejection}")
                return False
            try:
                button = self.find_submit_button(current)
                if PublishPlayer.group_has_submission_receipt(current, button):
                    return True
            except StaleElementReferenceException:
                current = group
            except Exception:
                pass
            if self.stop_requested.wait(0.5):
                return False
        diag = self._publish_submit_diagnostic(current)
        extra = f"（{diag}）" if diag else ""
        print(f"\t提交后未检测到已批改状态，请人工确认{extra}")
        return False

    # ---- 外壳层弹窗 -----------------------------------------------------
    def dismiss_rules_popup(self, max_candidates: int = 400) -> bool:
        """关闭「本单元学习规则」弹窗：挂在外壳层，且只认真实鼠标点击。"""
        PublishPlayer.reset(self.driver)
        # 一次浏览器端查询定位规则弹窗及按钮，避免全页逐元素往返 WebDriver。
        script = r"""
        const labels = arguments[0], limit = arguments[1];
        const visible = el => {
            const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden';
        };
        const textOf = el => (el.innerText || '').replace(/\s+/g, '');
        const selectors = ['.ipublish-modal', '[role="dialog"]', '[class*="modal"], [class*="dialog"]'];
        for (const selector of selectors) {
            for (const modal of [...document.querySelectorAll(selector)].slice(0, limit)) {
                if (!visible(modal) || !textOf(modal).includes('单元学习规则')) continue;
                if (arguments[2]) return true;
                for (const css of ['button, [role="button"], [class*="btn"]', 'a, span, div']) {
                    const buttons = [...modal.querySelectorAll(css)].slice(0, limit);
                    for (const button of buttons) {
                        if (visible(button) && !button.disabled && labels.includes(textOf(button))) {
                            return button;
                        }
                    }
                }
            }
        }
        return arguments[2] ? false : null;
        """
        try:
            button = self.driver.execute_script(script, list(self.RULE_POPUP_TEXTS),
                                                max_candidates, False)
            if button is None:
                return False
        except Exception:
            return False
        try:
            ActionChains(self.driver).move_to_element(button).click().perform()
        except Exception:
            if not WebDriverHelper.safe_click(self.driver, button):
                return False
        try:
            WebDriverWait(self.driver, 2, poll_frequency=0.1).until(
                lambda driver: not driver.execute_script(
                    script, list(self.RULE_POPUP_TEXTS), max_candidates, True))
        except Exception:
            return False
        print("    已关闭「本单元学习规则」弹窗")
        return True


class ContentHandler(ABC):
    """内容处理器基类"""

    @abstractmethod
    def can_handle(self, question: Question) -> bool:
        pass

    @abstractmethod
    def handle(self, question: Question) -> bool:
        pass


class DiscussionBoardHandler(ContentHandler):
    """讨论板处理器：读题 → 生成一段观点 → 发帖。"""

    def __init__(self, driver, ai_client=None, stop_requested: Optional[threading.Event] = None):
        self.driver = driver
        self.ai_client = ai_client
        self.stop_requested = stop_requested or threading.Event()

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.DISCUSSION_BOARD

    def handle(self, question: Question) -> bool:
        textarea = self._find_textarea()
        if not textarea:
            print("     讨论板未找到输入框，跳过")
            return True

        topic = self._topic_text()
        print(f"     讨论板发帖：{topic[:60]}")

        body = self._compose(topic)
        if not body:
            print("     讨论板内容生成失败，跳过")
            return True

        try:
            textarea.click()
            textarea.clear()
        except Exception:
            pass
        filled = False
        try:
            self.driver.execute_script("""
                const el = arguments[0], value = arguments[1];
                el.focus();
                const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
                if (desc && desc.set) { desc.set.call(el, value); } else { el.value = value; }
                el.dispatchEvent(new Event('input', {bubbles: true}));
                return el.value;
            """, textarea, body)
            filled = True
        except Exception as exc:
            print(f"     讨论板写入失败: {str(exc)[:50]}")
        if not filled:
            textarea.send_keys(body)

        # 触发 antd 的字符计数与提交按钮状态
        try:
            textarea.send_keys(Keys.SPACE)
            textarea.send_keys(Keys.BACKSPACE)
        except Exception:
            pass

        if self.stop_requested.wait(0.6):
            return False

        submit = self._find_submit()
        if not submit:
            print("     讨论板未找到发布按钮")
            return False
        clicked = WebDriverHelper.safe_click(self.driver, submit)
        if not clicked:
            try:
                self.driver.execute_script("""
                    const btn = arguments[0];
                    btn.scrollIntoView({block: 'center'});
                    btn.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                    btn.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                    btn.click();
                """, submit)
                clicked = True
            except Exception:
                clicked = False
        if not clicked:
            return False

        self.stop_requested.wait(2.5)
        if self._posted_successfully():
            print("     讨论板已发布")
            return True
        print("     讨论板发布后未检测到评论，可能被限流或需要人机验证")
        return False

    def _posted_successfully(self) -> bool:
        try:
            return bool(self.driver.execute_script("""
                const area = document.querySelector('.ds-discussion-bottom textarea, textarea[placeholder*="评论"]');
                if (area && (area.value || '').trim().length === 0) return true;
                const text = document.body.innerText || '';
                return /全部评论（[1-9]/.test(text);
            """))
        except Exception:
            return False

    def _find_textarea(self):
        for selector in ('.ds-discussion-bottom textarea',
                         'textarea[placeholder*="评论"]',
                         '.ds-discussion-reply textarea'):
            try:
                for area in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    if area.is_displayed():
                        return area
            except Exception:
                continue
        return None

    def _find_submit(self):
        # 优先点真正的 <button>：外层 .btns-submit 只是个容器，点了不会发帖
        for selector in ('button.submit-btn',
                         '.btns-submit button',
                         '.ds-discussion-bottom-btns button',
                         '.ds-discussion-bottom button',
                         'button.ant-btn-default'):
            try:
                for btn in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    text = re.sub(r'\s+', '', btn.text or '')
                    if btn.is_displayed() and ('发布' in text or 'submit' in text.lower()):
                        return btn
            except Exception:
                continue
        return None

    def _topic_text(self) -> str:
        parts = []
        try:
            parts = self.driver.execute_script("""
                const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                const out = [];
                const dir = document.querySelector('.layout-direction-container');
                if (dir) out.push(clean(dir.innerText));
                // 真正的问题通常是一个独立的段落/富文本块
                for (const el of document.querySelectorAll('.component-htmlview, p, .question-common-abs-scoop')) {
                  if (el.closest('.ds-discussion-bottom, [class*="recordList"], [class*="comment"], .layout-sidebar')) continue;
                  const text = clean(el.innerText);
                  if (text.length < 15 || text.length > 400) continue;
                  if (text.includes('全部评论') || text.includes('我来评论')) continue;
                  if (out.includes(text)) continue;
                  out.push(text);
                  if (out.length >= 3) break;
                }
                return out;
            """) or []
        except Exception:
            parts = []
        text = "\n".join([p for p in parts if p])[:1200]
        if len(text) > 10:
            return text
        return ""

    def _compose(self, topic: str) -> str:
        if self.ai_client and topic:
            prompt = (f"【讨论题】{topic}\n\n"
                      "请用英文写一段 60-90 词、观点明确的讨论回复（3 个要点，可举例），"
                      "直接输出正文，不要标题、不要编号、不要解释。")
            try:
                answer = self.ai_client.ask(prompt, retry_count=2, stop_requested=self.stop_requested)
                if answer:
                    return re.sub(r'^[\s"“”]+|[\s"“”]+$', '', answer).strip()[:1200]
            except Exception as exc:
                print(f"     讨论板生成失败: {str(exc)[:50]}")
        return ""


class AudioHandler(ContentHandler):
    """完整播放纯听力任务，只有自然结束才算成功。"""

    def __init__(self, driver, stop_requested: threading.Event):
        self.driver = driver
        self.stop_requested = stop_requested

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.AUDIO

    def handle(self, question: Question) -> bool:
        audios = question.element.find_elements(By.TAG_NAME, 'audio')
        if not audios:
            return False
        for index, audio in enumerate(audios, 1):
            if self.stop_requested.is_set():
                return False
            print(f"     播放第 {index}/{len(audios)} 段音频，等待完整播放...")
            try:
                self.driver.execute_script("""
                    const audio = arguments[0];
                    audio.loop = false;
                    audio.playbackRate = 1;
                    audio.currentTime = 0;
                    audio.__ucampusPlayError = '';
                    const playing = audio.play();
                    if (playing && playing.catch) playing.catch(error => {
                        audio.__ucampusPlayError = String(error);
                    });
                """, audio)
                last_time = 0.0
                progress_at = time.monotonic()
                while not self.stop_requested.is_set():
                    state = self.driver.execute_script("""
                        const audio = arguments[0];
                        return {time: audio.currentTime || 0, ended: audio.ended,
                                error: !!audio.error || !!audio.__ucampusPlayError};
                    """, audio) or {}
                    current = float(state.get('time') or 0)
                    if state.get('error'):
                        print("     音频播放失败，保留任务以便重试")
                        return False
                    if state.get('ended') and current > 0:
                        print(f"     第 {index} 段音频已完整播放（{current:.1f}s）")
                        break
                    if current > last_time:
                        last_time, progress_at = current, time.monotonic()
                    if time.monotonic() - progress_at > 30:
                        print("     音频加载或播放停滞，保留任务以便重试")
                        return False
                    if self.stop_requested.wait(0.25):
                        return False
                else:
                    return False
            except Exception as exc:
                print(f"     音频播放失败: {str(exc)[:80]}")
                return False
            finally:
                try:
                    self.driver.execute_script('arguments[0].pause();', audio)
                except Exception:
                    pass
        return not self.stop_requested.is_set()


class SelfCheckHandler(ContentHandler):
    """Self-check 词汇勾选处理器"""

    def __init__(self, driver, stop_requested: threading.Event):
        self.driver = driver
        self.stop_requested = stop_requested

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.SELF_CHECK

    def handle(self, question: Question) -> bool:
        print("     处理 Self-check 词汇勾选表...")
        clicked = 0

        row_count = len(question.element.find_elements(By.CSS_SELECTOR, 'tbody tr.ant-table-row:not(.category-name)'))
        for row_index in range(row_count):
            if self.stop_requested.is_set():
                return False
            try:
                rows = question.element.find_elements(By.CSS_SELECTOR, 'tbody tr.ant-table-row:not(.category-name)')
                if row_index >= len(rows):
                    break
                row = rows[row_index]

                word = ""
                try:
                    word = row.find_element(By.CSS_SELECTOR, '.content-text').text.strip()
                except:
                    pass

                got_it_cell = row.find_element(By.CSS_SELECTOR, 'td:nth-child(2)')
                icon = got_it_cell.find_element(By.CSS_SELECTOR, '.anticon')
                icon_class = icon.get_attribute('class') or ''
                aria_label = (icon.get_attribute('aria-label') or '').lower()

                if 'anticon-border' not in icon_class and aria_label != 'border':
                    continue

                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", icon)
                if self.stop_requested.wait(0.1):
                    return False

                if WebDriverHelper.safe_click(self.driver, icon):
                    clicked += 1
                    print(f"      勾选: {word or clicked}")
                else:
                    self.driver.execute_script("""
                        arguments[0].click();
                        arguments[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    """, icon)
                    clicked += 1
                    print(f"      JS勾选: {word or clicked}")

                if self.stop_requested.wait(0.1):
                    return False
            except Exception as e:
                print(f"      勾选失败: {str(e)[:50]}")
                continue

        print(f"     Self-check 完成，共勾选 {clicked} 项")
        return True


class VideoHandler(ContentHandler):
    """视频处理器（旧版 ucontent 播放器使用）"""

    def __init__(self, driver, config: Config, stop_requested: threading.Event,
                 transcriber: Optional[AudioTranscriber] = None):
        self.driver = driver
        self.config = config
        self.stop_requested = stop_requested
        self.popup_monitor_thread = None
        self.stop_monitoring = threading.Event()

        self.transcriber = transcriber if transcriber is not None else AudioTranscriber()

        self.analyzer_client = make_model_client(config.base_url, config.api_key)

        self.video_transcript = ""
        self.current_video_url = ""
        self.skip_transcription = False
        self._transcript_lock = threading.Lock()

    def _play_video_and_handle_popups(self, on_transcript: Optional[Callable[[str], None]] = None):
        """播放视频并自动处理弹窗选择题（供外部预处理调用）"""
        if self.stop_requested.is_set():
            return
        video_info = self._get_video_info()
        if not video_info:
            print("      未找到视频元素")
            return

        video_url = (video_info.get('url') or '').split('#', 1)[0]
        duration = video_info.get('duration', 0)

        if video_url != self.current_video_url:
            self.current_video_url = video_url
            self.video_transcript = ""
        if self.video_transcript:
            print(f"      使用已缓存的视频转录（{len(self.video_transcript)}字符）")
        elif not self.skip_transcription:
            self.video_transcript = self._transcribe_video(video_url, duration)
        if self.stop_requested.is_set():
            return
        if on_transcript:
            try:
                on_transcript(self.video_transcript)
            except Exception as exc:
                print(f"      预取答案启动失败: {str(exc)[:60]}")
        self.stop_monitoring.clear()
        self.popup_monitor_thread = threading.Thread(
            target=self._monitor_popup_questions,
            daemon=True
        )
        self.popup_monitor_thread.start()

        try:
            self._play_video(duration)
            if not self.stop_requested.is_set():
                print("      视频播放完成")
        finally:
            self.stop_monitoring.set()
            if self.popup_monitor_thread.is_alive():
                self.popup_monitor_thread.join(timeout=1)

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.VIDEO

    def handle(self, question: Question) -> bool:
        if self._check_video_completed():
            print("     视频已标记为完成，跳过")
            return True

        print("     视频页面，开始处理...")
        self._play_video_and_handle_popups()
        print("     视频处理完成")
        return True

    def _get_video_info(self) -> Optional[Dict]:
        try:
            video = self.driver.find_element(By.TAG_NAME, 'video')
            url = video.get_attribute('src') or ''

            if not url:
                sources = video.find_elements(By.TAG_NAME, 'source')
                for source in sources:
                    url = source.get_attribute('src')
                    if url:
                        break

            duration = self.driver.execute_script("return arguments[0].duration;", video)

            return {
                'url': url,
                'duration': duration or 0,
                'element': video
            }
        except:
            return None

    def _transcribe_video(self, video_url: str, duration: float) -> str:
        if not video_url:
            return ""

        print(f"     开始识别视频音频（时长: {int(duration)}秒）...")

        try:
            transcript = self.transcriber.transcribe(video_url, language="en")

            if transcript:
                preview = transcript[:200] + "..." if len(transcript) > 200 else transcript
                print(f"     识别成功: {preview}")
                return transcript
            else:
                print("     未能识别音频内容")
                return ""

        except Exception as e:
            print(f"     音频识别失败: {str(e)[:50]}")
            return ""

    def ensure_transcript(self, video_url: str = "") -> str:
        url = video_url or self.current_video_url or (self._get_video_info() or {}).get('url', '')
        url = (url or '').split('#', 1)[0]
        if not url:
            return ""
        with self._transcript_lock:
            if self.current_video_url == url and self.video_transcript:
                return self.video_transcript
            print("     补做视频转写...")
            transcript = self._transcribe_video(url, 0)
            self.current_video_url, self.video_transcript = url, transcript
            return transcript

    def _play_video(self, duration: float):
        video = None
        try:
            video = self.driver.find_element(By.TAG_NAME, 'video')

            if duration > 0:
                print(f"      【播放】开始播放视频（{int(duration)} 秒，2 倍速）...")
                self.driver.execute_script("""
                    arguments[0].playbackRate = 2.0;
                    arguments[0].muted = true;
                    arguments[0].play();
                """, video)

                self._wait_for_video_complete(video, duration)
            else:
                self.driver.execute_script("""
                    arguments[0].playbackRate = 2.0;
                    arguments[0].muted = true;
                    arguments[0].play();
                """, video)
                print(f"      【等待】等待 10 秒...")
                self.stop_requested.wait(10)

        except Exception as e:
            print(f"       视频播放失败: {str(e)[:50]}")

        finally:
            if self.stop_requested.is_set() and video is not None:
                try:
                    self.driver.execute_script("arguments[0].pause();", video)
                except Exception:
                    pass

    def _monitor_popup_questions(self):
        print("      [监视器] 开始监视弹窗...")
        check_interval = 0.5
        processed_popups = set()

        while not self.stop_monitoring.is_set() and not self.stop_requested.is_set():
            try:
                popup = self._find_popup_question()

                if popup and popup.is_displayed():
                    popup_id = self._get_popup_id(popup)

                    if popup_id in processed_popups:
                        if self.stop_requested.wait(0.5):
                            break
                        continue

                    print("      [监视器]  检测到新弹窗题目！")
                    question_data = self._parse_popup_question(popup)

                    if not question_data:
                        print("      [监视器]  未能解析题目")
                        continue

                    if question_data['options'] and not self.video_transcript:
                        # 播放中才弹题时补转写，避免只能随机猜。
                        self.ensure_transcript()
                    if self.video_transcript and question_data['options']:
                        answer = self._intelligent_select_answer(question_data)
                    else:
                        answer = self._random_select(question_data)
                        print(f"      [监视器]  随机选择: {answer}")

                    if self.stop_requested.is_set() or self.stop_monitoring.is_set():
                        break
                    success = self._click_option(popup, answer)

                    if success:
                        print(f"      [监视器]  已选择: {answer}")
                        processed_popups.add(popup_id)
                        if self.stop_requested.wait(0.5) or self.stop_monitoring.is_set():
                            break
                        self._click_submit_if_exists(popup)
                        self.stop_requested.wait(1.0)
                    else:
                        print(f"      [监视器]  点击失败: {answer}")

            except Exception as e:
                pass

            self.stop_monitoring.wait(check_interval)

        print("      [监视器] 已停止")

    def _find_popup_question(self) -> Optional[Any]:
        selectors = [
            '.video-box .popupBox .questionReplyBox',
            '.popupBox .question-common-abs-choice',
            '.questionReplyBox .question-common-abs-choice',
            '.video-popup .question-common-abs-choice',
            '.popupBox:has(.option)',
        ]

        for selector in selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for elem in elements:
                    if elem.is_displayed():
                        options = elem.find_elements(By.CSS_SELECTOR, '.option, .option-wrap .option')
                        if len(options) >= 2:
                            return elem
            except:
                continue
        return None

    def _get_popup_id(self, popup) -> str:
        try:
            text = popup.text
            return hashlib.md5(text[:200].encode()).hexdigest()[:16]
        except:
            return str(time.time())

    def _parse_popup_question(self, popup) -> Optional[Dict]:
        try:
            title_selectors = ['.ques-title', '.question-title', '.title', '.question-stem']
            title = ""
            for sel in title_selectors:
                try:
                    elem = popup.find_element(By.CSS_SELECTOR, sel)
                    title = elem.text.strip()
                    if title:
                        break
                except:
                    continue

            option_elems = popup.find_elements(By.CSS_SELECTOR,
                                               '.option.isNotReview, .option-wrap .option, .choice-option')

            options = []
            for i, opt_elem in enumerate(option_elems):
                try:
                    letter_selectors = ['.caption', '.index', '.option-label', '.choice-label']
                    letter = ""
                    for sel in letter_selectors:
                        try:
                            letter_elem = opt_elem.find_element(By.CSS_SELECTOR, sel)
                            letter = letter_elem.text.strip().replace('.', '').replace(')', '').upper()
                            if letter:
                                break
                        except:
                            continue

                    if not letter:
                        letter = chr(65 + i)

                    content_selectors = ['.content', '.option-content', '.text', '.choice-text']
                    content = ""
                    for sel in content_selectors:
                        try:
                            content_elem = opt_elem.find_element(By.CSS_SELECTOR, sel)
                            content = content_elem.text.strip()
                            if content:
                                break
                        except:
                            continue

                    if not content:
                        content = opt_elem.text.strip()

                    options.append({
                        'letter': letter,
                        'text': content,
                        'element': opt_elem
                    })

                except:
                    continue

            if not options:
                return None

            return {
                'question': title,
                'options': options
            }

        except Exception as e:
            print(f"      [监视器] 解析失败: {str(e)[:50]}")
            return None

    def _intelligent_select_answer(self, question_data: Dict) -> str:
        question = question_data['question']
        options = question_data['options']

        print(f"      [监视器]  分析问题: {question[:50]}...")
        prompt = self._build_analysis_prompt(question, options)

        try:
            response = self.analyzer_client.chat.completions.create(
                model="kimi-k2-turbo-preview",
                messages=[
                    {
                        "role": "system",
                        "content": "你是视频理解助手。根据视频内容选择最正确的答案，只返回选项字母，不要解释。"
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=5
            )

            answer_text = response.choices[0].message.content.strip().upper()

            valid_letters = [opt['letter'] for opt in options]
            for letter in valid_letters:
                if letter in answer_text:
                    print(f"      [监视器]  AI选择: {letter}")
                    return letter

            return self._keyword_match(question, options)

        except Exception as e:
            print(f"      [监视器]  AI分析失败: {str(e)[:50]}，使用关键词匹配")
            return self._keyword_match(question, options)

    def _build_analysis_prompt(self, question: str, options: List[Dict]) -> str:
        transcript = self.video_transcript[:2000] if len(self.video_transcript) > 2000 else self.video_transcript

        prompt = f"""【视频内容】
        {transcript}

        【问题】
        {question}

        【选项】
        """
        for opt in options:
            prompt += f"{opt['letter']}. {opt['text']}\n"

        prompt += """
        【任务】
        根据视频内容，选择最正确的答案。只返回选项字母（如：A 或 B），不要任何解释。

        答案："""

        return prompt

    def _keyword_match(self, question: str, options: List[Dict]) -> str:
        transcript_lower = self.video_transcript.lower()
        question_lower = question.lower()

        best_option = None
        best_score = -1

        for opt in options:
            opt_text = opt['text'].lower()
            score = 0
            score += transcript_lower.count(opt_text) * 2

            keywords = [w for w in opt_text.split() if len(w) > 3]
            for kw in keywords:
                if kw in transcript_lower:
                    score += 1

            if any(word in question_lower for word in opt_text.split()[:3]):
                score += 3

            if score > best_score:
                best_score = score
                best_option = opt

        if best_option:
            print(f"      [监视器]  关键词匹配: {best_option['letter']} (得分: {best_score})")
            return best_option['letter']

        return options[0]['letter'] if options else "A"

    def _random_select(self, question_data: Dict) -> str:
        options = question_data.get('options', [])
        if not options:
            return "C"
        choice = random.choice(options)
        return choice['letter']

    def _click_option(self, popup, answer: str) -> bool:
        try:
            option_elems = popup.find_elements(By.CSS_SELECTOR,
                                               '.option.isNotReview, .option-wrap .option')

            for opt_elem in option_elems:
                try:
                    letter_selectors = ['.caption', '.index', '.option-label']
                    letter = ""
                    for sel in letter_selectors:
                        try:
                            letter_elem = opt_elem.find_element(By.CSS_SELECTOR, sel)
                            letter = letter_elem.text.strip().replace('.', '').replace(')', '').upper()
                            if letter:
                                break
                        except:
                            continue

                    if letter == answer.upper():
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                            opt_elem
                        )
                        time.sleep(0.2)

                        try:
                            opt_elem.click()
                        except:
                            self.driver.execute_script("arguments[0].click();", opt_elem)

                        return True

                except:
                    continue

            try:
                idx = ord(answer.upper()) - ord('A')
                if 0 <= idx < len(option_elems):
                    opt_elem = option_elems[idx]
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                        opt_elem
                    )
                    time.sleep(0.2)
                    opt_elem.click()
                    return True
            except:
                pass

            return False

        except Exception as e:
            print(f"      [监视器] 点击失败: {str(e)[:50]}")
            return False

    def _click_submit_if_exists(self, popup):
        submit_selectors = [
            '.submit-btn', '.confirm-btn', '.ok-btn',
            'button[type="submit"]', '.popup-submit',
            '.questionReplyBox .submit'
        ]

        for selector in submit_selectors:
            try:
                btn = popup.find_element(By.CSS_SELECTOR, selector)
                if btn.is_displayed():
                    btn.click()
                    print("      [监视器]  已提交")
                    return True
            except:
                continue
        return False

    def _wait_for_video_complete(self, video, duration: float):
        max_wait = duration / 2 + 30
        start_time = time.time()
        last_progress = 0

        while time.time() - start_time < max_wait:
            try:
                if self.stop_requested.is_set() or self.stop_monitoring.is_set():
                    break

                current = self.driver.execute_script("return arguments[0].currentTime;", video)
                ended = self.driver.execute_script("return arguments[0].ended;", video)

                if ended or current >= duration - 1:
                    print(f"       视频播放完成")
                    break

                elapsed = int(time.time() - start_time)
                if elapsed - last_progress >= 5:
                    print(f"      播放进度: {int(current)}/{int(duration)} 秒")
                    last_progress = elapsed

                if self.stop_requested.wait(0.5):
                    break

            except:
                break

    def _check_video_completed(self) -> bool:
        try:
            indicators = [
                '.video-completed', '.watched', '.finished',
                '[class*="completed"]', '[class*="finished"]'
            ]
            for indicator in indicators:
                if self.driver.find_elements(By.CSS_SELECTOR, indicator):
                    return True
            return False
        except:
            return False


class FlashcardHandler(ContentHandler):
    """单词闪卡处理器 """

    def __init__(self, driver, stop_requested: threading.Event):
        self.driver = driver
        self.stop_requested = stop_requested

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.VOCABULARY_FLASHCARD

    def handle(self, question: Question) -> bool:
        print("     处理单词闪卡...")
        max_cards = 100
        clicked = 0
        if self.stop_requested.wait(2):
            return False
        for i in range(max_cards):
            if self.stop_requested.is_set():
                return False
            try:
                next_btn = self._find_next_button()
                if not next_btn:
                    print(f"      未找到下一个按钮，可能已完成（已点击{clicked}个）")
                    break
                if not next_btn.is_displayed() or not next_btn.is_enabled():
                    print(f"      按钮不可用，完成")
                    break
                try:
                    disabled_next_button = self.driver.find_element(By.CSS_SELECTOR, '.action.next.disabled')
                    if disabled_next_button:
                        break
                except:
                    pass
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                    next_btn
                )
                if self.stop_requested.wait(0.5):
                    return False
                try:
                    next_btn.click()
                except:
                    self.driver.execute_script("arguments[0].click();", next_btn)
                clicked += 1
                if self.stop_requested.wait(0.5):
                    return False
                current_word = self.driver.find_element(By.XPATH,
                                                        '//*[@id="question-vocabulary-base-id"]/div/div[2]/div')
                print(f" 学习{current_word.text}")
            except Exception as e:
                error_msg = str(e)
                print(f"      处理闪卡失败: {error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                if self.stop_requested.wait(1):
                    return False
                continue

        print(f"     单词闪卡完成，共 {clicked} 个")
        return True

    def _find_next_button(self):
        selectors = [
            '.vocActions .next',
            '.action.next',
            '.next-btn',
            '.vocabulary-actions .next',
            'button.next',
            '.flashcard-next',
            '[class*="next"]:not([class*="disabled"])',
        ]

        for selector in selectors:
            try:
                elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for elem in elems:
                    if elem.is_displayed():
                        return elem
            except:
                continue
        return None


PAGE_SCORE_JS = r"""
    const parseScore = text => {
        const match = text.match(/^(\d{1,3}(?:\.\d+)?)\s*(?:\/\s*100)?\s*分?$/);
        return match && Number(match[1]) <= 100 ? Number(match[1]) : null;
    };
    const lines = (document.body?.innerText || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    const inSummary = lines.some(s => /^(答题小结|答题结果|成绩报告|测试结果)$/.test(s));
    const scores = new Set();
    for (let i = 0; i < lines.length; i++) {
        const label = lines[i].match(/^(本次|最终|实际)?\s*(?:得分|成绩|分数)\s*[:：]?\s*(.*)$/);
        if (!label || (!label[1] && !inSummary)) continue;
        const score = parseScore(label[2] || lines[i + 1] || '');
        if (score !== null) scores.add(score);
    }
    if (scores.size) return scores.size === 1 ? [...scores][0] : null;
    const rows = [...document.querySelectorAll('.score_layout')].filter(el =>
        el.getClientRects().length &&
        (!el.checkVisibility || el.checkVisibility({ checkVisibilityCSS: true, checkOpacity: true })));
    return rows.length === 1
        ? parseScore((rows[0].innerText || '').trim().replace(/^(?:得分|成绩)\s*[:：]?\s*/, ''))
        : null;
"""


class AnswerPrefetch:
    """后台预取 AI 答案；只做网络请求，不碰 Selenium。"""

    def __init__(self, client, prompt: str, screenshot: Optional[bytes],
                 stop_requested: threading.Event):
        self.client = client
        self.materials = tuple((m['role'], m['content']) for m in client.material_messages)
        self.prompt = prompt
        self.screenshot = screenshot
        self.stop_requested = stop_requested
        self.answer: Optional[str] = None
        self.error: Optional[Exception] = None
        self.started = time.monotonic()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self.answer = self.client.ask(
                self.prompt, stop_requested=self.stop_requested,
                screenshot=self.screenshot, remember=False)
        except Exception as exc:
            self.error = exc
        finally:
            self._done.set()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def wait(self, timeout: Optional[float] = None) -> Optional[str]:
        self._done.wait(timeout)
        return self.answer


class AISolver:
    """AI答题器 - 协调解析、构建、执行流程"""

    def __init__(self, driver, config: Config):
        self.driver = driver
        self.config = config
        self.stop_requested = threading.Event()
        self.ai_client = OpenAICompatibleClient(self.config)
        self.parser = QuestionParser(driver)
        # Vocabulary 页收来的中英对照：Exercise 1 的填空、Exercise 2 的口语稿都吃它
        self.vocab_store = VocabularyStore()
        self.voice_cloner = self._build_voice_cloner(config)
        self.tts_engine = TTSEngine(
            getattr(config, 'tts_engine', 'auto') or 'auto',
            getattr(config, 'tts_voice', '') or '',
            getattr(config, 'tts_proxy', '') or '',
            voice_cloner=self.voice_cloner,
        )
        self.prompt_builder = PromptBuilder(self.ai_client, vocab_store=self.vocab_store)
        self.executor = AnswerExecutor(driver, tts=self.tts_engine,
                                       stop_requested=self.stop_requested)
        self.publish_parser = PublishQuestionParser(driver)
        self.publish_executor = PublishAnswerExecutor(driver, tts=self.tts_engine,
                                                      stop_requested=self.stop_requested)
        # 独立音视频转写器（新版 ipublish 直接使用，不强耦合旧版 VideoHandler）
        self.audio_transcriber = AudioTranscriber()
        self.video_handler = VideoHandler(driver, self.config, self.stop_requested,
                                          transcriber=self.audio_transcriber)
        self.content_handlers: List[ContentHandler] = [
            AudioHandler(driver, self.stop_requested),
            self.video_handler,
            FlashcardHandler(driver, self.stop_requested),
            SelfCheckHandler(driver, self.stop_requested),
            DiscussionBoardHandler(driver, self.ai_client, self.stop_requested),
        ]
        self.processed_hashes: set = set()
        self._processed_video_tabs: set = set()
        self._skipped_video_tabs: set = set()
        self._processed_audio_tabs: set = set()
        self._audio_transcript_cache: Dict[str, str] = {}
        self._section_transcript_cache: Dict[tuple, List[str]] = {}
        self._current_section_key = None
        self._publish_processed_groups: set = set()
        self._publish_scope: Optional[PublishTaskScope] = None
        self._publish_scope_url: str = ''
        self._video_playback_rate: float = float(getattr(config, 'video_playback_rate', 1.0) or 1.0)
        self._all_skipped_media_tasks: set = set()
        self._current_directory_task: Optional[Dict] = None
        self._retry_hint: str = ''
        self._last_question_types: set = set()

    @staticmethod
    def _build_voice_cloner(config):
        try:
            from unipus_helper.voice_cloner import VoiceCloneEngine
            return VoiceCloneEngine(
                enabled=bool(getattr(config, 'voice_clone_enabled', False)),
                mode=getattr(config, 'voice_clone_mode', 'local') or 'local',
                profile_id=getattr(config, 'voice_clone_profile', 'my_voice') or 'my_voice',
                tau=float(getattr(config, 'voice_clone_tau', 0.35)),
                base_gender=getattr(config, 'voice_clone_base_gender', 'auto') or 'auto',
                rate=getattr(config, 'voice_clone_rate', '+0%') or '+0%',
                convert_reference=bool(getattr(config, 'voice_clone_convert_reference', True)),
                proxy=getattr(config, 'tts_proxy', '') or '',
                api_url=getattr(config, 'voice_clone_api_url', '') or '',
                api_key=getattr(config, 'voice_clone_api_key', '') or '',
                api_model=getattr(config, 'voice_clone_api_model', 'FunAudioLLM/CosyVoice2-0.5B') or '',
            )
        except Exception as exc:
            logger.warning("初始化音色克隆引擎失败: %s", exc)
            return None

    def reload_voice_config(self, data: Dict[str, Any]):
        """热同步「我的音色」配置到运行中的答题引擎，无需重启应用。"""
        cloner = getattr(self, 'voice_cloner', None)
        if cloner is None:
            cloner = self._build_voice_cloner(self.config)
            self.voice_cloner = cloner
        if cloner is not None:
            try:
                tau_val = float(data.get('voice_clone_tau', cloner.tau))
            except (TypeError, ValueError):
                tau_val = cloner.tau
            cloner.configure(
                enabled=bool(data.get('voice_clone_enabled', cloner.enabled)),
                mode=(data.get('voice_clone_mode') or cloner.mode or 'local').strip().lower(),
                profile_id=(data.get('voice_clone_profile') or cloner.profile_id or 'my_voice').strip(),
                tau=tau_val,
                base_gender=(data.get('voice_clone_base_gender') or cloner.base_gender or 'auto').strip().lower(),
                rate=(data.get('voice_clone_rate') or cloner.rate or '+0%').strip(),
                convert_reference=bool(data.get('voice_clone_convert_reference', cloner.convert_reference)),
                proxy=(data.get('tts_proxy') or cloner.proxy or '').strip(),
                api_url=(data.get('voice_clone_api_url') or '').strip(),
                api_key=(data.get('voice_clone_api_key') or '').strip(),
                api_model=(data.get('voice_clone_api_model') or 'FunAudioLLM/CosyVoice2-0.5B').strip(),
            )
            cloner.invalidate_profile_cache()
        if getattr(self, 'tts_engine', None) is not None:
            self.tts_engine.set_voice_cloner(cloner)
        try:
            rate_val = float(data.get('video_playback_rate', getattr(self, '_video_playback_rate', 1.0)))
            if rate_val in (1.0, 1.25, 1.5, 2.0):
                self._video_playback_rate = rate_val
        except (TypeError, ValueError):
            pass

    def _release_voice_cloner(self):
        if getattr(self, 'tts_engine', None) is not None:
            self.tts_engine.release()

    def request_stop(self):
        self.stop_requested.set()

    def clear_stop(self):
        self.stop_requested.clear()

    def _should_stop(self) -> bool:
        return self.stop_requested.is_set()

    def process_selected_tabs(self, selected_tabs: List[Dict], chapter_name: str = "",
                              on_task_done: Optional[Callable[[Dict], None]] = None):
        """
        按用户勾选的 Tab 列表逐个处理（自动模式）。
        selected_tabs: 完整的 tab dict 列表（来自扫描结果）
        on_task_done: 单个任务处理完后的回调，GUI 用它来取消勾选 / 更新状态
        """
        if not chapter_name:
            chapter_name = "selected_chapter"

        print(f"\n{'=' * 60}")
        print(f"开始处理 {len(selected_tabs)} 个选中任务")
        print(f"{'=' * 60}")

        self.ai_client.force_reset(chapter_name)
        self.processed_hashes.clear()
        self._publish_processed_groups = set()

        course_home_url = selected_tabs[0].get('_directory_url') if selected_tabs else ''
        course_home_url = course_home_url or self.driver.current_url
        consecutive_errors = 0
        self._aborted_by_errors = False

        for task_idx, tab in enumerate(selected_tabs):
            if self._should_stop():
                print("  已请求停止，批量处理提前结束")
                break

            self._retry_hint = ''
            self._previous_attempt_answers = []
            self._submission_unconfirmed = False
            tab.pop('_submission_unconfirmed', None)
            tab.pop('session_score', None)
            print(f"\n  [{task_idx + 1}/{len(selected_tabs)}] {tab.get('display') or tab.get('l1_title', '')}")
            if needs_live_conversation(tab):
                print("  实时语音对话，需在 U 校园手动完成")
                continue
            try:
                status = self._process_selected_tab(tab, task_idx, len(selected_tabs), chapter_name, course_home_url)
            except Exception as exc:
                print(f"  该任务处理异常，已保留勾选，下次会从这里继续: {str(exc)[:120]}")
                logger.error(f"任务处理异常: {exc}", exc_info=True)
                status = 'error'
            if self._submission_unconfirmed:
                tab['_submission_unconfirmed'] = True

            if status == 'done':
                consecutive_errors = 0
                if self._is_publish_page():
                    # 新版外壳和同页其他题组都可能带数字，成绩由当前任务的学习记录确认。
                    if tab.get('status') != 'done':
                        tab['status'] = 'unknown'
                        tab['status_text'] = '待平台确认'
                    continue
                last_types = getattr(self, '_last_question_types', set())
                record_types = {
                    QuestionType.RECORD, QuestionType.ORAL_ALOUD,
                    QuestionType.MY_VOICE_TEXT,
                }
                auto_graded_types = {
                    QuestionType.SINGLE_CHOICE, QuestionType.LISTENING_CHOICE,
                    QuestionType.VIDEO_CHOICE, QuestionType.MULTIPLE_CHOICE,
                    QuestionType.TRUE_FALSE, QuestionType.SORTING,
                    QuestionType.BANKED_CLOZE, QuestionType.DROPDOWN_SELECT,
                    QuestionType.FILL_IN, QuestionType.LISTENING_FILL_IN,
                    QuestionType.MATCHING, QuestionType.PARAGRAPH_MATCH,
                    QuestionType.LINK_MATCH, QuestionType.VOCABULARY_TEST,
                }
                expects_score = tab.get('is_scored') is not False
                score = self._read_task_score() if expects_score else None
                # 朗读题评分是提交后异步返回的，页面立即读可能为空；
                # 最多等 30 秒再判定“无分数”，避免把未出分当成完成。
                if expects_score and score is None and last_types & record_types:
                    for _ in range(15):
                        if self.stop_requested.wait(2):
                            break
                        score = self._read_task_score()
                        if score is not None:
                            print(f"  朗读评分已返回: {score:g}")
                            break
                elif expects_score and score is None and last_types & auto_graded_types:
                    # 部分客观题出分或重做按钮渲染略有延迟，轮询最多 2.5s 避免过早离开
                    for _ in range(5):
                        if self.stop_requested.wait(0.5):
                            break
                        score = self._read_task_score()
                        if score is not None or self._has_redo_button():
                            break
                retries = 0
                retry_status = status
                initial_score = score

                def _needs_retry() -> bool:
                    if not expects_score:
                        return False
                    if score is not None:
                        return self._score_is_abnormal(score) and retries < 3
                    # 多次答题机会的客观题首次答错时，平台不会立即出最终分而是显示「重做/返回修改」
                    # 并保持「未开始/进行中」状态；允许最多 4 次去重改错重试，避免原样重复错答。
                    return (bool(last_types & auto_graded_types)
                            and retries < 4
                            and self._has_redo_button())

                while _needs_retry():
                    if self._should_stop():
                        retry_status = 'stopped'
                        break
                    hint = self._read_answer_analysis()
                    prev_attempts = list(getattr(self, '_previous_attempt_answers', []) or [])
                    if prev_attempts:
                        prev_note = (
                            "【严禁重复·上次未通过记录】以下作答均已提交且被平台判定未通过（要求返回修改）："
                            + "、".join(prev_attempts[-6:])
                            + "。\n请特别警惕语音转写可能存在的同音词误差（如 exhibition/expedition、人称 he/she、数字与地点细节），"
                            + "逐条重新核对原文并给出一个与上述已失败组合完全不同的新答案！"
                        )
                        hint = f"{hint}\n\n{prev_note}".strip() if hint else prev_note
                    self._retry_hint = hint
                    if not self._click_redo_button():
                        if score is not None:
                            print(f"  当前页面未提供重做入口，保留已显示的 {score:g} 分")
                        break
                    retries += 1
                    score_desc = f"本次得分 {score:g} 偏低" if score is not None else "提交后未出分且出现重做按钮"
                    max_retries = 3 if score is not None else 4
                    print(f"  {score_desc}，"
                          f"{'使用答案解析' if hint else '用 AI 重做'}（第 {retries}/{max_retries} 次）")
                    logger.warning("作答复核 %s: %s", tab.get('display'), score_desc)
                    self.processed_hashes.clear()
                    # 保留课程材料，清空上次作答对本轮的影响。
                    if isinstance(getattr(self.ai_client, 'conversation_history', None), list):
                        self.ai_client.conversation_history.clear()
                    try:
                        tab_name = tab.get('l1_title', 'unknown')
                        ok = self._process_tab_with_accumulation(tab_name, task_idx, 0)
                        retry_status = 'stopped' if self._should_stop() else ('done' if ok else 'error')
                    except Exception as exc:
                        logger.error(f"重做任务异常: {exc}", exc_info=True)
                        retry_status = 'error'
                    if self._submission_unconfirmed:
                        tab['_submission_unconfirmed'] = True
                    if retry_status != 'done':
                        break
                    score = self._read_task_score()

                if retry_status == 'stopped':
                    print(f"  重做已中断，停止后续任务")
                    break
                if retry_status != 'done':
                    # 首次提交已经成功并拿到了成绩，重做失败不应中断整批后续任务；
                    # 保留首次成绩并继续下一个任务，避免整轮批量处理被迫中止。
                    self._retry_hint = ''
                    print(f"  重做未完成（{retry_status}），保留已提交成绩并继续下一个任务")
                    logger.warning("分数异常重做未完成 %s: status=%s, score=%s",
                                   tab.get('display'), retry_status, initial_score)
                    tab['status'] = 'unknown'
                    tab['status_text'] = '待核验重做结果'
                    if initial_score is not None:
                        tab['score'] = initial_score
                else:
                    if (expects_score and score is None
                            and bool(last_types & auto_graded_types) and self._has_redo_button()):
                        # 多次重试后仍未出分且页面仍显示重做按钮，不能伪装成「已完成」
                        tab['status'] = 'doing'
                        tab['status_text'] = '待平台确认（未出分）'
                        print("  提交后仍未出分且存在重做按钮，暂不标记为已完成")
                    elif score is None and tab.get('is_scored') is True:
                        # 计分任务提交后尚未出分，交给学习记录核验。
                        tab['status'] = 'unknown'
                        tab['status_text'] = '待平台确认（未读到分数）'
                        print("  计分客观题本次未读到页面分数，暂标记待平台复核")
                    else:
                        awaiting_directory = bool(tab.get('_directory_url')) and tab.get('status') != 'done'
                        tab['status'] = 'unknown' if awaiting_directory else 'done'
                        tab['status_text'] = '待平台确认' if awaiting_directory else '已完成'
                    tab['session_score'] = score
                    if score is not None:
                        tab['score'] = score
                    tab['score_abnormal'] = score is not None and self._score_is_abnormal(score)
                    if on_task_done and (tab.get('status') == 'done' or (score is not None and tab.get('_directory_url'))):
                        try:
                            on_task_done(tab)
                        except Exception as exc:
                            print(f"  完成回调异常: {str(exc)[:60]}")
            elif status == 'error':
                if tab.get('_submission_unconfirmed'):
                    tab['status'] = 'unknown'
                    tab['status_text'] = '待提交核验'
                    continue
                if getattr(self, '_last_task_empty', False):
                    self._last_task_empty = False
                    self._last_task_partial = False
                    consecutive_errors = 0
                    print("  该任务没有可作答内容，跳过并继续下一个")
                    tab['status_text'] = '无可作答内容'
                    if on_task_done:
                        try:
                            on_task_done(tab)
                        except Exception as exc:
                            print(f"  完成回调异常: {str(exc)[:60]}")
                    continue
                consecutive_errors += 1
                if consecutive_errors >= 4:
                    self._aborted_by_errors = True
                    print("  连续 4 个任务出错，停止批量处理，避免空转")
                    break
                if getattr(self, '_last_task_partial', False):
                    self._last_task_partial = False
                    self._last_task_empty = False
                    print("  本任务有音视频未播完或题目未完成，已保留勾选")
                    tab['status_text'] = '部分未完成'
                    continue
                self._last_task_partial = False
                self._last_task_empty = False
                tab['status_text'] = '出错已跳过'
                print("  本任务出错，已跳过继续下一个（连续 4 个出错才停）")
                continue

            if self.stop_requested.wait(0.2):
                break

        self._release_transcriber()
        self._release_voice_cloner()

        print(f"\n{'=' * 60}")
        if self._should_stop() or self._aborted_by_errors:
            print("批量处理已停止")
        else:
            print("本轮处理结束，课程任务的完成状态以平台目录核验为准")
        print(f"{'=' * 60}")

    def _score_is_abnormal(self, score: float) -> bool:
        return score < MIN_TASK_SCORE

    _REDO_BUTTON_JS = r"""
        const shouldClick = !!arguments[0];
        const clean = t => (t || '').replace(/\s+/g, '').trim();
        const stripCounter = t => t.replace(/[（(][^()（）]{0,12}[)）]/g, '').replace(/\d+\/\d+/g, '').trim();
        const isVisible = el => {
            if (!el || !el.getClientRects || !el.getClientRects().length) return false;
            const style = window.getComputedStyle(el);
            if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            if (el.closest('.ant-modal-wrap, .ant-modal, [role="dialog"]')) return false;
            const cls = (el.className || '').toString().toLowerCase();
            if (cls.includes('disabled') || cls.includes('ban') || el.disabled) return false;
            return true;
        };
        const targets = new Set([
            '重做', '再做一次', '重新作答', '重新做题', '重新答题', '再试一次',
            '再次作答', '再答一次', '重做错题', '重新提交', '修改答案', '继续作答',
            '返回修改', '再改一次', '再练一次', '再测一次', '重新做',
            'tryagain', 'redo', 'retry', 'retake'
        ]);
        const cands = document.querySelectorAll(
            'button, a, [role="button"], div.btn, span.btn, '
            + 'div[class*="btn"], span[class*="btn"], '
            + '[class*="submit-bar"], [class*="submit-bar"] div, [class*="submit-bar"] span, '
            + '[class*="redo"], [class*="Redo"]'
        );
        for (const el of cands) {
            if (!isVisible(el)) continue;
            const rawText = clean(el.innerText || el.textContent);
            if (!rawText || rawText.length > 18) continue;
            const baseText = stripCounter(rawText);
            if (!baseText || baseText.length > 10) continue;
            if (targets.has(baseText) || targets.has(baseText.toLowerCase())) {
                if (el.querySelector('button, a')) continue;
                const childDiv = el.querySelector('div');
                if (childDiv && clean(childDiv.innerText || childDiv.textContent) !== rawText) continue;
                if (shouldClick) el.click();
                return rawText;
            }
        }
        return '';
    """

    def _has_redo_button(self) -> bool:
        """检测页面是否存在可点击的「重做 / 再做一次 / 重新作答」按钮。"""
        try:
            return bool(self.driver.execute_script(self._REDO_BUTTON_JS, False))
        except Exception:
            return False

    def _exit_review_mode_if_needed(self) -> bool:
        """若任务页打开时已处于「上次提交后的查看态」（无提交按钮但有重做/再做一次按钮），
        先提取已有答案解析并点击重做切回可作答状态，避免把锁死查看页误判为 0 题或无法提交。"""
        if self._is_publish_page() or self._should_stop():
            return False
        try:
            if self._find_visible_submit_button() is not None:
                return False
            if not self._has_redo_button():
                return False
            hint = self._read_answer_analysis()
            if hint and not getattr(self, '_retry_hint', ''):
                self._retry_hint = hint
            print("    检测到任务处于已提交查看态，自动点击重做切回作答态...")
            if not self._click_redo_button():
                return False
            deadline = time.time() + 5.0
            while time.time() < deadline and not self._should_stop():
                if self._find_visible_submit_button() is not None or not self._has_redo_button():
                    break
                if self.stop_requested.wait(0.2):
                    return False
            return True
        except Exception:
            return False

    def _click_redo_button(self) -> bool:
        """在已提交页面查找并点击「重做 / 再做一次 / 重新作答」按钮。"""
        try:
            hit = self.driver.execute_script(self._REDO_BUTTON_JS, True)
            if not hit:
                return False
            print(f"  已点击重做按钮: {hit}")
            if self.stop_requested.wait(0.5):
                return False
            self._handle_confirm_dialog()
            if self.stop_requested.wait(0.8):
                return False
            return True
        except Exception:
            return False

    def _read_answer_analysis(self) -> str:
        """提交后的答案解析或逐项判分反馈（正确/错误标记）；页面没有时返回空串。"""
        js = r"""
        const nodes = Array.from(document.querySelectorAll(
            '[class*="analysis"], [class*="answer-detail"], [class*="correct-answer"], .answer-content'));
        const text = nodes.filter(n => n.offsetParent !== null).map(n => n.innerText || '').join('\n');
        const match = text.match(/(正确答案|参考答案|答案解析)[\s\S]{0,1500}/);
        if (match) return match[0].slice(0, 1500);

        // 多次作答首次提交后，部分题型不直接公布参考答案，但会给选项/空位打 right/wrong 标记
        const feedback = [];
        const hasFlag = (el, re) => {
            const cls = String(el.className || '');
            if (re.test(cls)) return true;
            return !!el.querySelector('[class*="wrong"], [class*="error"], [class*="right"], [class*="correct"]') &&
                   Array.from(el.querySelectorAll('*')).some(c => re.test(String(c.className || '')));
        };
        const wrongRe = /(?:^|[_-\s])(?:wrong|error|incorrect|mistake)(?:$|[_-\s])/i;
        const rightRe = /(?:^|[_-\s])(?:right|correct)(?:$|[_-\s])/i;
        const opts = document.querySelectorAll('.option, .MultipleChoice--checkbox-item-34A_-');
        const rightOpts = [], wrongOpts = [];
        for (const opt of opts) {
            if (!opt.getClientRects || !opt.getClientRects().length) continue;
            const cap = opt.querySelector('.caption, span[class*="index"], .MultipleChoice--checkbox-opt-2F4xY');
            const label = ((cap ? cap.innerText : opt.innerText) || '').trim().slice(0, 30);
            if (!label) continue;
            if (hasFlag(opt, wrongRe)) wrongOpts.push(label);
            else if (hasFlag(opt, rightRe)) rightOpts.push(label);
        }
        if (rightOpts.length || wrongOpts.length) {
            feedback.push('上次选项判分：正确=[' + rightOpts.join(', ') + '] 错误/漏选=[' + wrongOpts.join(', ') + ']');
        }
        const scoops = document.querySelectorAll('.scoop-select-wrapper, .fe-scoop');
        let idx = 0;
        for (const sc of scoops) {
            idx += 1;
            if (!sc.getClientRects || !sc.getClientRects().length) continue;
            const txt = (sc.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40);
            if (hasFlag(sc, wrongRe)) feedback.push('空' + idx + ' 判错(已填:' + txt + ')');
            else if (hasFlag(sc, rightRe)) feedback.push('空' + idx + ' 判对(已填:' + txt + ')');
        }
        return feedback.join('\n').slice(0, 1500);
        """
        try:
            value = self.driver.execute_script(js)
            return value.strip() if isinstance(value, str) else ''
        except Exception:
            return ''

    def _read_task_score(self) -> Optional[float]:
        """页面上当前这次作答的分数；还没出分或认不出来时返回 None。"""
        try:
            value = self.driver.execute_script(PAGE_SCORE_JS)
        except Exception:
            return None
        return confirmed_score(value)

    def _process_selected_tab(self, tab: Dict, task_idx: int, total: int,
                              chapter_name: str, course_home_url: str) -> str:
        """处理单个选中任务，返回 done / skipped / stopped / error。"""
        tab_name = tab.get('l1_title', 'unknown')

        if self._should_stop():
            return 'stopped'

        # 每个任务页都可能自带 Learning / Practicing 标签，逐个任务重新判断
        self._inline_learning_tried = False
        # 纯阅读页（无题目、无音视频）不算失败，批量处理时跳过继续
        self._last_task_empty = False
        # 有题目但没渲染/被平台拒绝：算部分完成，跳过本任务继续下一个
        self._last_task_partial = False
        self._last_question_types = set()
        self._current_tab_is_scored = tab.get('is_scored')
        tab.pop('_media_scope_key', None)
        self._current_directory_task = tab if tab.get('_element') is not None else None
        section_norm = (tab.get('section') or tab.get('l2_title') or '').strip().lower()
        self._current_section_key = (tab.get('_unit_idx'), section_norm) if section_norm else None

        # 目录模式每个任务独立重置 AI 上下文（必须在 _load_reading_context 之前，
        # 既防止 task_idx == 0 残留上一批任务的转写，又不冲掉刚刚拉取的 Collocation 课文）
        if '_element' in tab and tab['_element'] is not None:
            self.ai_client.force_reset(f"{chapter_name}_{tab_name}")

        # Collocation（搭配填空）要求「从课文里找搭配」，先把同单元 Reading the text 当上下文
        section_text = ' '.join([
            str(tab.get('section') or ''),
            str(tab.get('l2_title') or ''),
            str(tab.get('display') or ''),
        ]).lower()
        if 'collocation' in section_text and tab.get('_unit_idx') is not None:
            self._load_reading_context(course_home_url, tab.get('_unit_idx'),
                                      section_filter=str(tab.get('section') or tab.get('l2_title') or ''))
            if self._should_stop():
                return 'stopped'

        if '_element' in tab and tab['_element'] is not None:
            if task_idx > 0 or self.driver.current_url != course_home_url:
                self.driver.get(course_home_url)
                # 条件轮询目录就绪（确保无加载转圈且点击事件与学习记录已完成水合）
                dir_deadline = time.time() + 10.0
                while time.time() < dir_deadline and not self._should_stop():
                    try:
                        ready = self.driver.execute_script(
                            "return document.readyState === 'complete' && !document.querySelector('.ant-spin-spinning') && "
                            "!!document.querySelector('[class*=\"unitTabScrollContainer\"], [class*=\"taskItemInnerLayout\"], [class*=\"nodeItemInnerLayout\"]');"
                        )
                        if ready:
                            break
                    except Exception:
                        pass
                    if self.stop_requested.wait(0.2):
                        return 'stopped'
                if self.stop_requested.wait(1.2):
                    return 'stopped'

            if '_unit_idx' in tab:
                try:
                    unit_container = WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, '[class*="unitTabScrollContainer"]'))
                    )
                    unit_tabs = unit_container.find_elements(By.CSS_SELECTOR, ':scope > *')
                    unit_id = tab.get('_unit_id')
                    if unit_id:
                        target_unit = next((el for el in unit_tabs
                                            if el.get_attribute('id') == f'id-{unit_id}-tab'), None)
                    else:
                        index = tab['_unit_idx']
                        target_unit = unit_tabs[index] if 0 <= index < len(unit_tabs) else None
                    if target_unit is None:
                        print("    目标Unit不存在，跳过")
                        return 'skipped'
                    if self._should_stop():
                        return 'stopped'
                    is_active_js = CourseCatalogMixin.ACTIVE_UNIT_JS
                    already_active = False
                    try:
                        already_active = bool(self.driver.execute_script(is_active_js, target_unit))
                    except Exception:
                        already_active = False
                    if not already_active:
                        for _u_try in range(3):
                            self.driver.execute_script("arguments[0].click();", target_unit)
                            if self.stop_requested.wait(1.0):
                                return 'stopped'
                            try:
                                if self.driver.execute_script(is_active_js, target_unit):
                                    already_active = True
                                    break
                            except Exception:
                                break
                        if not already_active:
                            print("    未确认已切换到选中的单元，请重新扫描目录")
                            return 'skipped'
                        spin_deadline = time.time() + 4.0
                        while time.time() < spin_deadline and not self._should_stop():
                            try:
                                if not self.driver.execute_script("return !!document.querySelector('.ant-spin-spinning');"):
                                    break
                            except Exception:
                                break
                            if self.stop_requested.wait(0.15):
                                return 'stopped'
                        if self.stop_requested.wait(0.35):
                            return 'stopped'
                except Exception as e:
                    print(f"    切换Unit失败: {str(e)[:50]}")
                    return 'skipped'

            # 单次 JS 定位当前可见面板中的匹配任务项：
            #   attempt 0: 点击任务标题 (nameEl)
            #   attempt 1: 点击任务行容器
            #   attempt 2: 返回 WebElement 用 Selenium 原生点击（应对 isTrusted/合成指针校验）
            locate_and_click_js = r"""
                const targetName = arguments[0];
                const targetOcc = arguments[1] || 0;
                const mode = arguments[2] || 0;
                const targetSection = arguments[3];
                const rows = (function() {
            """ + CourseCatalogMixin.READ_TASKS_JS + r"""
                })().filter(r => r.name === targetName);
                const inSection = targetSection == null
                    ? rows : rows.filter(r => r.section === targetSection);
                const row = targetSection != null && inSection.length === 1 ? inSection[0] : rows[targetOcc];
                if (!row || !inSection.includes(row)) return null;
                const items = document.querySelectorAll('[class*="taskItemInnerLayout"], [class*="nodeItemInnerLayout"]');
                const ch = items[row.index];
                const nameEl = ch?.querySelector('[class*="taskTypeName"], [class*="nodeTypeName"]');
                if (!nameEl) return null;
                ch.scrollIntoView({block: 'center', behavior: 'instant'});
                if (mode === 2) return nameEl;
                (mode === 0 ? nameEl : ch).click();
                return true;
            """
            prev_handles = []
            try:
                prev_handles = list(self.driver.window_handles)
            except Exception:
                pass

            navigated = False
            for attempt in range(3):
                chapter_clicked = False
                click_deadline = time.time() + 2.5
                while time.time() < click_deadline and not self._should_stop():
                    try:
                        res = self.driver.execute_script(
                            locate_and_click_js, tab_name, tab.get('_name_occurrence', 0), attempt,
                            tab.get('_section_title'))
                        if res:
                            if attempt == 2 and not isinstance(res, bool):
                                chapter_clicked = bool(WebDriverHelper.safe_click(self.driver, res))
                            else:
                                chapter_clicked = True
                            if chapter_clicked:
                                break
                    except Exception as e:
                        print(f"    重新定位章节失败: {str(e)[:50]}")
                        break
                    if self.stop_requested.wait(0.15):
                        return 'stopped'
                if self._should_stop():
                    return 'stopped'
                if not chapter_clicked:
                    print("  点击章节失败，跳过")
                    return 'skipped'

                # 确认浏览器确实离开目录页（含同页跳转或新标签页打开）
                nav_deadline = time.time() + (2.5 if attempt < 2 else 5.0)
                while time.time() < nav_deadline and not self._should_stop():
                    try:
                        cur_handles = list(self.driver.window_handles)
                        if len(cur_handles) > len(prev_handles):
                            self.driver.switch_to.window(cur_handles[-1])
                            navigated = True
                            break
                        if (self.driver.current_url or '') != course_home_url:
                            navigated = True
                            break
                    except Exception:
                        pass
                    if self.stop_requested.wait(0.2):
                        return 'stopped'
                if navigated or self._should_stop():
                    break
                if attempt == 0:
                    print("    点击任务名后页面未跳转，改点任务行重试...")
                elif attempt == 1:
                    print("    点击任务行后仍未跳转，改用原生鼠标点击重试...")

            if self._should_stop():
                return 'stopped'
            if not navigated:
                cur_u = ''
                try:
                    cur_u = (self.driver.current_url or '')[:80]
                except Exception:
                    pass
                print(f"  点击任务后仍停留在课程目录页（{cur_u}），保留勾选跳过")
                self._last_task_partial = True
                return 'error'

            # 条件轮询任务页真实内容挂载完成（兼容旧版 ucontent 与新版 ipublish iframe）
            # 注意：不能把空外壳 .question-common-course-page 当作挂载完成，否则慢网时会在题目加载前提前放行导致 0 题！
            if self.stop_requested.wait(0.35):
                return 'stopped'
            page_deadline = time.time() + 12.0
            while time.time() < page_deadline and not self._should_stop():
                try:
                    mounted = self.driver.execute_script(
                        "if (document.readyState !== 'complete') return false;"
                        "if (document.querySelector('.ant-spin-spinning')) return false;"
                        "if (location.href.includes('publishpc_default')) {"
                        "  return !!document.querySelector('iframe#ipublish-pc-book-easy-iframe, "
                        "    iframe[id*=\"ipublish\"], iframe[src*=\"ipub.unipus.cn\"]');"
                        "}"
                        "return !!document.querySelector("
                        "'.layout-container, .question-common-abs-choice, "
                        ".record-button-wrap, .vocabulary-wrapper, .p-oral-aloud, .question-video-dub-pc, "
                        ".abs-direction, .layout-direction-container, "
                        ".discussion-title, video, audio, "
                        "iframe#ipublish-pc-book-easy-iframe, iframe[src*=\"ipub.unipus.cn\"], "
                        "[data-ipublish-question-group-type]');"
                    )
                    if mounted:
                        break
                except Exception:
                    pass
                if self.stop_requested.wait(0.2):
                    return 'stopped'
            if self.stop_requested.wait(0.4):
                return 'stopped'
            success = self._process_tab_with_accumulation(tab_name, task_idx, 0)
            return 'stopped' if self._should_stop() else ('done' if success else 'error')

        l1_idx = tab.get('l1_idx', 0)
        l2_idx = tab.get('l2_idx', -1)

        level1_tabs = self._get_level1_tabs()
        if l1_idx >= len(level1_tabs):
            print(f"  一级Tab索引 {l1_idx} 越界，跳过")
            return 'skipped'

        l1_tab = level1_tabs[l1_idx]
        print(f"    一级Tab: {l1_tab['title']}")

        if self._should_stop():
            return 'stopped'
        if not WebDriverHelper.safe_click(self.driver, l1_tab['element']):
            print(f"  点击一级Tab失败，跳过")
            return 'skipped'
        if self.stop_requested.wait(1.5):
            return 'stopped'
        if l2_idx < 0:
            success = self._process_tab_with_accumulation(l1_tab['title'], l1_idx, 0)
            return 'stopped' if self._should_stop() else ('done' if success else 'error')

        level2_tabs = self._get_level2_tabs()
        if l2_idx >= len(level2_tabs):
            print(f"  二级Tab索引 {l2_idx} 越界，跳过")
            return 'skipped'

        l2_tab = level2_tabs[l2_idx]
        print(f"    二级Tab: {l2_tab['title']}")

        if self._should_stop():
            return 'stopped'
        if not WebDriverHelper.safe_click(self.driver, l2_tab['element']):
            print(f"  点击二级Tab失败，跳过")
            return 'skipped'
        if self.stop_requested.wait(1.5):
            return 'stopped'
        combined_name = f"{l1_tab['title']}_{l2_tab['title']}"
        success = self._process_tab_with_accumulation(combined_name, l1_idx, l2_idx)
        return 'stopped' if self._should_stop() else ('done' if success else 'error')

    def _answer_chunked_fill(self, q: Question, directions: str = "", chunk_size: int = 8) -> bool:
        """空位很多的填空题拆成小块问模型，按「全局空号」写回，全部填上才算成功。"""
        total = len(q.inputs)
        if not total:
            return False
        filled = 0
        used_answers: List[str] = []
        print(f"    空位较多（{total} 个），拆成每 {chunk_size} 空一批作答")

        pending = [(start, min(start + chunk_size, total)) for start in range(0, total, chunk_size)]
        # 从题面 + 课文上下文里给每个空列出「以已给字母开头的候选词」，
        # 模型在搭配填空里最容易错的就是挑同前缀的其它词，给候选能明显纠偏。
        corpus = q.text or ""
        try:
            corpus += " " + " ".join(m.get('content', '') for m in self.ai_client.material_messages)
        except Exception:
            pass
        corpus_words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", corpus)
        while pending:
            if self._should_stop():
                return False
            start, end = pending.pop(0)
            sub_blanks = [dict(b) for b in q.banked_blanks[start:end]]
            for item in sub_blanks:
                hint = (item.get('hint') or '').strip()
                if not hint:
                    continue
                seen = []
                for word in corpus_words:
                    if word.lower().startswith(hint.lower()) and len(word) > len(hint):
                        if word.lower() not in [w.lower() for w in seen]:
                            seen.append(word)
                    if len(seen) >= 8:
                        break
                item['candidates'] = seen
            sub = Question(
                number=q.number,
                text=q.text,
                q_type=q.q_type,
                element=q.element,
                inputs=q.inputs[start:end],
                banked_blanks=sub_blanks,
                banked_options=q.banked_options,
                directions=q.directions,
            )
            prompt = self.prompt_builder.build([sub], directions)
            if not prompt:
                continue
            prompt += (f"\n本批只包含第 {start + 1}-{end} 空，请按这些空号作答，"
                       f"格式：{start + 1}.答案 {start + 2}.答案 ...")
            if used_answers and re.search(r'\b(?:only\s+once|once\s+only)\b', q.directions or '', re.I):
                prompt += ("\n【已用答案】前面批次已填入：" + "；".join(used_answers) +
                           "。词库里的每个表达只能用一次，本批不得重复使用，"
                           "请优先选取尚未使用的表达。")
            response = self.ai_client.ask(prompt, stop_requested=self.stop_requested,
                                          screenshot=self._question_screenshot([q]))
            if not response:
                # 推理型模型偶尔整批返回空（token 全烧在思考上），把这批再拆小重问
                if end - start > 2:
                    mid = (start + end) // 2
                    print(f"    第 {start + 1}-{end} 空无答案，拆成两批重试")
                    pending[0:0] = [(start, mid), (mid, end)]
                else:
                    # 只剩 1-2 个空还拿不到答案：换一种更短的问法再试一次
                    retry_prompt = (f"【填空】请只回答第 {start + 1}-{end} 空，"
                                    f"每空一个答案（可以是多词短语），格式：{start + 1}.答案\n"
                                    f"{q.text[:1200]}")
                    for item in sub_blanks:
                        retry_prompt += f"\n空{item['index'] + 1}: 提示 {item.get('hint', '') or '无'}；前文 {item.get('context', '')[-80:]}"
                    response = self.ai_client.ask(retry_prompt, retry_count=1,
                                                  stop_requested=self.stop_requested)
                if not response:
                    print(f"    第 {start + 1}-{end} 空未拿到答案，跳过")
                    continue

            mapping = self._parse_numbered_map(response)
            for offset, inp in enumerate(sub.inputs):
                global_no = start + offset + 1
                ans = mapping.get(global_no) or mapping.get(offset + 1) or ""
                if not ans:
                    continue
                hint = sub_blanks[offset].get('hint', '') if offset < len(sub_blanks) else ''
                ans = self.executor._strip_given_prefix(hint, ans)
                try:
                    if self.executor._fill_text_input_verified(inp, ans):
                        filled += 1
                        used_answers.append(ans)
                except Exception as exc:
                    print(f"    空{global_no} 写入失败: {str(exc)[:50]}")
            print(f"    第 {start + 1}-{end} 空已处理（累计 {filled}/{total}）")

        return filled == total

    @staticmethod
    def _parse_numbered_map(text: str) -> Dict[int, str]:
        """把 "1.on average 2.come across" 解析成 {1: 'on average', 2: 'come across'}。

        答案经常是多词短语（on average / with respect to / turn one's nose up），
        必须在「下一个编号」处截断，不能按空格切。老实现按非空白字符匹配，只取到首个单词，
        结果把 on average 回填成 on、come across 回填成 come，整题判错。
        """
        result: Dict[int, str] = {}
        if not (text or '').strip():
            return result
        marker = re.compile(r'(?:^|[\s,;，；])(?:空\s*)?(\d{1,2})\s*[.、:：)）]')
        matches = list(marker.finditer(text))
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            value = text[match.end():end].strip().strip(' .,;，。；')
            if value:
                result.setdefault(int(match.group(1)), value)
        if not result:
            # 没有编号时按行降级，仍然保留整行的多词内容
            for line in (text or '').splitlines():
                cleaned = re.sub(r'^\s*(?:空\s*)?\d{1,2}\s*[.、:：)）]\s*', '', line)
                cleaned = cleaned.strip().strip(' .,;，。；')
                if cleaned:
                    result[len(result) + 1] = cleaned
        return result

    def _load_reading_context_for_current_page(self) -> bool:
        """快速模式用：先试当前页切 Learning；不行再从播放器页反推单元去目录找。"""
        if getattr(self, '_reading_ctx_tried', False):
            return False
        self._reading_ctx_tried = True

        if self._load_learning_inline():
            return True

        url = self.driver.current_url or ""
        resource_id = ""
        try:
            query = parse_qs(urlsplit(url).query)
            resource_id = (query.get('courseResourceId') or [""])[0]
        except Exception:
            resource_id = ""
        if not resource_id:
            print("    搭配学习页：当前页没有 courseResourceId，跳过")
            return False

        unit_label = self._unit_label_from_page()
        if not unit_label:
            print("    搭配学习页：认不出当前单元，跳过")
            return False

        directory_url = f"https://ucloud.unipus.cn/app/cmgt/resource-detail/{resource_id}"
        print(f"    搭配学习页：当前单元 {unit_label}，准备读取同单元 Learning 页")
        try:
            self._context_reference_text = self.driver.find_element(By.TAG_NAME, 'body').text or ""
        except Exception:
            self._context_reference_text = ""
        return self._load_reading_context(directory_url, unit_label=unit_label)

    def _unit_label_from_page(self) -> str:
        """从面包屑「课程 > 单元标题 > 板块」+ 侧栏「Unit N 单元标题」反推 Unit 编号。"""
        try:
            body = self.driver.find_element(By.TAG_NAME, 'body').text or ""
        except Exception:
            return ""
        first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
        head = body[:800] if body else ""
        breadcrumb = [part.strip() for part in head.split('>') if part.strip()]
        if not first_line or len(breadcrumb) < 2:
            breadcrumb = [part.strip() for part in first_line.split('>') if part.strip()]
        unit_title = breadcrumb[1] if len(breadcrumb) >= 2 else ""

        items = []
        try:
            items = self.driver.execute_script("""
                const out = [];
                for (const el of document.querySelectorAll('div, span, li, a')) {
                  const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                  if (text.length > 80) continue;
                  if (!/^Unit\\s*\\d+\\b/i.test(text)) continue;
                  if (out.includes(text)) continue;
                  out.push(text);
                }
                return out;
            """) or []
        except Exception:
            items = []

        if unit_title:
            probe = re.sub(r'\W+', '', unit_title).lower()
            for item in items:
                match = re.match(r'(Unit\s*\d+)\s*(.*)', item, re.I)
                if not match:
                    continue
                title_part = re.sub(r'\W+', '', match.group(2) or '').lower()
                # 侧栏标题会被省略成 "When work is a pleasu..."，用前缀匹配
                if probe and title_part and (title_part.startswith(probe[:10])
                                             or probe.startswith(title_part[:10])):
                    return match.group(1)
        if items:
            print(f"    搭配学习页：侧栏单元标题没对上（{unit_title or '无面包屑'}），候选: {items[:6]}")
        return ""

    def _load_learning_inline(self) -> bool:
        """当前页顶部就有 Learning / Practicing 标签，直接切过去读答案页再切回来。

        比绕目录更准：Learning 一定是当前 Practicing 的同一分组，不会读错。
        """
        if getattr(self, '_inline_learning_tried', False):
            return False
        self._inline_learning_tried = True
        try:
            tabs = [t for t in self.driver.find_elements(By.CSS_SELECTOR, '[class*="pc-header-tab"]')
                    if t.is_displayed()]
        except Exception:
            return False

        learning = next((t for t in tabs if (t.text or '').strip().lower() == 'learning'), None)
        practicing = next((t for t in tabs if (t.text or '').strip().lower() == 'practicing'), None)
        if learning is None:
            return False

        try:
            self.driver.execute_script("arguments[0].click();", learning)
        except Exception:
            return False
        time.sleep(5)

        text = ""
        try:
            text = self.driver.execute_script("""
                const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                let text = clean(document.body.innerText);
                const cut = text.indexOf('目录笔记');
                if (cut > 200) text = text.slice(0, cut);
                return text.slice(0, 7000);
            """) or ""
        except Exception:
            text = ""

        if practicing is not None:
            try:
                self.driver.execute_script("arguments[0].click();", practicing)
            except Exception:
                pass
            time.sleep(4)

        if len(text) < 120:
            print("    搭配学习页：Learning 页没抓到内容，跳过")
            return False

        self._inline_learning_chars = len(text)
        self.ai_client.add_passage_if_new(text)
        print(f"    搭配学习页：当前页切到 Learning 抓到 {len(text)} 字符，已加入上下文")
        return True

    def _load_reading_context(self, directory_url: str, unit_idx: Optional[int] = None,
                              unit_label: str = "", section_filter: str = "") -> bool:
        """把搭配答案页作为上下文：优先当前页切 Learning，失败再绕目录找。"""
        if self._load_learning_inline():
            # Learning 页就是「已填好的讲解版」，内容够充实就不用再拉课文
            if getattr(self, '_inline_learning_chars', 0) >= 400:
                return True
            print("    搭配学习页：Learning 内容太短，改用同单元 Reading the text 兜底")
        if not directory_url or (unit_idx is None and not unit_label):
            return False
        if getattr(self, '_loaded_reading_units', None) is None:
            self._loaded_reading_units = set()
        cache_key = (directory_url, unit_idx, (unit_label or '').lower(), section_filter.lower())
        if cache_key in self._loaded_reading_units:
            return True

        try:
            original = self.driver.current_window_handle
        except Exception:
            original = None

        try:
            self.driver.switch_to.new_window('tab')

            def open_unit():
                self.driver.get(directory_url)
                WebDriverWait(self.driver, 25).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, '[class*="unitTabScrollContainer"]')))
                time.sleep(1.5)
                tabs = self.driver.find_elements(By.CSS_SELECTOR, '[class*="unitTabScrollContainer"] > *')
                target = None
                if unit_label:
                    wanted = re.sub(r'\s+', '', unit_label).lower()
                    for tab in tabs:
                        if wanted and wanted in re.sub(r'\s+', '', (tab.text or '')).lower():
                            target = tab
                            break
                elif unit_idx is not None and unit_idx < len(tabs):
                    target = tabs[unit_idx]
                if target is None:
                    return False
                self.driver.execute_script("arguments[0].click();", target)
                time.sleep(1.5)
                return True

            if not open_unit():
                print(f"    搭配学习页：目录里找不到 {unit_label or unit_idx}，跳过")
                return False

            candidates = self.driver.execute_script("""
                const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                const all = [...document.querySelectorAll('[class*="taskItemInnerLayout"]')];
                const out = [];
                all.forEach((el, index) => {
                  if (!el.getClientRects().length) return;
                  const nameEl = el.querySelector('[class*="taskTypeName"]');
                  const name = clean(nameEl && nameEl.innerText).toLowerCase();
                  if (!name.includes('learning')) return;
                  const path = [];
                  let p = el;
                  while ((p = p.parentElement)) {
                    const head = p.querySelector(':scope > .ant-collapse-header');
                    if (head) path.unshift(clean(head.innerText));
                  }
                  if (!path.join(' / ').toLowerCase().includes('collocation')) return;
                  out.push({index, path: path.join(' / '), name});
                });
                return out;
            """) or []

            if section_filter:
                wanted = re.sub(r'\s+', '', section_filter).lower()
                filtered = [c for c in candidates
                            if wanted and wanted in re.sub(r'\s+', '', c.get('path', '')).lower()]
                if filtered:
                    candidates = filtered
                else:
                    print(f"    搭配学习页：没有 section 为「{section_filter}」的 Learning，改用全部候选")

            if not candidates:
                print("    搭配学习页：本单元没有 Collocation / Learning，跳过")
                return False
            print(f"    搭配学习页：本单元有 {len(candidates)} 个候选 Learning，按内容匹配挑一个")

            reference = self._context_reference_tokens()
            best_text, best_score, best_path = "", -1.0, ""
            for order, cand in enumerate(candidates[:3]):
                if order > 0 and not open_unit():
                    break
                clicked = self.driver.execute_script("""
                    const index = arguments[0];
                    const all = [...document.querySelectorAll('[class*="taskItemInnerLayout"]')];
                    const el = all[index];
                    if (!el) return false;
                    const nameEl = el.querySelector('[class*="taskTypeName"]');
                    let target = nameEl || el;
                    for (let i = 0; i < 3 && target; i++) { target.click(); target = target.parentElement; }
                    return true;
                """, cand['index'])
                if not clicked:
                    continue
                time.sleep(11)
                text = self.driver.execute_script("""
                    const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                    let text = clean(document.body.innerText);
                    const cut = text.indexOf('目录笔记');
                    if (cut > 200) text = text.slice(0, cut);
                    return text.slice(0, 7000);
                """) or ""
                if len(text) < 120:
                    continue
                score = self._text_overlap(reference, text)
                print(f"      候选 {cand['path']} → 重合度 {score:.2f}（{len(text)} 字符）")
                if score > best_score:
                    best_text, best_score, best_path = text, score, cand['path']
                if score >= 0.35:
                    break

            if not best_text:
                print("    搭配学习页：没抓到有效正文，跳过")
                return False

            self.ai_client.add_passage_if_new(best_text)
            print(f"    搭配学习页：采用 {best_path}（重合度 {best_score:.2f}，{len(best_text)} 字符）")
            self._loaded_reading_units.add(cache_key)
            return True
        except Exception as exc:
            print(f"    搭配学习页读取失败（不影响作答）: {str(exc)[:80]}")
            return False
        finally:
            try:
                if original:
                    self.driver.close()
                    self.driver.switch_to.window(original)
            except Exception:
                pass

    def _context_reference_tokens(self) -> set:
        """当前页面的词集合，用来判断哪一份 Learning 属于这道题。"""
        text = getattr(self, '_context_reference_text', "") or ""
        if not text:
            try:
                text = self.driver.find_element(By.TAG_NAME, 'body').text or ""
            except Exception:
                text = ""
        return {w for w in re.findall(r"[A-Za-z]{5,}", text.lower())}

    @staticmethod
    def _text_overlap(reference: set, text: str) -> float:
        if not reference:
            return 0.0
        tokens = {w for w in re.findall(r"[A-Za-z]{5,}", (text or '').lower())}
        if not tokens:
            return 0.0
        return len(reference & tokens) / max(1, len(reference))

    # ---- Vocabulary 复用与录音题稿件 -------------------------------------
    def _vocab(self) -> VocabularyStore:
        """拿词汇缓存（测试或半初始化的 solver 上也能安全用）。"""
        store = getattr(self, 'vocab_store', None)
        if store is None:
            store = VocabularyStore()
            self.vocab_store = store
        return store

    def _tts(self) -> TTSEngine:
        engine = getattr(self, 'tts_engine', None)
        if engine is None:
            engine = TTSEngine()
            self.tts_engine = engine
        return engine

    def _needs_vocabulary(self, questions: List[Question]) -> bool:
        """这些题要不要 Vocabulary 对照表（中文括注填空 / 无原声口语稿）。"""
        for q in questions or []:
            if getattr(q, 'needs_speech_script', False):
                return True
            if getattr(q, 'q_type', None) in (QuestionType.FILL_IN, QuestionType.BANKED_CLOZE,
                                              QuestionType.TEXT) and blank_cjk_hints(q):
                return True
        return False

    def _harvest_from_vocabulary_tab(self) -> Optional[bool]:
        """当前在 Exercise 子标签时，先切到同任务的 Vocabulary 抓一遍对照再回来。

        子步骤本来就是二级 Tab（`.pc-header-tasks-row > .pc-task`），批量处理时会
        依次走到；但「快速处理当前页」只停在一个 Tab 上，这时对照表还是空的，答案就
        只能靠模型猜。抓一次 Vocabulary 再切回来，Exercise 1 的填空能逐字对上。
        """
        if self._should_stop():
            return False
        try:
            tabs = self._get_level2_tabs()
        except Exception:
            return False
        if len(tabs) < 2:
            return False

        def title_of(tab: Dict) -> str:
            return str(tab.get('title') or '').strip().lower()

        vocab_tab = next((tab for tab in tabs if title_of(tab) == 'vocabulary'), None)
        if vocab_tab is None:
            return False
        current = next((tab for tab in tabs if tab.get('is_active')), None)
        if current is None or title_of(current) == 'vocabulary':
            return False

        print("    缓存里没有 Vocabulary 对照表，先切过去抓一遍...")
        if not WebDriverHelper.safe_click(self.driver, vocab_tab['element']):
            print("    切换 Vocabulary 失败，继续用模型作答")
            return False
        if self.stop_requested.wait(2.5):
            return None
        self._harvest_vocabulary()
        if not WebDriverHelper.safe_click(self.driver, current['element']):
            print("    切回原标签失败，请留意页面是否停在 Vocabulary")
            return None
        if self.stop_requested.wait(2.0):
            return None
        return True

    def _harvest_vocabulary(self) -> int:
        """把当前页 Vocabulary 里的中英对照收进缓存（跨页、跨任务都保留）。"""
        if self._should_stop():
            return 0
        try:
            rows = self.driver.execute_script(VOCAB_PAIRS_JS) or []
        except Exception as exc:
            logger.debug(f"读取词汇对照失败: {str(exc)[:80]}")
            return 0
        if not isinstance(rows, (list, tuple)):
            return 0
        added = self._vocab().add_rows(rows)
        if added:
            print(f"    词汇表更新 {added} 条（累计 {len(self._vocab())} 条）")
        return added

    def _speaking_script_for(self, q: Question, directions: str = "") -> str:
        """主观录音题：让模型写一段可直接朗读的英文口语稿。"""
        if self._should_stop():
            return ""
        images = q.element.find_elements(
            By.XPATH,
            "ancestor::div[contains(@class,'p-oral-personal-state')][1]"
            "//div[contains(@class,'oral-personal-state-sentence-container')]//img")
        screenshot = None
        if images:
            if not self.config.screenshot:
                print("    口语题需要识别图片，请在配置中启用截图并使用支持图片的模型")
                return ""
            try:
                screenshot = images[0].screenshot_as_png
            except Exception:
                print("    口语题图片截图失败，停止作答")
                return ""
            if not screenshot:
                print("    口语题图片截图为空，停止作答")
                return ""
        prompt = self.prompt_builder.build_speaking_script(
            q, directions, target_seconds=self.executor.RECORD_MAX_SECONDS * 0.85)
        response = self.ai_client.ask(prompt, stop_requested=self.stop_requested,
                                      screenshot=screenshot, require_image=bool(images))
        if not response or self._should_stop():
            print(f"    题目 {q.number}: 没有拿到口语稿")
            return ""
        script = self._clean_speech_script(response)
        if not script:
            print(f"    题目 {q.number}: 口语稿清洗后为空")
        else:
            print(f"    题目 {q.number}: 口语稿 {len(script.split())} 词")
        return script

    @staticmethod
    def _clean_speech_script(text: str) -> str:
        value = (text or '').strip()
        value = re.sub(r'^```[a-zA-Z]*\s*|\s*```$', '', value).strip()
        value = re.sub(r'^(口语稿|参考答案|Answer|Script)\s*[:：]\s*', '', value, flags=re.I)
        value = re.sub(r'[*_`#>]+', ' ', value)
        return re.sub(r'\s+', ' ', value).strip()

    def _repair_questions(self, questions: List[Question], directions: str = "") -> int:
        """上一轮没填好的题，带着更严格的格式要求再答一次。"""
        if not questions or self._should_stop():
            return 0
        prompt = self.prompt_builder.build(questions, directions)
        prompt += ("\n\n【修正要求】上一轮这些题没有填好：答案格式不符合题型或没有写进输入框。"
                   "请严格按题型作答——选项/匹配题只输出字母（如 1.B），填空题每空一个答案，"
                   "文本题给完整段落，不要解释、不要重复题号之外的文字。")
        response = self.ai_client.ask(prompt, stop_requested=self.stop_requested)
        if not response or self._should_stop():
            return 0

        repaired = 0
        for q in questions:
            if self._should_stop():
                break
            if q.q_type in [QuestionType.SINGLE_CHOICE, QuestionType.LISTENING_CHOICE,
                            QuestionType.VIDEO_CHOICE, QuestionType.MULTIPLE_CHOICE,
                            QuestionType.MATCHING, QuestionType.VOCABULARY_TEST]:
                ans = self._extract_single_answer(response, q.number)
                if not ans:
                    ans = response
            elif q.q_type == QuestionType.SORTING and len(questions) > 1:
                ans = self._extract_single_answer(response, q.number)
            else:
                ans = response
            try:
                if ans and q.q_type == QuestionType.SORTING:
                    letters = [opt.letter for opt in q.options]
                    order = self.executor._parse_sorting_order(ans, letters)
                    corrected = self._reorder_sorting_by_transcript(q, order)
                    if corrected and corrected != order:
                        ans = ' '.join(corrected)
                if ans and self.executor.execute(q, ans):
                    repaired += 1
            except Exception as exc:
                print(f"    修正题目 {q.number} 失败: {str(exc)[:60]}")
        return repaired

    def _process_tab_with_accumulation(self, tab_name: str, l1_idx: int, l2_idx: int) -> bool:
        """处理Tab - 新版 ipublish 直接走专用流程，旧版 ucontent 保留按需预处理。"""

        if self._should_stop():
            return False
        if self._is_publish_page():
            return self._process_current_tab_content(
                self.ai_client.current_chapter_id or "unknown", tab_name, l1_idx, l2_idx
            )
        # 进旧版任务页后先清掉首次引导气泡（.know-box .iKnow），避免遮挡多选题复选框或下拉触发器
        if WebDriverHelper.dismiss_onboarding_tips(self.driver):
            if self.stop_requested.wait(0.25):
                return False
        # 若任务页打开时已处于「上次交卷后的查看态」（无提交按钮但有重做/再做一次按钮），
        # 先切回可作答态，再做音视频预处理与题面解析。
        self._exit_review_mode_if_needed()
        if self._should_stop():
            return False
        skip_asr = self._skip_transcription(tab_name)
        prefetch_holder = {}
        prefetch_signature = ""
        prepared = self._prepare_media_prefetch(skip_asr)
        if prepared:
            _, candidates, directions, prefetch_signature = prepared

        def on_transcript(transcript, *, from_video=False):
            nonlocal prepared, candidates, directions, prefetch_signature
            if not transcript or self._should_stop():
                return
            if not prepared:
                # 有些页面播放前题目未挂载，转写完成后、播放前再解析一次。
                prepared = self._prepare_media_prefetch(False)
                if not prepared:
                    return
                _all, candidates, directions, prefetch_signature = prepared
            if from_video or not self._sorting_needs_video(candidates, directions):
                prefetch_holder['answer'] = self._start_answer_prefetch(candidates, directions)

        video_transcript = self._preprocess_video_if_needed(
            tab_name, l1_idx, l2_idx, skip_transcription=skip_asr,
            on_transcript=lambda transcript: on_transcript(transcript, from_video=True))
        if self._should_stop():
            return False
        self._preprocess_audio_if_needed(tab_name, l1_idx, l2_idx,
                                         skip_transcription=skip_asr,
                                         on_transcript=on_transcript)
        if self._should_stop():
            return False

        current_passage = self._extract_passage()
        if self._should_stop():
            return False
        if current_passage:
            self.ai_client.add_passage_if_new(current_passage)

        return self._process_current_tab_content(
            self.ai_client.current_chapter_id or "unknown", tab_name, l1_idx, l2_idx,
            prefetch=prefetch_holder.get('answer'),
            prefetch_signature=prefetch_signature,
            initial_media_done=not skip_asr,
            initial_video_transcript=video_transcript,
        )

    def _prepare_media_prefetch(self, skip_asr: bool = False):
        """视频页先解析题目，等转写完成就在后台问 AI，与播放并行。"""
        # 即使页面被判定为“无需作答”，视频播完后也可能出现题目；
        # 先照常尝试解析，解析不到再在转写完成回调里重试一次。
        if not (self._has_video_on_page() or self._has_audio_on_page()):
            return None
        try:
            questions, directions = self.parser.parse_all()
        except Exception as exc:
            print(f"    预取解析题目失败: {str(exc)[:60]}")
            return None
        excluded = {
            QuestionType.VIDEO, QuestionType.AUDIO,
            QuestionType.VOCABULARY_FLASHCARD, QuestionType.SELF_CHECK,
            QuestionType.DISCUSSION_BOARD, QuestionType.RECORD,
            QuestionType.ORAL_ALOUD, QuestionType.MY_VOICE_TEXT,
        }
        if any(q.q_type in (QuestionType.RECORD, QuestionType.ORAL_ALOUD,
                            QuestionType.MY_VOICE_TEXT) for q in questions):
            return None
        candidates = [q for q in questions if q.q_type not in excluded]
        if not candidates:
            return None
        large_fill = any(
            q.q_type in (QuestionType.FILL_IN, QuestionType.BANKED_CLOZE)
            and len(q.inputs) > 8 and not is_expression_bank_question(q)
            for q in candidates)
        if large_fill:
            return None
        signature = self._generate_questions_signature(questions)
        return questions, candidates, directions, signature

    def _start_answer_prefetch(self, questions, directions) -> Optional[AnswerPrefetch]:
        # 重试改错轮次需要携带 _retry_hint 同步请求，不启动无提示的后台预取，防止污染对话历史
        if getattr(self, '_retry_hint', ''):
            return None
        prompt = self.prompt_builder.build(questions, directions)
        if not prompt:
            return None
        shot = self._question_screenshot(questions)
        print(f"    后台预取答案（{len(questions)} 题，与视频播放并行）...")
        return AnswerPrefetch(self.ai_client, prompt, shot, self.stop_requested)

    def _material_corpus(self) -> str:
        parts = []
        for msg in getattr(self.ai_client, 'material_messages', []) or []:
            text = msg.get('content', '') if isinstance(msg, dict) else ''
            if text:
                parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        value = (text or '').lower().replace('’', "'").replace('‘', "'")
        value = re.sub(r'[^a-z0-9%$.\s-]', ' ', value)
        return re.sub(r'\s+', ' ', value).strip()

    def _reorder_sorting_by_transcript(self, q: Question, order: List[str]) -> List[str]:
        if not order:
            return order
        corpus = self._normalize_for_match(self._material_corpus())
        if not corpus:
            return order
        positions = {}
        for letter in order:
            opt = next((o for o in q.options if o.letter == letter), None)
            text = self._normalize_for_match(opt.text if opt else '')
            if not text:
                return order
            pos = corpus.find(text)
            if pos < 0 and len(text.split()) > 8:
                pos = corpus.find(' '.join(text.split()[:8]))
            if pos >= 0:
                positions[letter] = pos
        if len(positions) != len(order):
            positions = self._keyword_positions(corpus, q, order)
        if len(positions) != len(order) or len(set(positions.values())) != len(positions):
            return order
        corrected = sorted(order, key=lambda letter: positions[letter])
        if corrected != order:
            print(f"    按原文顺序校正排序: {order} -> {corrected}")
        return corrected

    def _keyword_positions(self, corpus: str, q: Question, order: List[str]) -> Dict[str, int]:
        """选项常是转写的改写，用实词重合度找最可能的原文句。"""
        sentences = [s for s in re.split(r'(?<=[.!?])\s+', corpus) if s.strip()]
        if not sentences:
            return {}
        stop = {'the', 'and', 'that', 'with', 'from', 'this', 'have', 'were', 'their',
                'they', 'into', 'such', 'when', 'what', 'which', 'been', 'also', 'more',
                'some', 'many', 'other', 'after', 'before', 'over', 'than', 'them'}
        result = {}
        for letter in order:
            opt = next((o for o in q.options if o.letter == letter), None)
            words = {w for w in self._normalize_for_match(opt.text if opt else '').split()
                     if len(w) > 2 and w not in stop}
            if len(words) < 2:
                return {}
            best_idx, best_score = -1, 0.0
            for idx, sentence in enumerate(sentences):
                overlap = len(words & set(sentence.split()))
                score = overlap / len(words)
                if score > best_score:
                    best_idx, best_score = idx, score
            if best_idx < 0 or best_score < 0.34:
                return {}
            result[letter] = best_idx
        return result

    @staticmethod
    def _sorting_needs_video(questions: List[Question], directions: str) -> bool:
        prompt = re.compile(
            r'\b(?:watch|view|rewatch)\b[^.!?\n]{0,60}\b(?:video|clip|footage|podcast|interview|episode|film|movie|documentary|talk|part\s*\d+)\b'
            r'|\b(?:in|from|based on|according to)\s+(?:the|this)\s+(?:video|clip|footage|podcast|interview|episode)\b'
            r'|(?:观看|根据)[^。！？\n]{0,20}(?:视频|播客|访谈|短片|片段)', re.I)
        return any(q.q_type == QuestionType.SORTING and
                   prompt.search(f"{directions or ''}\n{q.directions or ''}\n{q.text or ''}")
                   for q in questions)

    def _process_current_tab_content(self, chapter_name: str, tab_name: str, l1_idx: int, l2_idx: int,
                                     prefetch: Optional[AnswerPrefetch] = None,
                                     prefetch_signature: str = "",
                                     initial_media_done: bool = False,
                                     initial_video_transcript: str = "") -> bool:
        if self._should_stop():
            return False
        direction_part = self._generate_content_hash_from_direction()
        if direction_part == "empty":
            direction_part = "no_direction"

        content_hash = f"{chapter_name}|{tab_name}|{l1_idx}|{l2_idx}|{direction_part}"

        print(f"    内容标识: {hashlib.md5(content_hash.encode()).hexdigest()[:16]}...")

        if content_hash in self.processed_hashes:
            print(f"   【跳过】已处理过，跳过")
            return False

        self.processed_hashes.add(content_hash)

        if self._is_publish_page():
            success = self._process_publish_content(tab_name)
            if not success:
                self.processed_hashes.discard(content_hash)
            return success

        page_num = 1
        total_answered = 0
        any_special_handled = False
        last_questions_signature = ""

        while True:
            if self._should_stop():
                print("    已请求停止，当前任务提前结束")
                return False

            if page_num == 1 and not initial_media_done:
                self._exit_review_mode_if_needed()
                if self._should_stop():
                    return False

            if page_num == 1 and initial_media_done:
                video_transcript = initial_video_transcript
            else:
                skip_asr = self._skip_transcription(tab_name)
                video_transcript = self._preprocess_video_if_needed(
                    tab_name, l1_idx, l2_idx, skip_transcription=skip_asr)
                if self._should_stop():
                    return False
                self._preprocess_audio_if_needed(tab_name, l1_idx, l2_idx,
                                                 skip_transcription=skip_asr)
                if self._should_stop():
                    return False
            questions, directions = self.parser.parse_all()
            if self._should_stop():
                return False
            # 首屏 SPA 题目可能异步晚挂载：计分任务或外壳已出时最多等 6 秒复查，避免误判 0 题跳过
            if page_num == 1 and not questions:
                max_waits = 12 if getattr(self, '_current_tab_is_scored', None) is True else 6
                for _ in range(max_waits):
                    if self.stop_requested.wait(0.5):
                        return False
                    if self._exit_review_mode_if_needed():
                        pass
                    questions, directions = self.parser.parse_all()
                    if questions:
                        # 题目晚挂载时，前面的音视频预处理可能因 _page_needs_answer=False 被跳过，这里补做一次
                        if not getattr(self.ai_client, 'material_messages', None):
                            skip_asr_late = self._skip_transcription(tab_name)
                            if not video_transcript:
                                video_transcript = self._preprocess_video_if_needed(
                                    tab_name, l1_idx, l2_idx, skip_transcription=skip_asr_late)
                            if self._should_stop():
                                return False
                            self._preprocess_audio_if_needed(
                                tab_name, l1_idx, l2_idx, skip_transcription=skip_asr_late)
                            if self._should_stop():
                                return False
                        break
            # 若同一小节（如 Exercise 2）自身未挂载音频，但题干明确要求听/看材料且当前上下文为空，
            # 自动回补同小节已缓存的转写，防止模型失聪盲猜。
            section_key = getattr(self, '_current_section_key', None)
            if (page_num == 1 and section_key
                    and not getattr(self.ai_client, 'material_messages', None)
                    and section_key in self._section_transcript_cache):
                dir_low = (directions or '').lower()
                needs_media_ctx = (
                    any(q.q_type in (QuestionType.LISTENING_CHOICE, QuestionType.LISTENING_FILL_IN,
                                     QuestionType.VIDEO_CHOICE, QuestionType.DROPDOWN_SELECT,
                                     QuestionType.SORTING) for q in questions)
                    or any(kw in dir_low for kw in ('listen', 'audio', 'talk', 'video', 'watch', 'view', 'conversation', 'passage'))
                )
                if needs_media_ctx:
                    reused = 0
                    for cached_text in self._section_transcript_cache[section_key]:
                        if self.ai_client.add_audio_transcript_if_new(cached_text):
                            reused += 1
                    if reused:
                        print(f"    已从同小节缓存复用 {reused} 份音视频转写")
            # 先把 Vocabulary 的中英对照收进缓存，Exercise 1 填空题直接复用
            self._harvest_vocabulary()
            # 当前 Tab 是 Exercise、缓存还是空的（快速处理当前页时很常见）：
            # 借同任务的 Vocabulary 子标签抓一遍再切回来，答案就能逐字对上
            if page_num == 1 and not len(self._vocab()) and self._needs_vocabulary(questions):
                switched = self._harvest_from_vocabulary_tab()
                if switched is None:
                    return False
                if switched:
                    questions, directions = self.parser.parse_all()
                    if self._should_stop():
                        return False
                    if prefetch is not None:
                        print("    词汇页切换后，放弃预取答案")
                        prefetch = None
            if not video_transcript and self._sorting_needs_video(questions, directions):
                print("    视频排序题缺少对应的转写，已保留为未完成")
                self._last_task_partial = True
                self.processed_hashes.discard(content_hash)
                return False

            self._last_question_types = {q.q_type for q in questions}
            print(f"\n    处理第 {page_num} 页题目...")
            print(f"    找到 {len(questions)} 个可见题目")

            current_signature = self._generate_questions_signature(questions)

            if current_signature == last_questions_signature and page_num > 1:
                print(f"    题目内容与上次相同，可能已到达最后一页")
                break

            last_questions_signature = current_signature

            special_handled = False
            self_check_handled = False
            for q in questions:
                if self._should_stop():
                    return False
                for handler in self.content_handlers:
                    if self._should_stop():
                        return False
                    if handler.can_handle(q):
                        print(f"     使用 {handler.__class__.__name__} 处理")
                        handled = handler.handle(q)
                        if q.q_type == QuestionType.AUDIO:
                            if not handled or self._should_stop():
                                self.processed_hashes.discard(content_hash)
                                return False
                            special_handled = True
                            any_special_handled = True
                            break
                        if self._should_stop():
                            return False
                        special_handled = True
                        any_special_handled = True
                        if q.q_type == QuestionType.SELF_CHECK:
                            self_check_handled = True
                        if q.q_type in [QuestionType.VOCABULARY_FLASHCARD, QuestionType.VIDEO]:
                            print(f"    特殊内容处理完成")
                            return True
                        if q.q_type == QuestionType.DISCUSSION_BOARD:
                            print(f"    讨论区已处理")
                            return True
                        break

            normal_questions = [q for q in questions if q.q_type not in [
                QuestionType.VOCABULARY_FLASHCARD,
                QuestionType.VIDEO,
                QuestionType.AUDIO,
                QuestionType.SELF_CHECK,
                QuestionType.DISCUSSION_BOARD
            ]]

            total_questions = len(normal_questions)

            # 录音题：有参考音频就用，没有就用 TTS 朗读页面原文。
            # total_questions 保持原值（含录音题），否则「只有录音题」的页面不会触发提交。
            record_questions = [q for q in normal_questions
                                if q.q_type in (QuestionType.RECORD, QuestionType.ORAL_ALOUD)]
            record_answered = 0
            if record_questions:
                print(f"    本页 {len(record_questions)} 道录音题，开始逐条录音...")
                # 一页包含多道独立录音题（如 Vocabulary 6 题）时，后台并发预热原声/TTS 缓存
                record_pool = None
                if len(record_questions) > 1:
                    warmup_jobs = []
                    for rq in record_questions:
                        if rq.q_type != QuestionType.RECORD or rq.needs_speech_script:
                            continue
                        rq_items = list(rq.record_items or [])
                        if not rq_items or rq_items[0].get('role_play') or rq_items[0].get('vocab'):
                            continue
                        rq_texts = self.executor._record_speech_texts(rq, "")
                        for r_idx in range(len(rq_items)):
                            warmup_jobs.append((rq_items, rq_texts, r_idx))
                    if len(warmup_jobs) > 1:
                        record_pool = ThreadPoolExecutor(max_workers=min(3, len(warmup_jobs)))
                        for rq_items, rq_texts, r_idx in warmup_jobs:
                            record_pool.submit(
                                self.executor._prefetch_record_audio, rq_items, rq_texts, r_idx
                            )
                try:
                    for q in record_questions:
                        if self._should_stop():
                            return False
                        needs_tts = q.needs_speech_script or q.q_type == QuestionType.ORAL_ALOUD
                        if needs_tts and not self._tts().enabled:
                            print("    录音题没有原声，TTS 已关闭，跳过该题")
                            continue
                        script = ""
                        if q.q_type == QuestionType.RECORD and q.needs_speech_script:
                            # 页面既没有样音也没有原文（自由口语题），只能让模型写一段
                            script = self._speaking_script_for(q, directions)
                            if not script:
                                continue
                        if self.executor.execute(q, script):
                            record_answered += 1
                finally:
                    if record_pool is not None:
                        record_pool.shutdown(wait=False, cancel_futures=True)
                print(f"    录音题完成 {record_answered}/{len(record_questions)}")
                if record_answered < len(record_questions):
                    print("    录音题未完成，停止当前页")
                    return False
                normal_questions = [q for q in normal_questions
                                    if q.q_type not in (QuestionType.RECORD,
                                                        QuestionType.ORAL_ALOUD)]
                if not normal_questions:
                    total_answered += record_answered
                    print(f"    本页成功填写 {record_answered}/{total_questions} 题")

            if normal_questions:
                print(f"    共 {total_questions} 道题目需要回答")
                success_count = record_answered

                # 空位特别多的填空题（如 28 空的首字母搭配）一次性问模型时，
                # 推理型模型容易把 token 全烧在思考上导致返回空；拆成小块逐段问更稳。
                # 短语变形填空要求「每个表达只用一次」，必须一次性看到全部空位做
                # 全局分配；拆批会让后一批重复选用已用过的表达。
                large_fills = [q for q in normal_questions
                               if q.q_type in (QuestionType.FILL_IN, QuestionType.BANKED_CLOZE)
                               and len(q.inputs) > 8
                               and not is_expression_bank_question(q)]
                for q in large_fills:
                    if self._should_stop():
                        return False
                    if self._answer_chunked_fill(q, directions):
                        success_count += 1
                normal_questions = [q for q in normal_questions if q not in large_fills]

                ai_response = None
                used_prefetch = (prefetch is not None and page_num == 1
                                 and current_signature == prefetch_signature
                                 and not self._retry_hint
                                 and prefetch.materials == tuple(
                                     (m['role'], m['content']) for m in self.ai_client.material_messages))
                if used_prefetch:
                    print(f"    等待播放期间的 AI 预取答案（已运行 {prefetch.elapsed:.1f}s）...")
                    ai_response = prefetch.wait(timeout=180)
                    if ai_response:
                        print(f"    预取答案已就绪（总耗时 {prefetch.elapsed:.1f}s）")
                    else:
                        print("    预取未返回，改为同步请求")
                if not ai_response:
                    prompt = self.prompt_builder.build(normal_questions, directions) if normal_questions else ""
                    if prompt and self._retry_hint:
                        prompt += "\n\n【重做改错提示】\n" + self._retry_hint[:2000]
                    if self._should_stop():
                        return False
                    shot = self._question_screenshot(normal_questions)
                    ai_response = self.ai_client.ask(prompt, stop_requested=self.stop_requested,
                                                     screenshot=shot) if normal_questions else None
                    # 重做选择题时若模型仍顽固复读已失败组合，立即追问（最多 2 次）强制换一个未试过的次优组合
                    if (ai_response and self._retry_hint and len(normal_questions) == 1
                            and normal_questions[0].q_type in (
                                QuestionType.MULTIPLE_CHOICE, QuestionType.SINGLE_CHOICE,
                                QuestionType.LISTENING_CHOICE, QuestionType.VIDEO_CHOICE)):
                        _norm_choice = lambda s: ''.join(sorted(set(re.findall(r'[A-H]', str(s or '').upper()))))
                        _prev_raw = list(getattr(self, '_previous_attempt_answers', []) or [])
                        _tried_combos = sorted({_norm_choice(x) for x in _prev_raw if _norm_choice(x)})
                        for _force_round in range(2):
                            _cand = _norm_choice(self._extract_single_answer(ai_response, normal_questions[0].number))
                            if not (_cand and _cand in _tried_combos) or self._should_stop():
                                break
                            print(f"    重做返回了已失败组合 {_cand}，强制要求 AI 更换未试过的新组合（第 {_force_round + 1}/2 次）...")
                            if hasattr(self.ai_client, 'conversation_history') and isinstance(self.ai_client.conversation_history, list):
                                self.ai_client.conversation_history.clear()
                            _force_prompt = (
                                prompt
                                + f"\n\n【强制纠错】你刚才仍输出了已判错的组合 {_cand}！"
                                + f"以下组合均已提交失败，严禁再选其中任何一个：{', '.join(_tried_combos)}。\n"
                                + "请重新评估最可能因为语音转写误差（如 exhibition/expedition）或人物/限定词判断偏差而选错或漏选的选项，"
                                + "给出你认为最可能正确的一个全新组合（严禁与上述已试组合相同），末尾直接写「答案：XXX」。"
                            )
                            _alt = self.ai_client.ask(_force_prompt, stop_requested=self.stop_requested, screenshot=shot)
                            if _alt:
                                ai_response = _alt
                            else:
                                break
                if self._should_stop():
                    return False
                if ai_response:
                    failed_questions = []

                    for q in normal_questions:
                        if self._should_stop():
                            return False
                        if q.q_type in [QuestionType.SINGLE_CHOICE, QuestionType.LISTENING_CHOICE,
                                        QuestionType.VIDEO_CHOICE, QuestionType.MULTIPLE_CHOICE,
                                        QuestionType.VOCABULARY_TEST]:
                            ans = self._extract_single_answer(ai_response, q.number)
                        elif q.q_type in [QuestionType.BANKED_CLOZE, QuestionType.FILL_IN,
                                          QuestionType.DROPDOWN_SELECT]:
                            ans = ai_response
                        elif q.q_type == QuestionType.SORTING and len(normal_questions) > 1:
                            ans = self._extract_single_answer(ai_response, q.number)
                        else:
                            ans = ai_response

                        if ans:
                            if q.q_type == QuestionType.SORTING:
                                letters = [opt.letter for opt in q.options]
                                order = self.executor._parse_sorting_order(ans, letters)
                                corrected = self._reorder_sorting_by_transcript(q, order)
                                if corrected and corrected != order:
                                    ans = ' '.join(corrected)
                            if self.executor.execute(q, ans):
                                success_count += 1
                                compact_ans = re.sub(r'\s+', ' ', str(ans)).strip()[:80]
                                if compact_ans:
                                    bucket = getattr(self, '_previous_attempt_answers', None)
                                    if isinstance(bucket, list) and compact_ans not in bucket:
                                        bucket.append(compact_ans)
                            else:
                                failed_questions.append(q)
                        else:
                            print(f"    题目 {q.number} 无答案")
                            failed_questions.append(q)

                    if failed_questions:
                        repaired = self._repair_questions(failed_questions, directions)
                        success_count += repaired
                        print(f"    二次修正成功 {repaired}/{len(failed_questions)} 题")

                    total_answered += success_count
                    print(f"    本页成功填写 {success_count}/{total_questions} 题")
                    if success_count != total_questions:
                        print("    本页未完整填写，停止当前任务，请检查答案和选项状态")
                        return False
                elif normal_questions:
                    print("    AI 未返回答案，停止当前任务")
                    return False
                else:
                    total_answered += success_count
                    print(f"    本页成功填写 {success_count}/{total_questions} 题")
                    if success_count != total_questions:
                        print("    本页未完整填写，停止当前任务，请检查答案和选项状态")
                        return False

            if self._should_stop():
                return False
            if self_check_handled and not normal_questions and self.executor.submit():
                self._wait_for_submit_complete()
                if self._should_stop():
                    return False
            next_btn = self._find_next_question_button()
            if next_btn:
                print(f"    点击下一题...")
                pre_click_signature = current_signature

                if self._should_stop():
                    return False
                if not WebDriverHelper.safe_click(self.driver, next_btn):
                    print(f"    点击下一题失败")
                    break

                # 翻页后等 30 秒：录音刚结束时平台还在上传/评分，页面不会立刻切，
                # 5 秒窗口太短（parse_all 一次约 0.7s，只够 7 次）。
                if not self._wait_for_content_change(pre_click_signature, timeout=30):
                    if self._should_stop():
                        return False
                    print(f"    内容未变化，可能已到最后一页")
                    break

                page_num += 1

                if page_num > 50:
                    print(f"    达到最大页数限制，停止")
                    break
                continue

            if self._should_stop():
                return False
            # 注意：分批作答时 normal_questions 会被清空，是否提交要看 total_questions，
            # 否则「做完不交卷」，用户还得手动点提交。
            legacy_unreviewed_url = self._legacy_unreviewed_url() if total_questions else ''
            if total_questions and self.executor.submit():
                submitted = self._wait_for_submit_complete(timeout=18, legacy_unreviewed_url=legacy_unreviewed_url)
                if self._should_stop():
                    return False
                if submitted:
                    print(f"    已自动提交")
                else:
                    self._last_task_partial = True
                    self.processed_hashes.discard(content_hash)
                    return False
            elif total_questions:
                print(f"    没找到可点击的提交按钮，请手动提交")
                self._last_task_partial = True
                self.processed_hashes.discard(content_hash)
                return False
            print(f"    没有更多题目了")
            break
        print(f"    总共回答 {total_answered} 题")
        if total_answered == 0 and not any_special_handled:
            # 防止把「页面未加载完 / 锁死查看态 / 未识别题型」误判为已完成！
            has_interactive_hint = (
                self._find_visible_submit_button() is not None
                or self._has_redo_button()
                or getattr(self, '_current_tab_is_scored', None) is True
            )
            if has_interactive_hint:
                print("    本页未识别到任何有效作答（0 题），拒绝误标已完成，保留勾选")
                self._last_task_partial = True
                self.processed_hashes.discard(content_hash)
                return False
            self._last_task_empty = True
            return False
        return True

    # ---- 新版 ipublish 播放器（course-v3）---------------------------------
    def _is_publish_page(self) -> bool:
        """当前页面是否为新版 ipublish 播放器；无 driver 时安全返回 False。"""
        driver = getattr(self, 'driver', None)
        if driver is None:
            return False
        try:
            return PublishPlayer.is_publish_page(driver)
        except Exception:
            return False

    def _process_publish_content(self, task_name: str = '') -> bool:
        """只处理当前打开的目录任务；批量任务切换由勾选列表控制。"""
        self._last_task_partial = False
        print("    检测到新版 ipublish 播放器（course-v3），启用专用适配流程")
        self.publish_executor.dismiss_rules_popup()
        if not self._publish_prepare_scope(task_name):
            return False

        quiz_result = self._publish_process_quiz()
        if quiz_result is not None:
            return quiz_result
        videos = self._publish_play_task_media(task_name)
        if videos is None:
            return False
        reading = self._publish_read_task()
        if reading is None:
            return False

        handled = 0
        skipped = 0
        rejected = 0
        idle_reveals = 0
        while not self._should_stop():
            result = self._publish_process_next_group()
            if result == 'error':
                return False
            if result == 'answered':
                handled += 1
                idle_reveals = 0
                if handled >= 80:
                    print("    已处理 80 个题目组，停止当前任务")
                    break
                continue
            if result == 'skipped':
                skipped += 1
                idle_reveals = 0
                continue
            if result == 'rejected':
                rejected += 1
                idle_reveals = 0
                continue
            if result == 'reviewed':
                idle_reveals = 0
                continue

            # 只在当前任务边界内补充懒加载，不点击「下一节」进入未勾选任务。
            self.publish_executor.dismiss_rules_popup()
            if idle_reveals < 25 and self._publish_reveal_more_groups():
                idle_reveals += 1
                continue
            break

        if handled:
            print(f"    新版播放器共提交 {handled} 个题目组")
        if rejected:
            print(f"    另有 {rejected} 个题目组因正确率不足 60% 被平台拒绝提交")
        if skipped:
            print(f"    另有 {skipped} 个题目组题型暂不支持或未渲染，已跳过")
        if not handled and not skipped and not rejected:
            if not videos and not reading and not self._last_task_partial:
                print("    当前任务没有可处理的题目或视频，按纯阅读页处理，跳过本任务")
                self._last_task_empty = True
                return False
            if reading:
                print("    本任务正文已浏览，请以课程目录显示的进度为准")
        if skipped or rejected:
            self._last_task_partial = True
        # 离开任务页前留短暂上报窗口，防止最后一条阅读/完播心跳被切页请求打断
        if (videos or reading or handled) and not self._should_stop():
            self.stop_requested.wait(1.5)
        return not self._should_stop() and not self._last_task_partial

    def _publish_read_task(self):
        """浏览任务正文（含 rich_text_read_group 阅读文章），以触发平台记录阅读进度。"""
        selector = (
            '.editor-content > [data-lexical-node-type="base-paragraph"], '
            '.editor-content > [data-lexical-node-type="highlight-block"], '
            '.editor-content > [data-lexical-node-type="list"], '
            '.editor-content > [data-lexical-node-type="table"], '
            '.editor-content > ul, .editor-content > ol, '
            '[data-lexical-node-type="collapsible-content"] [data-lexical-node-type="base-paragraph"], '
            '[data-lexical-node-type="collapsible-content"] p, '
            '[data-ipublish-question-group-type="rich_text_read_group"] [data-lexical-node-type="base-paragraph"], '
            '[data-ipublish-question-group-type="rich_text_read_group"] .editor-content > *, '
            '[data-ipublish-question-group-type="rich_text_read_group"] p'
        )
        # 展开当前任务范围内的阅读折叠面板（如 Writing skill 里的优秀范文/参考材料），确保平台记录阅读进度
        try:
            with self._publish_scan_nodes('[data-lexical-node-type="collapsible-head"], .ant-collapse-header') as heads:
                for head in (heads or []):
                    try:
                        in_q = self.driver.execute_script(
                            'return !!arguments[0].closest("[data-ipublish-question-group-type]");', head)
                        if in_q:
                            continue
                        is_collapsed = self.driver.execute_script('''
                            const h = arguments[0], p = h.parentElement;
                            if (!p) return false;
                            const c = p.querySelector('[data-lexical-node-type="collapsible-content"], .ant-collapse-content');
                            if (!c) return false;
                            const s = getComputedStyle(c);
                            return c.hasAttribute('hidden') || s.display === 'none' || s.contentVisibility === 'hidden'
                                || c.classList.contains('ant-collapse-content-inactive');
                        ''', head)
                        if is_collapsed:
                            WebDriverHelper.safe_click(self.driver, head)
                            if self.stop_requested.wait(0.35):
                                return None
                    except Exception:
                        continue
        except Exception:
            pass
        read_groups = '[data-ipublish-question-group-type="rich_text_read_group"]'
        wait_deadline = time.monotonic() + 3.0
        while not self._should_stop():
            with self._publish_scan_nodes(selector, fallback_selector=read_groups) as nodes:
                if nodes is None:
                    return None
                readable = []
                for node in nodes:
                    try:
                        if not (node.get_attribute('textContent') or '').strip():
                            continue
                        in_question = self.driver.execute_script('''
                            const group = arguments[0].closest('[data-ipublish-question-group-type]');
                            return !!group && group.dataset.ipublishQuestionGroupType !== 'rich_text_read_group';
                        ''', node)
                        if not in_question:
                            readable.append(node)
                    except (StaleElementReferenceException, NoSuchFrameException, TimeoutException):
                        continue
                if readable:
                    count = self._publish_scroll_readable(readable)
                    if count is None:
                        return None
                    self._publish_open_missing_read_items()
                    return count
            if time.monotonic() >= wait_deadline:
                self._publish_open_missing_read_items()
                return 0
            if self.stop_requested.wait(0.5):
                break
        return None

    def _publish_open_missing_read_items(self) -> None:
        """针对平台学习记录里明确缺失的阅读子项（如外部范文链接/PDF/卡片），精准定位并触发阅读上报。"""
        task = getattr(self, '_current_directory_task', None) or {}
        missing_titles = []
        for g in task.get('_record_groups') or []:
            if g.get('kind') in ('read', 'reading', 'article', 'doc', 'file') and g.get('progress') in (None, 0):
                name = (g.get('name') or '').strip()
                if name:
                    missing_titles.append(name)
        if not missing_titles or self._should_stop():
            return
        with PublishFrameContext(self.driver) as context:
            if not context.entered:
                return
            for title in missing_titles[:4]:
                if self._should_stop():
                    return
                needle = re.sub(r'\s+', ' ', title).strip()[:32].casefold()
                if not needle:
                    continue
                try:
                    hit = self.driver.execute_script("""
                        const needle = arguments[0];
                        const clean = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        let best = null, bestLen = 1e9;
                        for (const el of document.querySelectorAll('a, button, p, div, span, li, section')) {
                            if (!el.getClientRects || !el.getClientRects().length) continue;
                            const txt = clean(el.innerText || el.textContent);
                            if (!txt || !txt.includes(needle)) continue;
                            if (txt.length < bestLen) {
                                best = el;
                                bestLen = txt.length;
                            }
                        }
                        if (!best) return null;
                        best.scrollIntoView({block: 'center'});
                        try { window.dispatchEvent(new Event('scroll')); } catch (e) {}
                        const clickTarget = best.matches('a[href], button, [role="button"]') ? best
                            : (best.querySelector('a[href], button, [role="button"], [class*="link"], [class*="file"], [class*="pdf"], [class*="preview"]')
                               || best.closest('a[href], button, [role="button"], [data-lexical-node-type*="link"], [data-lexical-node-type*="file"], [data-lexical-node-type*="pdf"]')
                               || best);
                        return {
                            el: best,
                            clickEl: clickTarget,
                            tag: clickTarget.tagName,
                            cls: String(clickTarget.className || '').slice(0, 60),
                            href: clickTarget.getAttribute ? (clickTarget.getAttribute('href') || '') : '',
                            nodeType: clickTarget.getAttribute ? (clickTarget.getAttribute('data-lexical-node-type') || '') : '',
                        };
                    """, needle)
                    if not hit:
                        print(f"    未在正文找到缺失阅读项「{title[:36]}」的匹配元素")
                        continue
                    print(
                        f"    定位到缺失阅读项「{title[:36]}」: "
                        f"<{hit.get('tag')}> type={hit.get('nodeType')!r} href={bool(hit.get('href'))}"
                    )
                    prev_handles = list(self.driver.window_handles)
                    orig_handle = self.driver.current_window_handle
                    click_el = hit.get('clickEl') or hit.get('el')
                    if click_el is not None:
                        WebDriverHelper.safe_click(self.driver, click_el)
                        if self.stop_requested.wait(3.5):
                            return
                        new_handles = [h for h in self.driver.window_handles if h not in prev_handles]
                        for nh in new_handles:
                            try:
                                self.driver.switch_to.window(nh)
                                self.stop_requested.wait(2.0)
                                self.driver.close()
                            except Exception:
                                pass
                        if new_handles:
                            try:
                                self.driver.switch_to.window(orig_handle)
                                PublishFrameContext(self.driver).enter()
                            except Exception:
                                pass
                        else:
                            # 若页内弹出了阅读预览弹窗，停留后顺手关闭
                            try:
                                self.driver.execute_script("""
                                    const closeBtn = document.querySelector(
                                        '.ant-modal-close, [aria-label="Close"], [class*="modal"] [class*="close"]'
                                    );
                                    if (closeBtn && closeBtn.getClientRects && closeBtn.getClientRects().length) {
                                        try { closeBtn.click(); } catch (e) {}
                                    }
                                """)
                            except Exception:
                                pass
                except Exception as exc:
                    print(f"    触发缺失阅读项失败: {str(exc)[:80]}")

    def _publish_scroll_readable(self, readable: List[Any]) -> Optional[int]:
        # 长文章最多均匀抽样 8 个关键段落平滑滚动（首尾必含），兼顾浏览到位与效率
        if len(readable) > 8:
            indices = sorted({int(round(i * (len(readable) - 1) / 7)) for i in range(8)})
            targets = [readable[i] for i in indices]
        else:
            targets = readable
        count = 0
        started = time.monotonic()
        dwell = 1.5 if len(targets) >= 4 else 2.5
        for idx, node in enumerate(targets):
            try:
                block = 'end' if idx == len(targets) - 1 else 'center'
                self.driver.execute_script('''
                    const el = arguments[0], blk = arguments[1];
                    el.scrollIntoView({block: blk});
                    try { window.dispatchEvent(new Event('scroll')); } catch (e) {}
                ''', node, block)
            except Exception:
                continue
            if self.stop_requested.wait(dwell):
                return None
            count += 1
        # 补足最低 6.5 秒阅读停留，给 ipublish 上报阅读心跳的完整窗口
        if count > 0:
            remain = 6.5 - (time.monotonic() - started)
            if remain > 0 and self.stop_requested.wait(remain):
                return None
        return count

    # ---- 新版音视频：稳定指纹持久化、三级自愈、原生倍速 ----------------------
    def _media_watch_state_file(self) -> str:
        override = getattr(self, '_media_watch_state_path', '')
        if override:
            return override
        return os.path.join(BASE_DIR, 'logs', 'media_watch_state.json')

    def _load_watched_media_store(self) -> Dict[str, Any]:
        path = self._media_watch_state_file()
        try:
            if os.path.isfile(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {'watched': {}}

    def _save_watched_media_store(self, store: Dict[str, Any]) -> None:
        path = self._media_watch_state_file()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            watched = store.get('watched') or {}
            # 限制最多保留最近 300 条完播记录，防止文件无限增长
            if len(watched) > 300:
                ordered = sorted(watched.items(), key=lambda kv: kv[1].get('ts', 0))[-300:]
                store['watched'] = dict(ordered)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(store, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def _publish_media_fingerprint(scope_key: str, media_url: str, duration: float, index: int) -> str:
        """去签名的稳定媒体指纹：即使刷新页面后 CDN 签名参数变化也能准确命中。"""
        url = (media_url or '').strip()
        stable_path = ''
        if url and not url.startswith('blob:'):
            parsed = urlsplit(url)
            if parsed.netloc or parsed.path:
                stable_path = f"{parsed.netloc}{parsed.path}"
        dur_bucket = int(round(float(duration or 0)))
        raw = f"{scope_key}|{stable_path or 'media'}|{dur_bucket}|{index}"
        return hashlib.md5(raw.encode('utf-8')).hexdigest()

    def _mark_media_watched(self, scope_key: str, task_title: str, fp: str) -> None:
        store = self._load_watched_media_store()
        watched = store.setdefault('watched', {})
        watched[fp] = {
            'scope': scope_key,
            'task': (task_title or '').strip(),
            'ts': int(time.time()),
        }
        self._save_watched_media_store(store)

    def clear_watched_media_for_task(self, task_title: str, *, scope_key: str) -> int:
        """只清理当前课件锚点下的旧缓存，供平台明确未登记时重试。"""
        skipped_set = getattr(self, '_all_skipped_media_tasks', None)
        if not scope_key or not skipped_set or scope_key not in skipped_set:
            return 0
        skipped_set.discard(scope_key)
        session_played = getattr(self, '_session_played_media_tasks', None) or set()
        if scope_key in session_played:
            return 0
        store = self._load_watched_media_store()
        watched = store.get('watched') or {}
        removed = 0
        for fp in list(watched.keys()):
            entry = watched.get(fp) or {}
            if entry.get('scope') == scope_key:
                watched.pop(fp, None)
                removed += 1
        if removed:
            self._save_watched_media_store(store)
            print(f"    平台核验「{task_title}」仍未完成，已清除该任务 {removed} 条本地已播缓存，下轮完整复播")
        return removed

    def _publish_recorded_media_kinds(self, scoped: List[Dict]) -> set:
        """仅使用当前目录任务的已登记整组媒体。网页和记录无法逐段使用相同 ID。"""
        task = getattr(self, '_current_directory_task', None) or {}
        scope = getattr(self, '_publish_scope', None)
        if not task.get('_record_matched') or not scope or not scope.start_anchor or scope.block_only:
            return set()
        directory = urlsplit(task.get('_directory_url') or '')
        resource = directory.path.rstrip('/').rsplit('/', 1)[-1]
        player = urlsplit(getattr(self, '_publish_scope_url', ''))
        if (directory.hostname != 'ucloud.unipus.cn' or not resource.isdigit()
                or parse_qs(player.query).get('courseResourceId') != [resource]):
            return set()

        groups = [g for g in task.get('_record_groups') or [] if g['kind'] in ('video', 'audio')]
        totals = Counter(g['kind'] for g in groups)
        completed = Counter(g['kind'] for g in groups if g['progress'] == 100)
        eligible = {kind for kind, total in totals.items() if completed[kind] == total}
        if not eligible:
            return set()
        try:
            standalone = self.driver.execute_script("""
                return arguments[0].filter(v => !v.closest('[data-ipublish-question-group-type]'))
                    .map(v => v.dataset.lexicalNodeType === 'insert-audio' ? 'audio' : 'video');
            """, [row['element'] for row in scoped]) or []
        except WebDriverException:
            return set()
        page_counts = Counter(standalone)
        matching = {kind for kind in eligible if page_counts[kind] == totals[kind]}
        if matching != eligible:
            print("    平台媒体记录与当前课件段数不一致，继续按播放器状态核对")
        return matching

    def _publish_try_native_speed(self, wrapper, target_rate: float) -> Tuple[bool, float]:
        """仅在播放器界面自身公开提供合法倍速菜单时点击切换；无倍速控件时尊重原生 1×。"""
        if target_rate <= 1.0:
            return False, 1.0
        labels = [f"{target_rate:g}x", f"{target_rate:g}X", f"{target_rate:.2f}x".rstrip('0').rstrip('.') + "x",
                  f"{target_rate:g}倍", f"{target_rate:g}"]
        try:
            # 1. 查找播放器容器内可见的倍速按钮（如「倍速」「1.0x」「1x」）
            speed_btn = self.driver.execute_script("""
                const root = arguments[0];
                const isVis = el => {
                    if (!el || !el.getClientRects || !el.getClientRects().length) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) > 0;
                };
                const cands = root.querySelectorAll('button, div, span, li');
                for (const el of cands) {
                    if (!isVis(el) || el.children.length > 2) continue;
                    const txt = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
                    if (/^(倍速|1\\.0x|1x|1\\.0X|1X|1\\.0倍)$/i.test(txt)) return el;
                }
                return null;
            """, wrapper)
            if speed_btn is None:
                return False, 1.0
            WebDriverHelper.safe_click(self.driver, speed_btn)
            time.sleep(0.35)
            # 2. 在弹出的合法倍速菜单中点击精确匹配档位
            option_el = self.driver.execute_script("""
                const root = arguments[0], targets = new Set(arguments[1].map(x => x.toLowerCase()));
                const isVis = el => {
                    if (!el || !el.getClientRects || !el.getClientRects().length) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) > 0;
                };
                const scopes = [root, document.body];
                for (const scope of scopes) {
                    const cands = scope.querySelectorAll('li, button, div, span');
                    for (const el of cands) {
                        if (!isVis(el) || el.children.length > 2) continue;
                        const txt = (el.innerText || el.textContent || '').replace(/\\s+/g, '').toLowerCase();
                        if (targets.has(txt)) return el;
                    }
                }
                return null;
            """, wrapper, labels)
            if option_el is None:
                return False, 1.0
            if WebDriverHelper.safe_click(self.driver, option_el):
                time.sleep(0.25)
                actual = self.driver.execute_script(
                    "const v = arguments[0].querySelector('video, audio'); return v ? Number(v.playbackRate || 1) : 1;",
                    wrapper)
                if actual and abs(float(actual) - target_rate) <= 0.1:
                    return True, float(actual)
        except Exception:
            pass
        return False, 1.0

    def _publish_play_task_media(self, task_name: str = ''):
        """播放独立音视频和已提交题组中的材料：支持稳定指纹断点续播、三级自愈与刷新重连。"""
        scope = getattr(self, '_publish_scope', None)
        scope_title = (getattr(scope, 'title', '') or task_name or '').strip()
        scope_anchor = getattr(scope, 'start_anchor', '')
        scope_key = scope_anchor or scope_title
        task = getattr(self, '_current_directory_task', None)
        if task is not None and scope_anchor:
            task['_media_scope_key'] = scope_anchor
        target_rate = float(getattr(self, '_video_playback_rate', getattr(getattr(self, 'config', None), 'video_playback_rate', 1.0)) or 1.0)
        max_reloads = 2
        reloads = 0
        total_played = 0
        total_found = 0
        played_this_pass = 0
        skipped_this_pass = 0
        cached_this_pass = 0

        while not self._should_stop():
            # 开播前先在外壳层清理可能滞后弹出的「本单元学习规则」弹窗
            PublishPlayer.reset(self.driver)
            self.publish_executor.dismiss_rules_popup()

            need_reload = False
            with PublishFrameContext(self.driver) as context:
                if not context.entered:
                    return None
                wrappers = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    '[data-lexical-node-type="insert-video"], [data-lexical-node-type="insert-audio"]')
                scoped = self._publish_scoped_groups([{'element': wrapper} for wrapper in wrappers])
                recorded_kinds = self._publish_recorded_media_kinds(scoped)
                watched_store = self._load_watched_media_store().get('watched') or {}
                total_found = 0
                skipped_this_pass = 0
                played_this_pass = 0
                cached_this_pass = 0

                for idx, state in enumerate(scoped):
                    if self._should_stop():
                        return None
                    wrapper = state['element']
                    in_group = self.driver.execute_script(
                        'return !!arguments[0].closest("[data-ipublish-question-group-type]")', wrapper)
                    if in_group:
                        group = self.driver.execute_script('return arguments[0].closest("[data-ipublish-question-group-type]")', wrapper)
                        if not self.publish_parser.group_submitted(group):
                            continue
                    total_found += 1
                    video = None
                    try:
                        tag = 'audio' if wrapper.get_attribute('data-lexical-node-type') == 'insert-audio' else 'video'
                        if not in_group and tag in recorded_kinds:
                            skipped_this_pass += 1
                            print(f'    平台已登记该音视频（第 {total_found} 段），跳过重复播放')
                            continue
                        self.driver.execute_script('''
                            const w = arguments[0];
                            w.scrollIntoView({block:"center"});
                            try { window.dispatchEvent(new Event('scroll')); } catch (e) {}
                        ''', wrapper)
                        # 1. 等待 <video>/<audio> 挂载；若 4s 未挂载，尝试轻点封面/播放触发懒挂载
                        mount_deadline = time.monotonic() + 15.0
                        nudged_mount = False
                        while time.monotonic() < mount_deadline and not self._should_stop():
                            els = wrapper.find_elements(By.TAG_NAME, tag)
                            if els:
                                video = els[0]
                                break
                            if not nudged_mount and (mount_deadline - time.monotonic()) < 11.0:
                                nudged_mount = True
                                try:
                                    self.driver.execute_script('''
                                        const w = arguments[0];
                                        w.scrollIntoView({block:"center"});
                                        const btn = w.querySelector(
                                            'button[aria-label*="play" i], button[title*="播放"], ' +
                                            '[class*="play-btn"], [class*="playBtn"], .vjs-big-play-button, .xgplayer-start'
                                        );
                                        if (btn && btn.getClientRects && btn.getClientRects().length) {
                                            try { btn.click(); } catch (e) {}
                                        }
                                    ''', wrapper)
                                except Exception:
                                    pass
                            if self.stop_requested.wait(0.35):
                                return None
                        if video is None:
                            raise TimeoutError(f"等待 <{tag}> 元素挂载超时")

                        # 2. 主动唤醒 preload="none" / HLS 播放器拉取元数据，避免 duration 长期 NaN 超时
                        try:
                            self.driver.execute_script('''
                                const v = arguments[0];
                                if (!Number.isFinite(v.duration) || v.duration <= 0) {
                                    v.muted = true;
                                    v.preload = 'auto';
                                    if (v.readyState === 0) {
                                        try { v.load(); } catch (e) {}
                                    }
                                }
                            ''', video)
                        except Exception:
                            pass

                        dur_deadline = time.monotonic() + 20.0
                        kicked_play = False
                        duration = 0.0
                        while time.monotonic() < dur_deadline and not self._should_stop():
                            try:
                                dval = self.driver.execute_script(
                                    'return Number.isFinite(arguments[0].duration) && arguments[0].duration || 0',
                                    video)
                                if dval and float(dval) > 0:
                                    duration = float(dval)
                                    break
                            except Exception:
                                pass
                            if not kicked_play and (dur_deadline - time.monotonic()) < 14.0:
                                kicked_play = True
                                try:
                                    self.driver.execute_script('''
                                        const w = arguments[0], v = arguments[1];
                                        v.muted = true;
                                        v.preload = 'auto';
                                        const btn = w && w.querySelector ? w.querySelector(
                                            'button[aria-label*="play" i], button[title*="播放"], ' +
                                            '[class*="play-btn"], [class*="playBtn"], .vjs-big-play-button, .xgplayer-start'
                                        ) : null;
                                        if (btn && btn.getClientRects && btn.getClientRects().length) {
                                            try { btn.click(); } catch (e) {}
                                        } else {
                                            try { const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {}
                                        }
                                    ''', wrapper, video)
                                except Exception:
                                    pass
                            if self.stop_requested.wait(0.4):
                                return None
                        if duration <= 0:
                            raise TimeoutError("等待音视频元数据/时长加载超时")
                        media_url = (wrapper.get_attribute('data-video-url') if tag == 'video' else '') or ''
                        if not media_url:
                            try:
                                media_url = video.get_attribute('src') or ''
                            except Exception:
                                media_url = ''
                        fp = self._publish_media_fingerprint(scope_key, media_url, duration, idx)

                        # 检查是否已完整播完（稳定指纹命中或播放器自身已处于 ended 状态）
                        already_ended = False
                        try:
                            already_ended = bool(self.driver.execute_script(
                                'const v = arguments[0]; return !!v.ended && v.duration > 0 && v.currentTime >= v.duration - 1.5;',
                                video))
                        except Exception:
                            already_ended = False

                        if fp in watched_store or already_ended:
                            if fp in watched_store:
                                cached_this_pass += 1
                            skipped_this_pass += 1
                            print(f'    跳过已完整播完的音视频（第 {total_found} 段，{int(duration)} 秒）')
                            continue

                        applied_rate = 1.0
                        if target_rate > 1.0:
                            ok_speed, actual = self._publish_try_native_speed(wrapper, target_rate)
                            if ok_speed:
                                applied_rate = actual
                            else:
                                print('    当前播放器未开放倍速控件，保持原生 1× 播放')
                        print(f'    播放当前任务音视频（第 {total_found} 段，{int(duration)} 秒，{applied_rate:g}×）')

                        outcome = self._publish_wait_for_media(video, wrapper, duration, applied_rate)
                        if outcome is None:
                            return None
                        if outcome == 'done':
                            played_this_pass += 1
                            total_played += 1
                            self._mark_media_watched(scope_key, scope_title, fp)
                            watched_store[fp] = {'scope': scope_key, 'task': scope_title}
                            if scope_anchor:
                                played_set = getattr(self, '_session_played_media_tasks', None)
                                if played_set is None:
                                    played_set = set()
                                    self._session_played_media_tasks = played_set
                                played_set.add(scope_anchor)
                        elif outcome == 'reload':
                            need_reload = True
                            break
                        else:
                            self._last_task_partial = True
                    except Exception as exc:
                        err_msg = (str(exc).strip() or type(exc).__name__)[:100]
                        if isinstance(exc, (TimeoutError, TimeoutException)) and reloads < max_reloads:
                            print(f'    音视频加载超时（{err_msg}），准备自动刷新重连…')
                            need_reload = True
                            break
                        print(f'    音视频播放失败: {err_msg}，继续检查本任务的其他内容')
                        self._last_task_partial = True
                    finally:
                        try:
                            if video is not None:
                                self.driver.execute_script('arguments[0].pause()', video)
                        except Exception:
                            pass

            if need_reload:
                if reloads < max_reloads and not self._should_stop():
                    reloads += 1
                    print(f'    检测到视频断流或签名过期，正在自动刷新页面续播（第 {reloads}/{max_reloads} 次）…')
                    try:
                        PublishPlayer.reset(self.driver)
                        self.driver.refresh()
                        if self.stop_requested.wait(3.0):
                            return None
                        self.publish_executor.dismiss_rules_popup()
                        if (self._publish_prepare_scope(task_name, timeout=25.0)
                                and self._publish_scope.start_anchor == scope_anchor):
                            continue
                    except Exception as exc:
                        print(f'    自动刷新续播失败: {str(exc)[:80]}')
                self._last_task_partial = True
            break

        # 全部媒体都只靠旧缓存跳过时，允许平台明确未登记后按当前锚点清理。
        if scope_anchor:
            all_skip_set = getattr(self, '_all_skipped_media_tasks', None)
            if all_skip_set is None:
                all_skip_set = set()
                self._all_skipped_media_tasks = all_skip_set
            if total_found > 0 and total_played == 0 and cached_this_pass == total_found:
                all_skip_set.add(scope_anchor)
            else:
                all_skip_set.discard(scope_anchor)

        return None if self._should_stop() else (played_this_pass + skipped_this_pass)

    def _publish_wait_for_media(self, video, wrapper, duration: float, playback_rate: float = 1.0) -> Optional[str]:
        """等待真实播放结束，内置一级自愈（自动续播）与二级断流检测（触发刷新重连）。

        返回 'done' | 'reload' | 'failed' | None(用户取消)。
        """
        if self._should_stop():
            return None
        position = float(self.driver.execute_script('''
            const w = arguments[0], v = arguments[1];
            v.__unipusPlaybackError = null;
            if (!v.ended) {
                v.muted = true;
                v.preload = 'auto';
                // 优先通过播放器自身播放按钮起播，让 React 播放器内部状态同步为 playing，
                // 避免只调原生 v.play() 被播放器定时状态同步反复拉回暂停。
                if (v.paused && w && w.querySelector) {
                    const btn = w.querySelector(
                        'button[aria-label*="play" i], button[title*="播放"], ' +
                        '[class*="play-btn"], [class*="playBtn"], .vjs-big-play-button, .xgplayer-start'
                    );
                    if (btn && btn.getClientRects && btn.getClientRects().length) {
                        try { btn.click(); } catch (e) {}
                    }
                }
                if (v.paused) {
                    const p = v.play();
                    if (p && p.catch) p.catch(error => { v.__unipusPlaybackError = String(error); });
                }
            }
            return v.currentTime || 0;
        ''', wrapper, video) or 0)

        start = time.monotonic()
        last_advance = start
        next_report = start + 30
        recover_attempts = 0
        last_recover_at = start
        last_pause_log_at = 0.0
        pause_recover_streak = 0
        eff_rate = max(0.5, float(playback_rate or 1.0))
        hard_cap = start + max((duration * 3.0) / eff_rate, (duration / eff_rate) + 600.0)
        deadline = min(hard_cap, start + max(0.0, (duration - position) / eff_rate) + 180.0)

        while not self.stop_requested.wait(1):
            state = self.driver.execute_script('''
                const v = arguments[0];
                return {
                    t: v.currentTime || 0,
                    ended: !!v.ended,
                    paused: !!v.paused,
                    readyState: v.readyState || 0,
                    networkState: v.networkState || 0,
                    error: v.__unipusPlaybackError || (v.error && (v.error.message || v.error.code))
                };
            ''', video) or {}
            current = float(state.get('t') or 0)
            if state.get('ended'):
                print(f'    播放结束（{int(current)}/{int(duration)} 秒），等待平台记录完播状态…')
                # 留足 2.5 秒完播心跳窗口，避免紧接着切页取消异步上报请求
                if self.stop_requested.wait(2.5):
                    return None
                return 'done'

            if state.get('error'):
                print(f"    音视频播放报错（{state['error']}），准备刷新重连")
                return 'reload'

            now = time.monotonic()
            if current >= position + 0.25:
                position, last_advance = current, now
                recover_attempts = 0
                # 只要视频仍在真实往前播放，就动态顺延超时窗（不超过 hard_cap），
                # 避免缓冲稍慢的长视频在最后几秒（如 286/298 秒）被硬超时误杀！
                deadline = min(
                    hard_cap,
                    max(deadline, now + max(90.0, (duration - current) / eff_rate + 120.0)),
                )

            stalled = now - last_advance
            is_paused = bool(state.get('paused'))
            ready_state = int(state.get('readyState') or 0)
            # 一级自愈：
            # - 若视频处于暂停态（paused=True）且停滞 >= 1.5s：立即续播（冷却 3s），
            #   若反复被 React 播放器拉回暂停，改点播放器 UI 播放按钮同步内部状态，避免 303 秒视频拖成 15 分钟；
            # - 若视频并未暂停只是网络缓冲（paused=False）：宽容等待至 >= 15s 才温和滚入视口并续调 v.play()，绝不误点暂停按钮。
            if is_paused and stalled >= 1.5 and (now - last_recover_at >= 3.0):
                last_recover_at = now
                pause_recover_streak += 1
                if now - last_pause_log_at >= 30.0:
                    last_pause_log_at = now
                    print(
                        f'    检测到视频暂停（{int(current)}/{int(duration)} 秒，ready={ready_state}），'
                        f'自动同步播放器续播…'
                    )
                try:
                    prefer_ui_btn = pause_recover_streak >= 2
                    self.driver.execute_script('''
                        const w = arguments[0], v = arguments[1], preferUi = !!arguments[2];
                        w.scrollIntoView({block: 'center'});
                        v.muted = true;
                        const clickUiPlay = () => {
                            const playBtn = w && w.querySelector ? w.querySelector(
                                'button[aria-label*="play" i], button[title*="播放"], ' +
                                '[class*="play-btn"], [class*="playBtn"], .vjs-big-play-button, .xgplayer-start'
                            ) : null;
                            if (playBtn && playBtn.getClientRects && playBtn.getClientRects().length) {
                                try { playBtn.click(); return true; } catch (e) {}
                            }
                            return false;
                        };
                        if (preferUi && v.paused) {
                            clickUiPlay();
                        }
                        if (v.paused) {
                            const p = v.play();
                            if (p && p.catch) p.catch(err => { v.__unipusPlaybackError = String(err); });
                        }
                    ''', wrapper, video, prefer_ui_btn)
                    time.sleep(0.35)
                    still_paused = bool(self.driver.execute_script('return !!arguments[0].paused;', video))
                    if still_paused:
                        recover_attempts += 1
                        self.driver.execute_script('''
                            const w = arguments[0];
                            const playBtn = w && w.querySelector ? w.querySelector(
                                'button[aria-label*="play" i], button[title*="播放"], ' +
                                '[class*="play-btn"], [class*="playBtn"], .vjs-big-play-button, .xgplayer-start'
                            ) : null;
                            if (playBtn && playBtn.getClientRects && playBtn.getClientRects().length) {
                                try { playBtn.click(); } catch (e) {}
                            }
                        ''', wrapper)
                except Exception:
                    pass
            elif (not is_paused) and stalled >= 15.0 and (now - last_recover_at >= 10.0) and recover_attempts < 2:
                recover_attempts += 1
                last_recover_at = now
                print(
                    f'    检测到播放停滞（{int(current)}/{int(duration)} 秒，'
                    f'缓冲中/ready={ready_state}），'
                    f'尝试自动恢复播放（{recover_attempts}/2）…'
                )
                try:
                    self.driver.execute_script('''
                        const w = arguments[0], v = arguments[1];
                        w.scrollIntoView({block: 'center'});
                        v.muted = true;
                        const p = v.play();
                        if (p && p.catch) p.catch(err => { v.__unipusPlaybackError = String(err); });
                    ''', wrapper, video)
                except Exception:
                    pass

            # 二级自愈触发：连续 32 秒无推进且已尝试过自愈（或卡在 0 秒超过 22 秒 / networkState=3 无源），
            # 高度疑似 20 分钟 CDN 签名过期或断流，直接请求刷新页面续播，避免白白死等 60 秒！
            if (stalled >= 32.0 and recover_attempts >= 2) or (
                current < 0.5 and stalled >= 22.0 and ready_state <= 1
            ) or (int(state.get('networkState') or 0) == 3 and stalled >= 15.0):
                print(f'    音视频持续停滞（{int(current)}/{int(duration)} 秒，ready={ready_state}），触发页面刷新续播')
                return 'reload'

            if stalled >= 60.0:
                print(f'    音视频已 60 秒未推进（{int(current)}/{int(duration)} 秒），标记为未完成')
                return 'failed'
            if now >= deadline:
                print(f'    音视频超时（{int(current)}/{int(duration)} 秒），标记为未完成')
                return 'failed'
            if now >= next_report:
                print(f'    音视频播放进度（{int(current)}/{int(duration)} 秒）')
                next_report = now + 30
        return None

    def _publish_process_quiz(self):
        """处理当前任务中的独立诊断测试，已有成绩和未完成答卷不重做。"""
        with self._publish_scan_nodes('[data-lexical-node-type="quiz-paper-link"]') as links:
            if links is None:
                return False
            if not links:
                return None
            if len(links) != 1:
                print('    当前任务包含多个测试，请手动处理')
                return False
            covers = links[0].find_elements(By.CSS_SELECTOR, '[class*="coverContainer"]')
            if not covers or not WebDriverHelper.safe_click(self.driver, covers[0]):
                print('    无法打开单元测试')
                return False
            try:
                WebDriverWait(self.driver, 15).until(lambda d: d.find_elements(By.CSS_SELECTOR, '[class*="acknowledge_btn__"]') or d.find_elements(By.CSS_SELECTOR, 'iframe[src*="/learner/testing/"]'))
                start = self.driver.find_elements(By.CSS_SELECTOR, '[class*="acknowledge_btn__"]')
                if start and not WebDriverHelper.safe_click(self.driver, start[0]):
                    return False
                frame = WebDriverWait(self.driver, 15).until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'iframe[src*="/learner/testing/"]')))
                self.driver.switch_to.frame(frame)
                WebDriverWait(self.driver, 20).until(lambda d: d.find_elements(By.CSS_SELECTOR, '[data-ipublish-question-group-type]'))
                text = self.driver.find_element(By.TAG_NAME, 'body').text
                if '最终得分' in text or '再次作答' in text:
                    print('    单元测试已有成绩，保留现有结果')
                    return True
                groups = self.publish_parser.collect_groups()
                # 先收集单元测试页内的阅读文章作为上下文，避免阅读理解/翻译缺背景
                for passage in self.publish_parser.collect_passages(groups):
                    self.ai_client.add_passage_if_new(passage)
                supported_quiz_kinds = {'choice', 'true_false', 'fill', 'dropdown', 'text', 'link_match'}
                parsed = []
                for state in groups:
                    if state['kind'] == 'skip':
                        continue
                    if state['kind'] not in supported_quiz_kinds:
                        print(f"    单元测试暂不支持题型: {state['type']}")
                        return False
                    questions = self.publish_parser.parse_group(state['element'], state['type'], 1)
                    if not questions:
                        print(f"    单元测试题目组解析失败: {state['type']}，保留当前进度")
                        return False
                    has_existing = any(opt.is_selected for q in questions for opt in q.options)
                    if not has_existing:
                        for q in questions:
                            for inp in q.inputs or []:
                                try:
                                    if (inp.get_attribute('value') or '').strip():
                                        has_existing = True
                                        break
                                except Exception:
                                    pass
                            if has_existing:
                                break
                    if has_existing:
                        print('    测试已有作答，保留当前进度')
                        return False
                    parsed.append((state, questions))
                if not parsed:
                    return False
                for state, questions in parsed:
                    self.ai_client.force_reset(f"单元测试_{state['type']}")
                    for passage in self.publish_parser.passages:
                        self.ai_client.add_passage_if_new(passage)
                    if not self._publish_prepare_media(state['element']):
                        return False
                    prompt = self.prompt_builder.build(questions, self.publish_parser.group_direction(state['element']))
                    prompt += self._publish_fill_format_hint(questions)
                    if state['type'] == 'fill_blanks_dropdown_group':
                        prompt += self._publish_dropdown_options_prompt(questions)
                    response = self.ai_client.ask(prompt, stop_requested=self.stop_requested)
                    if not response or self._should_stop():
                        return False
                    blank_offset = 0
                    for question in questions:
                        answer = self._publish_answer_text(question, response, blank_offset)
                        if question.q_type == QuestionType.FILL_IN:
                            answer = self._normalize_fill_answer_capitalization(question, answer)
                        if not answer or not self.publish_executor.execute(question, answer):
                            print(f"    测试题组 {state['type']} 未完整填写，保留当前进度")
                            return False
                        if question.q_type in (QuestionType.FILL_IN, QuestionType.DROPDOWN_SELECT):
                            blank_offset += question.answer_count or 1
                text = self.driver.find_element(By.TAG_NAME, 'body').text
                progress = re.search(r'答题卡共计\s*(\d+)\s*题\s*(\d+)\s*/\s*(\d+)', text)
                if not progress or int(progress.group(1)) == 0 or len(set(progress.groups())) != 1:
                    print('    无法确认测试全部答完，不提交')
                    return False
                submit = self.driver.find_elements(By.XPATH, "//span[normalize-space(.)='提交']")
                if not submit or not WebDriverHelper.safe_click(self.driver, submit[-1]):
                    return False
                self.driver.switch_to.parent_frame()
                confirm = WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable(
                    (By.XPATH, "//button[normalize-space(.)='确 定' or normalize-space(.)='确定']")))
                if not WebDriverHelper.safe_click(self.driver, confirm):
                    return False
                self.driver.switch_to.frame(frame)
                WebDriverWait(self.driver, 30).until(lambda d: '最终得分' in d.find_element(By.TAG_NAME, 'body').text)
                print('    单元测试已提交并生成成绩')
                return True
            except Exception as exc:
                print(f'    单元测试处理失败: {str(exc)[:100]}')
                return False

    def _publish_record_route_matches(self, route: str) -> Optional[bool]:
        task = getattr(self, '_current_directory_task', None) or {}
        expected = tuple(task.get('_record_route') or ())
        if not task.get('_record_matched') or not expected:
            return None
        directory = urlsplit(task.get('_directory_url') or '')
        resource = directory.path.rstrip('/').rsplit('/', 1)[-1]
        player = urlsplit(route)
        if (directory.hostname != 'ucloud.unipus.cn' or not resource.isdigit()
                or player.hostname != PublishPlayer.SHELL_HOST_HINT
                or parse_qs(player.query).get('courseResourceId') != [resource]):
            return False
        parts = tuple(part for part in unquote(player.fragment).split('/') if part)
        if 'courseware' not in parts:
            return None
        current = parts[parts.index('courseware') + 1:]
        return current[:len(expected)] == expected

    def _publish_prepare_scope(self, task_name: str = '', timeout: float = 30.0,
                               *, rebind: bool = False) -> bool:
        self._publish_scope = None
        deadline = time.monotonic() + timeout
        reason = '未能进入任务内容'
        while not self._should_stop() and time.monotonic() < deadline:
            try:
                route = self._publish_current_url()
                route_matches = self._publish_record_route_matches(route)
                if route_matches is False or (rebind and route_matches is not True):
                    raise ValueError('当前单元或小节与所选课件地址不一致')
                frame = PublishPlayer.find_frame(self.driver)
                if frame is None:
                    raise ValueError('无法定位内容 iframe')
                anchor = unquote(urlsplit(frame.get_attribute('src') or '').fragment)
                with PublishFrameContext(self.driver) as context:
                    if not context.entered:
                        raise ValueError('未能进入内容 iframe')
                    if task_name.startswith('quick_current_page_'):
                        scope = PublishTaskScope.capture_visible(self.driver)
                        if not scope.select(self.driver, self.publish_parser.collect_groups()):
                            raise ValueError('当前可见题块尚未加载出题目组')
                    else:
                        task_path = []
                        if route_matches:
                            section = self._current_directory_task.get('section') or ''
                            task_path = [part.strip() for part in section.split('/') if part.strip()]
                            task_path.append(task_name)
                        scope = PublishTaskScope.capture(
                            self.driver, anchor, task_path=task_path, require_task_path=rebind)
                    if '/course-v3:' in route and not scope.start_anchor:
                        raise ValueError('新版任务没有明确的正文锚点')
                    if task_name and not task_name.startswith('quick_current_page_') and scope.title:
                        normalize = lambda text: re.sub(r'\s+', ' ', text).strip().casefold()
                        if normalize(task_name) != normalize(scope.title):
                            raise ValueError(f'打开的任务“{scope.title}”与勾选的“{task_name}”不一致')
                    if self._publish_current_url() != route:
                        raise ValueError('任务仍在切换中')
                    self._publish_scope = scope
                    self._publish_scope_url = route
                    print(f"    作答范围：{scope.title or '当前独立题目页'}，到本任务末尾即停止")
                    return True
            except Exception as exc:
                reason = str(exc)
            if self.stop_requested.wait(0.2):
                return False
        print(f"    无法确认当前任务范围：{reason}，停止当前任务")
        return False

    @contextmanager
    def _publish_scan_nodes(self, selector: str, *, fallback_selector: str = ''):
        """只重试定位操作，向调用方交出节点后由调用方执行后续动作。"""
        scope = getattr(self, '_publish_scope', None)
        reason = '没有已核对的任务范围'
        if scope is not None:
            for attempt in range(2):
                if self._should_stop():
                    break
                with PublishFrameContext(self.driver) as context:
                    if context.entered:
                        try:
                            nodes = scope.query(self.driver, selector)
                            if not nodes and fallback_selector:
                                nodes = scope.query(self.driver, fallback_selector)
                        except (PublishScopeChanged, StaleElementReferenceException,
                                NoSuchFrameException, TimeoutException) as exc:
                            detail = str(exc).split('\n', 1)[0]
                            reason = f'{type(exc).__name__} {detail}'.strip()
                        else:
                            if self._should_stop():
                                break
                            yield nodes
                            return
                    else:
                        reason = '未能进入内容 iframe'
                if (attempt or self._should_stop() or scope.block_only or not scope.title
                        or self._publish_record_route_matches(self._publish_current_url()) is not True):
                    break
                print("    课件内容已重新加载或读取超时，重新定位当前任务")
                if not self._publish_prepare_scope(scope.title, timeout=8.0, rebind=True):
                    break
                scope = self._publish_scope
        if not self._should_stop():
            print(f"    无法扫描当前任务内容：{reason}")
        yield None

    def _publish_scoped_groups(self, groups: List[Dict]) -> List[Dict]:
        scope = getattr(self, '_publish_scope', None)
        if scope is None:
            return groups
        # 播放器会随题块状态自动更新外壳路由，不能用它重新划定作答范围。
        # 始终使用最初的正文锚点；select 同时校验 iframe 文档和边界是否仍有效。
        return scope.select(self.driver, groups)

    def _publish_group_in_scope(self, group) -> bool:
        try:
            return bool(self._publish_scoped_groups([{'element': group}]))
        except Exception as exc:
            print(f"    题组范围校验失败：{exc}")
            return False

    def _publish_reveal_more_groups(self) -> bool:
        """滚动到本任务末尾的题块（含尚未懒加载挂载出 group 的 question-block），唤醒更多题组。"""
        with PublishFrameContext(self.driver) as context:
            if not context.entered:
                return False
            try:
                groups = self._publish_scoped_groups(self.publish_parser.collect_groups())
                before = len(groups)
                target = groups[-1]['element'] if groups else None
                if target is None:
                    # 初始 0 个已挂载题目组时，主动扫本任务范围内的 question-block / 末尾块唤醒懒加载
                    blocks = self.driver.find_elements(
                        By.CSS_SELECTOR,
                        '[data-lexical-node-type="question-block"], [data-lexical-node-type="collapsible-container"]')
                    scoped_blocks = self._publish_scoped_groups([{'element': b} for b in blocks]) if blocks else []
                    if scoped_blocks:
                        target = scoped_blocks[-1]['element']
                if target is None:
                    return False
                self.driver.execute_script("""
                    const el = arguments[0];
                    el.scrollIntoView({block: 'end', inline: 'nearest'});
                    try { window.dispatchEvent(new Event('scroll')); } catch (e) {}
                    try { window.dispatchEvent(new Event('resize')); } catch (e) {}
                """, target)
                if self.stop_requested.wait(1.2):
                    return False
                after = len(self._publish_scoped_groups(self.publish_parser.collect_groups()))
            except Exception:
                return False
            if after > before:
                print(f"    滚动加载出更多题目组: {before} -> {after}")
                return True
            return False

    def _publish_unrecorded_objective_group(self, scoped_groups):
        task = getattr(self, '_current_directory_task', None) or {}
        if (task.get('_publish_redo_attempted')
                or self._publish_record_route_matches(getattr(self, '_publish_scope_url', '')) is not True):
            return None
        recorded = [g for g in task.get('_record_groups') or []
                    if g.get('scored') is True and g.get('kind') == 'task']
        if len(recorded) != 1:
            return None
        rec = recorded[0]
        groups = [state for state in scoped_groups if state['kind'] not in ('skip', 'ai_oral')]
        if len(groups) != 1:
            return None
        grp = groups[0]
        if rec.get('score') is None and rec.get('progress') in (None, 0):
            if grp['kind'] in ('choice', 'true_false', 'fill', 'dropdown', 'link_match', 'text'):
                return grp
        # 写作题已出分但低于 85 分目标（如 Writing skill 60.7 分）：若页面提供重做/修改按钮，允许自动重写提分 1 次
        if (grp['type'] == 'writing_group'
                and rec.get('score') is not None
                and float(rec['score']) < 85.0):
            return grp
        return None

    def _publish_process_next_group(self) -> str:
        """在当前页处理下一个待作答题目组，返回 answered / done / error。"""
        # 切单元后可能再次弹出「本单元学习规则」，每组开始前先顺手关掉
        self.publish_executor.dismiss_rules_popup()
        with PublishFrameContext(self.driver) as context:
            if not context.entered:
                print("    未能进入 ipublish 内容 iframe")
                return 'error'

            parser = self.publish_parser
            groups = parser.collect_groups()
            if not groups:
                print("    内容层没有找到题目组（可能仍在加载）")
                return 'done'

            try:
                scoped_groups = self._publish_scoped_groups(groups)
            except ValueError as exc:
                print(f"    {exc}")
                return 'error'

            for passage in parser.collect_passages(groups):
                self.ai_client.add_passage_if_new(passage)

            reopenable = self._publish_unrecorded_objective_group(scoped_groups)
            pending = None
            for state in scoped_groups:
                effective_type = state['type']
                if state['kind'] == 'skip':
                    # 若 discussion_group 实际带有未提交的提交按钮与作答输入框，则按简答题自动作答提交；
                    # 纯提示/无提交按钮的讨论区仍保持跳过。
                    if state['type'] == 'discussion_group' and not parser.group_submitted(state['element']):
                        btn = parser.submit_button(state['element'])
                        has_input = bool(
                            parser._answer_textareas(state['element'])
                            or state['element'].find_elements(By.CSS_SELECTOR, '[contenteditable="true"]')
                        )
                        if btn is not None and has_input:
                            effective_type = 'short_answer_group'
                        else:
                            continue
                    else:
                        continue
                if state is not reopenable and parser.group_submitted(state['element']):
                    continue
                key = parser.group_key(state['element'])
                if key in self._publish_processed_groups:
                    continue
                if state['kind'] == 'ai_oral':
                    print('    AI 口语对话需要在平台内完成')
                    self._publish_processed_groups.add(key)
                    return 'skipped'
                pending = (state, effective_type, key)
                break

            if pending is None:
                if scoped_groups:
                    summary = []
                    for st in scoped_groups[:6]:
                        tag = ('页面显示已有作答' if parser.group_submitted(st['element'])
                               else '阅读或讨论' if st['kind'] == 'skip' else '待完成')
                        summary.append(f"{st['type']}({tag})")
                    sig = ', '.join(summary)
                    if sig != getattr(self, '_last_publish_group_summary', ''):
                        self._last_publish_group_summary = sig
                        print(f"    本任务题组状态: {sig}")
                return 'done'

            state, effective_type, key = pending
            outcome = self._publish_answer_group(
                state['element'], effective_type, key, reopen_unrecorded=state is reopenable)
            if outcome == 'submitted':
                return 'answered'
            if outcome == 'rejected':
                return 'rejected'
            if outcome == 'skipped':
                return 'skipped'
            if outcome == 'reviewed':
                return 'reviewed'
            return 'error'

    def _publish_reopen_unrecorded_group(self, group, resolver):
        task = getattr(self, '_current_directory_task', None)
        if not task or task.get('_publish_redo_attempted') or self._should_stop():
            return None
        executor = self.publish_executor
        button = executor.find_submit_button(group)
        if not executor.button_allows_action(
                button, ('重新作答', '再次作答', '重做', '重新写作', '再次写作', '修改作文', '重新提交', '修改')):
            btn_txt = PublishPlayer.button_text(button) if button is not None else 'None'
            print(f"    该题组当前按钮为 {btn_txt!r}，平台未开放重做入口")
            return None
        task['_publish_redo_attempted'] = True
        print(f"    触发该题组重做入口「{PublishPlayer.button_text(button)}」...")
        if not WebDriverHelper.safe_click(self.driver, button):
            return None
        deadline = time.monotonic() + 10
        while not self._should_stop() and time.monotonic() < deadline:
            try:
                current = resolver() or group
                submit = executor.find_submit_button(current)
                if executor.button_allows_action(submit, PublishPlayer.SUBMIT_ACTIONS):
                    return current
            except WebDriverException:
                pass
            if self.stop_requested.wait(0.25):
                return None
        return None

    def _publish_answer_group(self, group, group_type: str, key: str, *, reopen_unrecorded=False):
        """作答并提交一个题目组（调用前需已在内容 iframe 内）。

        返回 'submitted' / 'skipped' / False。
        """
        parser = self.publish_parser
        if not self._publish_group_in_scope(group):
            print("    题目组不在当前勾选任务内，不作答")
            return False
        group_id = group.get_attribute('id') or ''
        resolver = lambda: self._publish_find_group_by_key(key, group_id)
        if not self.publish_executor.reveal_group(group, self.stop_requested):
            print("    未能展开题目所在的折叠面板，停止当前任务")
            return False
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", group)
        except Exception:
            pass
        time.sleep(0.4)

        # 播放器是懒渲染：没滚到可视区时组内只有占位 DOM（innerText 为空）。
        # 必须先等它真正挂载，否则会对着空组填答案、提交也等不到已批改状态。
        rendered = self._publish_wait_group_rendered(group, timeout=15.0, group_type=group_type, resolver=resolver)
        if not rendered and not self._should_stop():
            # 若滚动唤醒后仍停在 100px 骨架占位（chunk/接口首载卡住），本任务自动刷新重连 1 次再试
            task_ref = getattr(self, '_current_directory_task', None)
            if isinstance(task_ref, dict) and not task_ref.get('_publish_render_reloaded'):
                task_ref['_publish_render_reloaded'] = True
                print(f"    题目组 {group_type} 首次加载卡在占位态，自动刷新页面重试 1 次…")
                try:
                    PublishPlayer.reset(self.driver)
                    self.driver.refresh()
                    if self.stop_requested.wait(3.0):
                        return False
                    self.publish_executor.dismiss_rules_popup()
                    scope_title = (getattr(getattr(self, '_publish_scope', None), 'title', '') or '').strip()
                    if self._publish_prepare_scope(scope_title, timeout=25.0):
                        if PublishFrameContext(self.driver).enter():
                            fresh_group = resolver()
                            if fresh_group is not None:
                                group = fresh_group
                                self.publish_executor.reveal_group(group, self.stop_requested)
                                rendered = self._publish_wait_group_rendered(
                                    group, timeout=15.0, group_type=group_type, resolver=resolver)
                except Exception as exc:
                    print(f"    刷新重试异常: {str(exc)[:80]}")
        if not rendered:
            print(f"    题目组 {group_type} 滚动后仍未渲染，跳过该组（下次再处理）")
            self._publish_processed_groups.add(key)
            return 'skipped'

        group = resolver() or group
        if parser.group_submitted(group):
            if not reopen_unrecorded:
                self._publish_processed_groups.add(key)
                return 'reviewed'
            editable = self._publish_reopen_unrecorded_group(group, resolver)
            if editable is None:
                print("    旧作答页面尚未进入可作答状态，需在平台确认重做入口")
                self._publish_processed_groups.add(key)
                return 'skipped'
            group = editable
            if not self._publish_wait_group_rendered(group, timeout=10.0, group_type=group_type, resolver=resolver):
                self._publish_processed_groups.add(key)
                return 'skipped'
            group = resolver() or group

        direction = parser.group_direction(group)
        unsupported_before = len(parser.unsupported)
        questions = parser.parse_group(group, group_type, 1)
        if group_type == 'true_false_group':
            task = getattr(self, '_current_directory_task', None) or {}
            if any('判断并改错' in (g.get('name') or '') for g in task.get('_record_groups') or []):
                for question in questions:
                    question.needs_correction = True
        if len(parser.unsupported) > unsupported_before:
            print(f"    暂不支持自动作答的题型: {parser.unsupported[-1]}")

        print(f"\n    题目组 {group_type}：{len(questions)} 道题")
        if not questions:
            print("    该题目组没有可解析的题目，跳过")
            self._publish_processed_groups.add(key)
            return 'skipped'
        nodes = parser._question_nodes(group)
        if nodes and group_type != 'multi_media_upload_group' and len(questions) != len(nodes):
            print("    本组有小题未能解析，不提交，请检查页面结构")
            return False

        if not self._publish_prepare_media(group):
            return False
        filled = 0
        if all(question.q_type == QuestionType.RECORD for question in questions):
            for question in questions:
                if self._should_stop() or not self._publish_group_in_scope(group):
                    return False
                if not self._publish_select_record_question(group, question):
                    print("    无法切换到口语小题，不提交")
                    return False
                if self.publish_executor.publish_record_uploaded(question):
                    print("    本题已有上传录音，保留现有结果")
                    filled += 1
                    continue
                answer = ''
                if question.needs_speech_script:
                    prompt = self.prompt_builder.build_speaking_script(question, direction)
                    answer = self.ai_client.ask(prompt, stop_requested=self.stop_requested)
                    if not answer or self._should_stop():
                        print("    AI 未返回口语稿")
                        return False
                if self.publish_executor.execute(question, answer):
                    filled += 1
        else:
            if group_type == 'writing_group':
                prompt = self.prompt_builder.build_writing(questions, direction)
            else:
                prompt = self.prompt_builder.build(questions, direction)
            prompt += self._publish_fill_format_hint(questions)
            if group_type == 'fill_blanks_dropdown_group':
                prompt += self._publish_dropdown_options_prompt(questions)
            elif group_type == 'multi_media_upload_group':
                prompt += (
                    "\n要求：本题需要直接提交纯英文作答内容或语音发言稿。请直接输出一段自然连贯的"
                    "英文正文（约 90–160 词，适合口语自然朗读或文档提交），不要输出 Markdown 符号、"
                    "不要写「答案：」等标题前缀、不要附带中文解释。"
                )
            ai_response = self.ai_client.ask(prompt, stop_requested=self.stop_requested)
            if self._should_stop():
                return False
            if not ai_response:
                print("    AI 未返回答案")
                return False
            blank_offset = 0
            for question in questions:
                if self._should_stop() or not self._publish_group_in_scope(group):
                    return False
                answer = self._publish_answer_text(question, ai_response, blank_offset)
                if question.q_type == QuestionType.FILL_IN:
                    answer = self._normalize_fill_answer_capitalization(question, answer)
                if group_type == 'writing_group' and len(questions) == 1 and len(question.inputs) == 1:
                    draft = _strip_markdown_text(answer).strip()
                    written = (bool(draft) and not AnswerExecutor._is_refusal_answer(draft)
                               and self.publish_executor._fill_text_input_verified(question.inputs[0], draft))
                else:
                    written = bool(answer) and self.publish_executor.execute(question, answer)
                    # 严格词库选词填空若有空位不在词库中，自动带精确词库列表追问 1 次纠错重填
                    mismatch = getattr(self.publish_executor, '_last_bank_mismatch', None)
                    if (not written and question.strict_word_bank and mismatch
                            and not self._should_stop()):
                        bad_desc = ', '.join(
                            f"空{i}({v})" for i, v in zip(mismatch.get('indices', []), mismatch.get('values', []))
                        )
                        bank_str = ', '.join(mismatch.get('bank', []))
                        print(f"    词库校验未通过（{bad_desc}），自动带精确词库列表请求 AI 纠错重选...")
                        repair_prompt = (
                            f"你刚才的填空答案中，{bad_desc} 不在题目给定的词库里！\n"
                            f"本题唯一合法词库（必须原样复制拼写，禁止自行造词、替换同义词或改动词形）：\n"
                            f"[{bank_str}]\n\n"
                            f"请重新输出全部 {len(question.inputs)} 个空的完整答案（每行格式：1. word），"
                            f"确保每一空都严格取自上述词库且每个词只用一次。"
                        )
                        repaired = self.ai_client.ask(repair_prompt, stop_requested=self.stop_requested)
                        if repaired and not self._should_stop():
                            repaired_ans = self._publish_answer_text(question, repaired, 0)
                            if question.q_type == QuestionType.FILL_IN:
                                repaired_ans = self._normalize_fill_answer_capitalization(question, repaired_ans)
                            written = bool(repaired_ans) and self.publish_executor.execute(question, repaired_ans)
                if written:
                    filled += 1
                if question.q_type in (QuestionType.FILL_IN, QuestionType.DROPDOWN_SELECT):
                    blank_offset += question.answer_count or 1
        print(f"    本组填写 {filled}/{len(questions)} 题")

        if filled != len(questions):
            print("    本组未完整填写，不提交，停止当前任务以免留下半成品")
            return False

        if self._should_stop():
            return False

        # 提交前退出内容层：切单元后「本单元学习规则」弹窗可能再次出现，
        # 它会挡住提交按钮（它在外壳层，只有真实鼠标点击才有效）。
        PublishPlayer.reset(self.driver)
        popup_closed = self.publish_executor.dismiss_rules_popup()
        if popup_closed:
            print("    提交前已关闭「本单元学习规则」弹窗")

        with PublishFrameContext(self.driver) as submit_context:
            if not submit_context.entered:
                print("    提交前未能重新进入内容 iframe")
                return False
            submit_group = group
            if popup_closed:
                # 关弹窗会重排外壳 DOM，重新定位一次更稳；定位不到就沿用原元素
                relocated = self._publish_find_group_by_key(key)
                if relocated is not None:
                    submit_group = relocated
            if not self._publish_wait_group_rendered(submit_group, timeout=6):
                print("    提交前题目组未渲染，跳过该组")
                self._publish_processed_groups.add(key)
                return 'skipped'
            if not self._publish_group_in_scope(submit_group):
                print("    提交前任务边界校验失败，不提交")
                return False
            button = self.publish_executor.find_submit_button(submit_group)
            print(f"    提交按钮状态: text={PublishPlayer.button_text(button)!r} "
                  f"displayed={button.is_displayed() if button is not None else None}")
            group_id = submit_group.get_attribute('id') or ''
            if self._should_stop():
                return False
            submitted_ok = self.publish_executor.submit(
                submit_group,
                timeout=30.0,
                resolver=lambda: self._publish_find_group_by_key(key, group_id))
            if not submitted_ok:
                rejection = getattr(self.publish_executor, 'last_rejection', '')
                if rejection:
                    print(f"    平台拒绝提交（{rejection}），该组保持未提交状态并继续后面题目")
                    self._publish_processed_groups.add(key)
                    return 'rejected'
            if not submitted_ok:
                self._submission_unconfirmed = not self._should_stop()
                print("    未收到本题组提交回执，等待课程学习记录核验")
                return False

        self._publish_processed_groups.add(key)
        print("    已收到本次提交的页面回执，等待学习记录确认")
        return 'submitted'

    def _publish_find_group_by_key(self, key: str, group_id: str = ''):
        if group_id:
            matches = self.driver.find_elements(By.ID, group_id)
            if (len(matches) == 1 and matches[0].get_attribute('data-ipublish-question-group-type')
                    and self._publish_group_in_scope(matches[0])):
                return matches[0]
            return None
        parser = self.publish_parser
        for state in self._publish_scoped_groups(parser.collect_groups()):
            group = state['element']
            if parser.group_key(group) == key:
                return group
        return None

    def _publish_select_record_question(self, group, question: Question) -> bool:
        """按当前可见小题的位置操作上一题/下一题，不跨越题组。"""
        nodes = self.publish_parser._question_nodes(group)
        target = question.element.get_attribute('data-question-id')
        for _ in range(len(nodes) + 1):
            if self._should_stop():
                return False
            visible = [i for i, node in enumerate(nodes) if node.is_displayed()]
            desired = next((i for i, node in enumerate(nodes)
                            if node.get_attribute('data-question-id') == target), -1)
            if desired < 0:
                return False
            if desired in visible:
                return True
            if len(visible) != 1:
                return False
            label = '下一个' if visible[0] < desired else '上一个'
            buttons = group.find_elements(By.CSS_SELECTOR, '[class*="q-oral-header_handle-btn"]')
            button = next((item for item in buttons
                           if label in (item.text or '') and
                           'disabled' not in (item.get_attribute('class') or '').lower()), None)
            if not button or not WebDriverHelper.safe_click(self.driver, button):
                return False
            if self.stop_requested.wait(0.3):
                return False
        return False

    def _get_transcriber(self):
        transcriber = getattr(self, 'audio_transcriber', None)
        if transcriber is None:
            handler = getattr(self, 'video_handler', None)
            transcriber = getattr(handler, 'transcriber', None)
        return transcriber

    def _publish_preceding_media_wrappers(self, group) -> List[Any]:
        """收集本任务范围内位于当前题目组之前的独立音视频块（如 Viewing 视听题的上方视频）。"""
        try:
            all_wrappers = self.driver.find_elements(
                By.CSS_SELECTOR,
                '[data-lexical-node-type="insert-audio"], [data-lexical-node-type="insert-video"]')
            scoped = self._publish_scoped_groups([{'element': w} for w in all_wrappers])
            preceding = []
            for state in scoped:
                w = state['element']
                is_before = self.driver.execute_script("""
                    const w = arguments[0], g = arguments[1];
                    if (!w || !g || g.contains(w)) return false;
                    if (w.closest('[data-ipublish-question-group-type]')) return false;
                    return !!(w.compareDocumentPosition(g) & Node.DOCUMENT_POSITION_FOLLOWING);
                """, w, group)
                if is_before:
                    preceding.append(w)
            return preceding[-2:]  # 仅取紧邻题组前的至多 2 段任务音视频，避免跨节串音
        except Exception:
            return []

    def _publish_prepare_media(self, group) -> bool:
        """处理当前题组内（以及紧邻题组前的任务级）音视频材料并转写，确保视听题有原文上下文。"""
        if self._should_stop():
            return False
        inner_wrappers = group.find_elements(
            By.CSS_SELECTOR,
            '[data-lexical-node-type="insert-audio"], [data-lexical-node-type="insert-video"]')
        preceding_wrappers = [] if inner_wrappers else self._publish_preceding_media_wrappers(group)
        if not inner_wrappers and not preceding_wrappers:
            return True
        transcriber = self._get_transcriber()
        if transcriber is None:
            if inner_wrappers:
                print("    当前题组缺少所需的音视频转写，请先核对材料")
                return False
            return True

        for is_inner, wrappers in ((True, inner_wrappers), (False, preceding_wrappers)):
            for wrapper in wrappers:
                kind = wrapper.get_attribute('data-lexical-node-type') or ''
                is_video = kind == 'insert-video'
                media = wrapper.find_elements(By.TAG_NAME, 'video' if is_video else 'audio')
                if not media:
                    if is_inner:
                        print("    音视频播放器未加载，不提交")
                        return False
                    continue
                url = (wrapper.get_attribute('data-video-url') if is_video
                       else media[0].get_attribute('src')) or ''
                if not url and is_video:
                    try:
                        url = media[0].get_attribute('src') or ''
                    except Exception:
                        url = ''
                if urlsplit(url).scheme not in ('http', 'https'):
                    if is_inner:
                        print("    未找到可下载的音视频材料，不提交")
                        return False
                    continue
                if is_inner and not self.publish_executor.play_material(media[0]):
                    print("    未能确认音视频播放，不提交")
                    return False
                clean_url = url.split('#')[0]
                cached = getattr(self, '_audio_transcript_cache', {}).get(clean_url)
                if cached:
                    transcript = cached
                else:
                    if not is_inner:
                        print(f"    转写本任务前置{'视频' if is_video else '音频'}作为视听答题上下文…")
                    transcript = transcriber.transcribe(clean_url, language='en')
                    if transcript and getattr(self, '_audio_transcript_cache', None) is not None:
                        self._audio_transcript_cache[clean_url] = transcript
                if not (transcript or '').strip() or self._should_stop():
                    if is_inner:
                        print("    音视频转写失败，不提交")
                        return False
                    continue
                if is_video:
                    self.ai_client.add_video_transcript_if_new(transcript)
                else:
                    self.ai_client.add_audio_transcript_if_new(transcript)
        return not self._should_stop()

    def _publish_dropdown_options_prompt(self, questions: List[Question]) -> str:
        """下拉填空：尽力抓取每个空位的候选列表，供模型按原样选择。"""
        if not questions:
            return ''
        lines = []
        offset = 0
        for question in questions:
            blanks = self.publish_executor._dropdown_blanks(question)
            for index, blank in enumerate(blanks, offset + 1):
                try:
                    options = self.publish_executor.collect_dropdown_options(blank)
                except Exception:
                    options = []
                print(f"    空{index} 候选抓取: {len(options)} 个")
                if options:
                    lines.append(f"   空{index} 候选: {' / '.join(options)}")
            offset += question.answer_count or len(blanks) or 1
        if not lines:
            return ''
        return ("\n【空位候选】每个空只能从下列候选中选择，尽量按原样（含大小写）作答：\n"
                + "\n".join(lines) + "\n")

    def _publish_fill_format_hint(self, questions: List[Question]) -> str:
        """一个题目组里有多道填空时，要求按整组空号连续编号，避免题号/空号混在一起。"""
        fill_questions = [q for q in questions
                          if q.q_type in (QuestionType.FILL_IN, QuestionType.DROPDOWN_SELECT)]
        total_blanks = sum(q.answer_count or 1 for q in fill_questions)
        if len(fill_questions) < 2 or total_blanks < 2:
            return ''
        ranges = []
        offset = 0
        for question in fill_questions:
            count = question.answer_count or 1
            ranges.append(f"小题{question.number} 的局部空号 1..{count} 对应整组空号 "
                          f"{offset + 1}..{offset + count}")
            offset += count
        return (
            "\n【本题组格式】\n"
            f"本题目组共 {len(fill_questions)} 道小题、{total_blanks} 个空，"
            f"请按空号 1..{total_blanks} 连续编号作答，"
            "不要按小题再分组，也不要在答案里加括号或额外题号，"
            "例如：1.answer1 2.answer2 3.answer3\n"
            + '\n'.join(ranges) + '\n'
        )

    def _publish_wait_group_rendered(self, group, timeout: float = 18.0, group_type: str = '', resolver=None) -> bool:
        """将题组保持在视口内并驱动可滚动祖先容器与离场重进，唤醒 IntersectionObserver 懒渲染。"""
        deadline = time.time() + timeout
        last_nudge = 0.0
        nudge_step = 0
        last_diag: Dict[str, Any] = {}
        while time.time() < deadline:
            if self._should_stop():
                return False
            try:
                if resolver is not None:
                    group = resolver() or group
                diag = self.driver.execute_script("""
                    const g = arguments[0];
                    if (!g || !g.isConnected) return {ready: false, reason: 'detached'};
                    const innerLen = (g.innerText || '').trim().length;
                    const textLen = (g.textContent || '').trim().length;
                    const rect = g.getBoundingClientRect();
                    const style = getComputedStyle(g);
                    const qNodes = g.querySelectorAll(
                        '[data-question-id], input, textarea, [contenteditable="true"], .react-flow__node'
                    ).length;
                    const visible = rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    const ready = visible && qNodes > 0;
                    return {
                        ready, innerLen, textLen, qNodes,
                        w: Math.round(rect.width), h: Math.round(rect.height),
                        display: style.display, cv: style.contentVisibility || '',
                        childCount: g.children ? g.children.length : 0,
                        hasShadow: !!g.shadowRoot,
                        hasIframe: !!g.querySelector('iframe'),
                        html: (g.outerHTML || '').replace(/\\s+/g, ' ').slice(0, 140),
                    };
                """, group) or {}
                last_diag = diag
                if diag.get('ready'):
                    return True
            except Exception:
                pass

            now = time.time()
            if now - last_nudge >= 2.5:
                last_nudge = now
                nudge_step += 1
                try:
                    self.publish_executor.reveal_group(group, self.stop_requested)
                    block = 'center' if nudge_step % 2 == 1 else 'start'
                    offset = 120 if nudge_step % 2 == 1 else -120
                    if nudge_step == 3:
                        # 强制滚离视口再滚回，触发 IntersectionObserver exit -> enter 状态跳变
                        self.driver.execute_script("""
                            const g = arguments[0];
                            if (!g) return;
                            for (let p = g.parentElement; p; p = p.parentElement) {
                                const st = getComputedStyle(p);
                                if (/(auto|scroll|overlay)/.test(st.overflowY) && p.scrollHeight > p.clientHeight + 20) {
                                    p.scrollTop = Math.max(0, p.scrollTop - 450);
                                    try { p.dispatchEvent(new Event('scroll', {bubbles: true})); } catch (e) {}
                                }
                            }
                            try { window.scrollBy(0, -450); } catch (e) {}
                            try { window.dispatchEvent(new Event('scroll')); } catch (e) {}
                        """, group)
                        if self.stop_requested.wait(0.35):
                            return False
                    self.driver.execute_script("""
                        const g = arguments[0], block = arguments[1], dy = arguments[2];
                        if (!g) return;
                        g.scrollIntoView({block: block, inline: 'nearest', behavior: 'instant'});
                        for (let p = g.parentElement; p; p = p.parentElement) {
                            const st = getComputedStyle(p);
                            if (/(auto|scroll|overlay)/.test(st.overflowY) && p.scrollHeight > p.clientHeight + 20) {
                                p.scrollTop = Math.max(0, p.scrollTop + dy);
                                try { p.dispatchEvent(new Event('scroll', {bubbles: true})); } catch (e) {}
                                try { p.dispatchEvent(new WheelEvent('wheel', {deltaY: dy, bubbles: true})); } catch (e) {}
                            }
                        }
                        try { window.scrollBy(0, dy); } catch (e) {}
                        try { window.dispatchEvent(new Event('scroll')); } catch (e) {}
                        try { window.dispatchEvent(new Event('resize')); } catch (e) {}
                    """, group, block, offset)
                except Exception:
                    pass

            if self.stop_requested.wait(0.5):
                return False

        if last_diag:
            print(
                f"    [未渲染诊断] type={group_type or 'unknown'} "
                f"size={last_diag.get('w', 0)}x{last_diag.get('h', 0)} "
                f"inner={last_diag.get('innerLen', 0)} text={last_diag.get('textLen', 0)} "
                f"nodes={last_diag.get('qNodes', 0)} children={last_diag.get('childCount', 0)} "
                f"shadow={last_diag.get('hasShadow', False)} iframe={last_diag.get('hasIframe', False)} "
                f"display={last_diag.get('display', '')} html={last_diag.get('html', '')!r}"
            )
        return False

    def _publish_answer_text(self, question: Question, ai_response: str,
                             blank_offset: int = 0) -> str:
        if question.q_type in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE,
                               QuestionType.TRUE_FALSE):
            return self._extract_single_answer(ai_response, question.number)
        if question.q_type == QuestionType.LINK_MATCH:
            # 收紧为 {提示序号}.{选项字母}，解释文字里的残留交给 _parse_link_answer 去重
            pairs = re.findall(r'(?<![\d.])(\d{1,2})\s*[.\-:：)）]\s*([A-Za-z])(?![A-Za-z])',
                               ai_response or '')
            return ' '.join(f'{n}.{letter}' for n, letter in pairs)
        answer = ai_response
        if question.q_type in (QuestionType.FILL_IN, QuestionType.DROPDOWN_SELECT):
            answer = self._sanitize_fill_answer(answer)
            if blank_offset:
                return self._shift_blank_answer(answer, blank_offset,
                                                question.answer_count or 1)
        return answer

    @staticmethod
    def _sanitize_fill_answer(answer: str) -> str:
        """去掉「1. (1) Iraq」这种题号后紧跟空号括号的重复编号。"""
        cleaned = re.sub(r'(?<=\d\.)\s*\(\s*\d+\s*\)', ' ', answer or '')
        return re.sub(r'[ \t]+', ' ', cleaned).strip()

    def _normalize_fill_answer_capitalization(self, question: Question, answer: str) -> str:
        """句首空自动补首字母大写（平台按正确率判分，大小写也算分）。"""
        if question.strict_word_bank:
            return answer
        count = question.answer_count or 1
        values = AnswerExecutor._parse_banked_answer(answer, count)
        if not values or not any(values):
            return answer
        values = self._capitalize_sentence_initial_answers(question.text, values)
        return ' '.join(f'{index + 1}.{value}'
                        for index, value in enumerate(values) if value)

    @staticmethod
    def _capitalize_sentence_initial_answers(text: str, values: List[str]) -> List[str]:
        text = text or ''
        result = []
        for index, value in enumerate(values, 1):
            marker = f'({index})'
            position = text.find(marker)
            if position < 0:
                result.append(value)
                continue
            prefix = text[:position].rstrip()
            rest_of_line = text[position + len(marker):].partition('\n')[0].strip()
            # 完整例句后独立编号的空位用于填写词项，句号并不表示答案处于句首。
            needs_capital = not prefix or (bool(rest_of_line) and prefix[-1] in '.!?;:。！？；：')
            if needs_capital and value and value[0].isalpha():
                value = value[0].upper() + value[1:]
            result.append(value)
        return result

    @staticmethod
    def _shift_blank_answer(answer: str, offset: int, count: int) -> str:
        """同组多道填空题：把第 offset+1..offset+count 个空映射回 1..count。

        course-v3 的题目组里可能有多道填空，AI 一次给的是整组空号；
        每道题只能拿自己那一段，否则第二题会重复第一题的答案。
        """
        parsed = AnswerExecutor._parse_banked_answer(answer, offset + count)
        values = parsed[offset:offset + count]
        return ' '.join(f'{index + 1}.{value}'
                        for index, value in enumerate(values) if value)

    def _publish_current_url(self) -> str:
        try:
            return self.driver.current_url or ''
        except Exception:
            return ''

    def _extract_single_answer(self, ai_response: str, question_number: int) -> str:
        answer = AnswerExecutor._extract_answer_by_number(ai_response, question_number)
        if answer:
            return answer
        lines = [l.strip() for l in (ai_response or '').split('\n') if l.strip()]
        # 模型带推理过程时（如 "- A: ... \n答案：ACE"），优先从底部提取显式「答案：...」行，
        # 避免把第一行开场白（如 "根据听力音频转写 1 核对："）误当成答案！
        if question_number == 1 and lines:
            label_re = re.compile(
                r'^(?:最终答案|正确答案|参考答案|答案|多选题|单选题|选择题|Answer|Correct\s+answer)\s*[：:]\s*(.+)$',
                re.I,
            )
            for raw_line in reversed(lines):
                cleaned_line = _strip_markdown_text(raw_line)
                m = label_re.match(cleaned_line)
                if m and m.group(1).strip():
                    return m.group(1).strip()
            for raw_line in reversed(lines):
                cleaned_line = _strip_markdown_text(raw_line).rstrip('.。')
                if re.fullmatch(r'[A-Z](?:[\s,，、/;&+]*[A-Z])*', cleaned_line.upper()):
                    return cleaned_line.upper()
        if re.search(AnswerExecutor.NUMBER_PREFIX_PATTERN, ai_response, re.IGNORECASE):
            return ""
        if 1 <= question_number <= len(lines):
            return lines[question_number - 1]
        return ""

    def _generate_questions_signature(self, questions: List[Question]) -> str:
        if not questions:
            return "empty"

        parts = []
        for q in questions:
            q_type = q.q_type.name if q.q_type else "UNKNOWN"
            # 用完整题干。只取前 30 字时，相邻题目若共用长前缀（录音题的
            # q.text 开头就是全任务固定的 directions），签名会相同，翻页被判成末页。
            text_preview = re.sub(r'\s+', ' ', q.text or '').strip()
            if q.q_type in (QuestionType.RECORD, QuestionType.ORAL_ALOUD):
                # 只使用稳定题干；q.text 和 name 的回退值可能包含播放器状态。
                stable = [(item.get('prompt') or item.get('text') or '').strip()
                          for item in (q.record_items or [])]
                stable = [value for value in stable if value]
                text_preview = '|'.join(stable)
            option_preview = "|".join(opt.text[:30] for opt in q.options[:4])
            page_progress = ""
            if q.q_type == QuestionType.RECORD:
                try:
                    page_progress = self.driver.execute_script("""
                        const progress = document.querySelector('.comp-question-progress .percent');
                        return progress ? progress.style.width : '';
                    """) or ""
                except Exception:
                    pass
            parts.append(f"{q.number}:{q_type}:{text_preview}:{option_preview}:{page_progress}")

        return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]

    def solve_current_page(self, chapter_name: str = "unknown") -> bool:
        print("\n" + "=" * 60)
        print(" 开始处理当前停留的页面")
        print("=" * 60)

        self._publish_processed_groups = set()
        # 每次快速处理都重新判断当前页有没有 Learning / Practicing 标签
        self._inline_learning_tried = False
        self._reading_ctx_tried = False
        self._current_section_key = None
        self._current_tab_is_scored = None
        self._current_directory_task = None
        self._last_question_types = set()

        quick_tab_name = f"quick_current_page_{int(time.time())}"
        self.ai_client.force_reset(f"{chapter_name}_{quick_tab_name}")

        state_key = self._generate_content_hash()

        if not state_key or state_key == "empty":
            state_key = f"{chapter_name}_{int(time.time())}"

        print(f"    内容标识: {state_key[:50]}...")

        if state_key in self.processed_hashes:
            print(f"   【继续】该页面的哈希已被记录，正在执行作答...")

        self.processed_hashes.add(state_key)

        # 快速模式也要能吃到课文上下文：从当前页的 courseResourceId + 面包屑单元名反推目录位置
        try:
            page_text = (self.driver.find_element(By.TAG_NAME, 'body').text or "").lower()
        except Exception:
            page_text = ""
        if 'collocation' in page_text or 'from the texts' in page_text:
            self._load_reading_context_for_current_page()

        success = self._process_current_tab_content(
            chapter_name,
            quick_tab_name,
            0,
            0
        )

        print(f"\n{'=' * 60}")
        print(" 当前页面处理已停止" if self._should_stop() else
              (" 当前页面处理完毕" if success else " 当前页面未处理完成"))
        print(f"{'=' * 60}")
        return success

    def _find_next_question_button(self) -> Optional[Any]:
        """单次 JS 查找可见且可用的「下一题 / 下一页」按钮，替代多轮 Selenium 往返。"""
        js = r"""
        const selectors = [
            '.next-question-btn:not(.disabled)',
            '.btn-next:not([disabled])',
            'button.next:not(.disabled)',
            '.pagination-next:not(.disabled)',
            '.question-next:not(.disabled)',
            '.action.next:not(.disabled)',
            '.submit-bar-pc--btn-next:not(.disabled)',
            '.next-btn:not(.disabled)',
            '[class*="next"]:not(.disabled)',
            '.question-common-course-page a.btn',
            '.question-common-course-page .btn',
            'a.btn',
            'a[class*="btn"]',
        ];
        const isUsable = el => {
            if (!el || !el.getClientRects || !el.getClientRects().length) return false;
            const style = window.getComputedStyle(el);
            if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                return false;
            }
            const cls = (el.className || '').toString().toLowerCase();
            if (el.disabled || cls.includes('disabled')) return false;
            return true;
        };
        const keywords = ['下一题', 'next question', '下一页', 'next'];
        const hasKeyword = text => keywords.some(k => text.includes(k));

        for (const sel of selectors) {
            let nodes;
            try { nodes = document.querySelectorAll(sel); } catch (e) { continue; }
            for (const btn of nodes) {
                if (!isUsable(btn)) continue;
                const text = (btn.innerText || btn.textContent || '').toLowerCase();
                const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                if (hasKeyword(text) || hasKeyword(aria)) {
                    if (!text.includes('submit') && !text.includes('提交')) return btn;
                }
            }
        }
        for (const btn of document.querySelectorAll('button, a')) {
            if (!isUsable(btn)) continue;
            const text = (btn.innerText || btn.textContent || '').toLowerCase();
            if (hasKeyword(text) && !text.includes('submit') && !text.includes('提交')) {
                return btn;
            }
        }
        return null;
        """
        try:
            return self.driver.execute_script(js)
        except Exception as e:
            logger.error(f"查找下一题按钮失败: {e}", exc_info=True)
            return None

    def _generate_content_hash_from_direction(self) -> str:
        if self._is_publish_page():
            # 新版路由包含教材/单元/任务，外壳没有旧版题干；不要白等 3 × 5 秒。
            return hashlib.md5(self._publish_current_url().encode()).hexdigest()[:16]
        try:
            direction_elem = WebDriverHelper.safe_find_element(
                self.driver,
                ['.abs-direction', '.layout-direction-container', '.direction-container']
            )
            if direction_elem:
                text = direction_elem.text.strip()
                if text:
                    return hashlib.md5(text.encode()).hexdigest()[:16]
        except:
            pass
        return "empty"

    def _generate_content_hash(self) -> str:
        if self._is_publish_page():
            return hashlib.md5(self._publish_current_url().encode()).hexdigest()[:16]
        try:
            direction_elem = WebDriverHelper.safe_find_element(
                self.driver,
                ['.abs-direction', '.layout-direction-container', '.discussion-title']
            )
            if direction_elem:
                text = direction_elem.text.strip()
                if text:
                    return hashlib.md5(text.encode()).hexdigest()[:16]

            questions, _ = self.parser.parse_all()
            if questions:
                content = "|".join([f"{q.number}:{q.text[:30]}" for q in questions[:3]])
                return hashlib.md5(content.encode()).hexdigest()[:16]

            body = self.driver.find_element(By.TAG_NAME, 'body').text[:300]
            return hashlib.md5(body.encode()).hexdigest()[:16]

        except Exception as e:
            error_msg = str(e)
            print(f"    生成哈希失败:{error_msg[:50]} ")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return "empty"

    def _get_level1_tabs(self) -> List[Dict]:
        tabs = []
        elements = WebDriverHelper.safe_find_elements(self.driver, Selectors.LEVEL1_TABS)
        seen = set()
        for elem in elements:
            try:
                title = elem.get_attribute('title') or elem.text.strip().split('\n')[0]
                if title and title not in seen and len(title) < 50:
                    seen.add(title)
                    tabs.append({
                        'element': elem,
                        'title': title,
                        'is_active': 'activity' in (elem.get_attribute('class') or '').lower()
                    })
            except:
                continue
        return tabs

    def _get_level2_tabs(self) -> List[Dict]:
        tabs = []
        container = WebDriverHelper.safe_find_element(
            self.driver,
            ['.pc-header-tasks-container', '.pc-header-tasks-layout']
        )
        if not container:
            return tabs
        elements = WebDriverHelper.safe_find_elements(
            self.driver,
            Selectors.LEVEL2_TABS,
            parent=container
        )
        seen = set()
        for elem in elements:
            try:
                title = elem.text.strip().split('\n')[0]
                if title and title not in seen and len(title) < 50:
                    seen.add(title)
                    tabs.append({
                        'element': elem,
                        'title': title,
                        'is_active': 'activity' in (elem.get_attribute('class') or '').lower()
                    })
            except:
                continue
        return tabs

    def _extract_passage(self) -> str:
        selectors = [
            '.question-common-abs-material',
            '.text-material-wrapper',
            '.reading-passage',
            '.passage-content',
        ]

        for selector in selectors:
            try:
                elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for elem in elems:
                    if elem.find_elements(By.TAG_NAME, 'video'):
                        continue
                    text = elem.text.strip()
                    if len(text) > 200:
                        return text
            except:
                continue

        return ""

    def _handle_confirm_dialog_detail(self) -> Dict[str, str]:
        """处理提交确认/提示弹窗并返回详情 {'clicked', 'kind', 'message'}。
        kind:
          - 'confirm'    : 真正的提交确认弹窗（已点击确定/确认）
          - 'onboarding' : 页面引导气泡（.know-box .iKnow），不应误算成已确认提交
          - 'blocked'    : 平台拦截警告弹窗（如「请先作答」「不能为空」且无取消按钮），提交未生效
          - ''           : 无弹窗
        """
        js = r"""
        const clean = t => (t || '').replace(/\s+/g, '').trim();
        const normText = t => (t || '').replace(/\s+/g, ' ').trim();
        const isVisible = el => {
            if (!el || !el.getClientRects || !el.getClientRects().length) return false;
            const style = window.getComputedStyle(el);
            if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                return false;
            }
            const hiddenAncestor = el.closest('.ant-modal-wrap, .ant-modal, [role="dialog"]');
            if (hiddenAncestor) {
                const hs = window.getComputedStyle(hiddenAncestor);
                if (hs && (hs.display === 'none' || hs.visibility === 'hidden')) return false;
            }
            const cls = (el.className || '').toString().toLowerCase();
            if (cls.includes('disabled') || el.disabled) return false;
            return true;
        };
        const clickEl = el => {
            // 只触发一次点击，避免 dispatchEvent('click') + el.click() 双击导致 React 提交回调竞态
            try {
                el.click();
                return;
            } catch (e) {}
            ['mouseover', 'mousedown', 'mouseup', 'click'].forEach(ev => {
                el.dispatchEvent(new MouseEvent(ev, {bubbles: true, cancelable: true, view: window}));
            });
        };
        const modalScopeSelector = '.ant-modal-wrap, .ant-modal, [role="dialog"], .el-message-box, .u-modal, .modal-dialog, .dialog-box, .confirm-dialog, .popup-dialog';
        const strictModalSelector = '.ant-modal-wrap, .ant-modal, [role="dialog"], .el-message-box';
        const readModalContext = (btn) => {
            const box = btn.closest('.ant-modal-content, .ant-modal-confirm-body-wrapper, .ant-modal, .el-message-box, [role="dialog"], .know-box');
            if (!box) return {message: '', hasCancel: false};
            const msgNode = box.querySelector('.ant-modal-confirm-content, .ant-modal-confirm-title, .ant-modal-body, .el-message-box__message, .content, p');
            const message = normText((msgNode ? msgNode.innerText : box.innerText) || '').slice(0, 120);
            const allBtns = Array.from(box.querySelectorAll('button, a, .btn')).filter(isVisible);
            const hasCancel = allBtns.some(b => {
                const t = clean(b.innerText || b.textContent);
                return t === '取消' || t === '返回' || t === '再检查一下' || t.toLowerCase() === 'cancel';
            });
            return {message, hasCancel};
        };
        const classify = (el, sel, label) => {
            if (sel === '.know-box .iKnow' || el.closest('.know-box')) {
                return {clicked: label || '我知道了', kind: 'onboarding', message: readModalContext(el).message};
            }
            const ctx = readModalContext(el);
            const blockedRe = /请先作答|尚未作答|未作答不能提交|请至少选择|不能为空|请完成全部|提交失败|网络异常|请填写答案/;
            if (!ctx.hasCancel && blockedRe.test(ctx.message)) {
                return {clicked: label || sel, kind: 'blocked', message: ctx.message};
            }
            return {clicked: label || sel, kind: 'confirm', message: ctx.message};
        };
        const knownSelectors = [
            '.ant-modal-confirm-btns .ant-btn-primary',
            '.ant-modal-footer .ant-btn-primary',
            '.el-message-box__btns .el-button--primary',
            '.system-info-cloud-ok-button',
            '.know-box .iKnow',
        ];
        const modalConfirmTexts = new Set(['确定', '确认', '确认提交', '继续提交', '仍要提交', '确认交卷', '我知道了', '知道了', '继续', '好的', 'OK', 'ok', 'Confirm', 'confirm']);
        const strictModalTexts = new Set(['提交', '交卷', '提交答案', 'Submit', 'submit']);
        const globalConfirmTexts = new Set(['确定', '确认', '确认提交', '继续提交', '仍要提交', '确认交卷', '我知道了', '知道了', 'OK', 'ok']);

        function tryDoc(doc) {
            for (const sel of knownSelectors) {
                const nodes = doc.querySelectorAll(sel);
                for (const node of nodes) {
                    if (isVisible(node)) {
                        const label = clean(node.innerText || node.textContent);
                        const info = classify(node, sel, label);
                        clickEl(node);
                        return info;
                    }
                }
            }
            const cands = doc.querySelectorAll('button, a, div[role="button"], .btn, span.btn');
            for (const el of cands) {
                if (!isVisible(el)) continue;
                const text = clean(el.innerText || el.textContent);
                if (!text || text.length > 10) continue;
                const inStrictModal = !!el.closest(strictModalSelector);
                const inModal = inStrictModal || !!el.closest(modalScopeSelector);
                if ((inStrictModal && strictModalTexts.has(text))
                        || (inModal && modalConfirmTexts.has(text))
                        || (!inModal && globalConfirmTexts.has(text))) {
                    const info = classify(el, '', text);
                    clickEl(el);
                    return info;
                }
            }
            return null;
        }

        let hit = tryDoc(document);
        if (hit) return hit;
        for (let i = 0; i < window.frames.length; i++) {
            try {
                hit = tryDoc(window.frames[i].document);
                if (hit) return hit;
            } catch (e) {}
        }
        return null;
        """
        try:
            raw = self.driver.execute_script(js)
            if isinstance(raw, dict) and raw.get('clicked'):
                clicked = str(raw.get('clicked') or '')
                kind = str(raw.get('kind') or 'confirm')
                message = str(raw.get('message') or '').strip()
                msg_suffix = f"（提示: {message[:80]}）" if message and message != clicked else ""
                if kind == 'blocked':
                    print(f"   提交被平台弹窗拦截: {clicked}{msg_suffix}")
                elif kind == 'onboarding':
                    print(f"   已关闭引导提示: {clicked}")
                else:
                    print(f"   已点击确认弹窗: {clicked}{msg_suffix}")
                pause = 0.35 if kind == 'onboarding' or clicked in ('我知道了', '知道了', '好的') else 0.55
                if self.stop_requested.wait(pause):
                    return {'clicked': '', 'kind': 'stopped', 'message': ''}
                return {'clicked': clicked, 'kind': kind, 'message': message}
            if isinstance(raw, str) and raw:
                print(f"   已点击确认弹窗: {raw}")
                pause = 0.35 if raw in ('我知道了', '知道了', '好的') else 0.55
                if self.stop_requested.wait(pause):
                    return {'clicked': '', 'kind': 'stopped', 'message': ''}
                kind = 'onboarding' if raw in ('我知道了', '知道了') else 'confirm'
                return {'clicked': raw, 'kind': kind, 'message': ''}
        except Exception:
            pass
        return {'clicked': '', 'kind': '', 'message': ''}

    def _handle_confirm_dialog(self) -> bool:
        """处理提交确认/提示弹窗（兼容 Ant Design "确 定" 空格、div/a 按钮及 iframe）。"""
        detail = self._handle_confirm_dialog_detail()
        return bool(detail.get('clicked')) and detail.get('kind') != 'stopped'

    def _page_needs_answer(self) -> bool:
        """这一页有没有需要作答的控件。"""
        driver = getattr(self, 'driver', None)
        if driver is None:
            return True
        for selector in ANSWER_WIDGET_SELECTORS:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            if not isinstance(elements, (list, tuple)):
                continue
            for element in elements:
                try:
                    if element.is_displayed():
                        return True
                except Exception:
                    continue
        return False

    def _release_transcriber(self):
        transcriber = self._get_transcriber()
        if transcriber is None:
            return
        try:
            transcriber.release_local_model()
        except Exception as exc:
            print(f"   释放 Whisper 模型失败: {str(exc)[:60]}")

    def _skip_transcription(self, tab_name: str = "") -> bool:
        """只要求听/看的页面（Listening skills 等）不做 ASR，有作答控件就照旧转写。"""
        title = (tab_name or "").lower()
        retry = 0.6 if any(word in title for word in SKIP_ASR_TITLE_KEYWORDS) else 0.0
        for attempt in range(3):
            if self._page_needs_answer():
                return False
            if not retry or attempt == 2:
                break
            time.sleep(retry)
        # 批量运行期间不中途释放 Whisper 模型，避免相邻任务反复卸载/重载（批量结束由 process_selected_tabs 统一释放）。
        return True

    def _question_screenshot(self, questions) -> Optional[bytes]:
        """题目区域截图，给支持识图的模型补上版面结构。"""
        if not getattr(self.config, 'screenshot', False):
            return None
        elements = [getattr(q, 'element', None) for q in questions]
        elements = [element for element in elements if element is not None]
        if not elements:
            return None
        try:
            size = self.driver.execute_script("return [window.innerWidth, window.innerHeight];")
            self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                'width': max(1400, int(size[0])), 'height': max(1200, int(size[1])),
                'deviceScaleFactor': 1, 'mobile': False})
            try:
                time.sleep(0.5)
                self.driver.execute_script("""
                    const btn = [...document.querySelectorAll('button, span, div')].find(e => {
                        if (e.children.length || !e.getClientRects().length) return false;
                        const t = (e.innerText || '').trim();
                        return /^(我知道了|知道了)$/.test(t);
                    });
                    if (!btn) return false;
                    const opts = {bubbles: true, cancelable: true, view: window};
                    ['mouseover', 'mousedown', 'mouseup', 'click'].forEach(ev =>
                        btn.dispatchEvent(new MouseEvent(ev, opts)));
                    return true;
                """)
                time.sleep(0.6)
                # 截取所有题目区域的并集。
                rect = self.driver.execute_script("""
                    const els = arguments[0];
                    if (!els.length) return null;
                    els[0].scrollIntoView({block: 'start'});
                    const sx = window.scrollX, sy = window.scrollY;
                    let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
                    for (const el of els) {
                        for (const target of [el, ...el.querySelectorAll('table')]) {
                            const r = target.getBoundingClientRect();
                            if (!r.width || !r.height) continue;
                            left = Math.min(left, r.left + sx);
                            top = Math.min(top, r.top + sy);
                            right = Math.max(right, r.right + sx);
                            bottom = Math.max(bottom, r.bottom + sy);
                        }
                    }
                    if (!isFinite(left) || !isFinite(top)) return null;
                    return {x: Math.max(0, left), y: Math.max(0, top),
                            w: right - left, h: Math.min(bottom - top, 6000)};
                """, elements)
                if not rect:
                    return None
                width, height = int(rect.get('w') or 0), int(rect.get('h') or 0)
                if width < 40 or height < 20:
                    return None
                scale = 1.5 if width <= 900 else 1.0
                shot = self.driver.execute_cdp_cmd('Page.captureScreenshot', {
                    'format': 'png',
                    'captureBeyondViewport': True,
                    'clip': {'x': rect['x'], 'y': rect['y'], 'width': width,
                             'height': height, 'scale': scale},
                })
                return base64.b64decode(shot.get('data') or '')
            finally:
                self.driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
                time.sleep(0.3)
        except Exception as exc:
            print(f"    题目截图失败: {str(exc)[:60]}")
            return None

    def _preprocess_audio_if_needed(self, tab_name: str, l1_idx: int, l2_idx: int,
                                    skip_transcription: Optional[bool] = None,
                                    on_transcript: Optional[Callable[[str], None]] = None):
        """检测并预处理音频：下载+转录，将转录文本注入 AI 上下文"""
        if self._should_stop():
            return
        if self._has_video_on_page():
            return

        if skip_transcription is None:
            skip_transcription = self._skip_transcription(tab_name)
        if skip_transcription:
            print("   该页不需要作答，跳过音频转写")
            return

        # 纯听音频任务只播放，跟读题直接使用样音，均不需要转写。
        if find_record_buttons(self.driver) or AudioStrategy.is_audio_only_page(self.driver):
            return

        if not self._has_audio_on_page():
            return

        try:
            audio_urls = self._extract_audio_urls_from_page()
            if not audio_urls:
                print("   未找到有效音频URL，跳过")
                return

            cache = getattr(self, '_audio_transcript_cache', None)
            if cache is None:
                cache = self._audio_transcript_cache = {}
            section_cache = getattr(self, '_section_transcript_cache', None)
            if section_cache is None:
                section_cache = self._section_transcript_cache = {}
            section_key = getattr(self, '_current_section_key', None)

            def _record_section(text: str):
                if not text or not section_key:
                    return
                bucket = section_cache.setdefault(section_key, [])
                if text not in bucket:
                    bucket.append(text)

            # 同小节的 Exercise 2 常复用 Exercise 1 的同一条音频 URL；
            # 任务切换时会 force_reset 清空对话上下文，这里必须把已缓存的转写重新注入 AI！
            injected_any = False
            pending = []
            reused_count = 0
            for url in audio_urls:
                audio_key = url.split('#')[0]
                if audio_key in cache:
                    cached_text = cache[audio_key]
                    if cached_text:
                        self.ai_client.add_audio_transcript_if_new(cached_text)
                        _record_section(cached_text)
                        reused_count += 1
                        injected_any = True
                elif audio_key not in self._processed_audio_tabs:
                    pending.append(url)
            if reused_count:
                print(f"   复用已缓存音频转写（{reused_count} 条）")

            if pending:
                transcriber = self.video_handler.transcriber
                prefetched: Dict[int, Any] = {}
                consumed_paths = set()
                dl_pool = (
                    ThreadPoolExecutor(max_workers=min(4, len(pending)))
                    if len(pending) > 1 and hasattr(transcriber, 'download_audio')
                    else None
                )
                try:
                    if dl_pool is not None:
                        for i, u in enumerate(pending, 1):
                            prefetched[i] = dl_pool.submit(transcriber.download_audio, u)

                    for index, audio_url in enumerate(pending, 1):
                        if self._should_stop():
                            return
                        audio_key = audio_url.split('#')[0]
                        print(f"   音频转写 {index}/{len(pending)}...")
                        local_path = None
                        if index in prefetched:
                            try:
                                local_path = prefetched[index].result()
                            except Exception:
                                local_path = None
                        if local_path:
                            consumed_paths.add(local_path)
                            transcript = transcriber.transcribe(
                                audio_url, language="en", predownloaded_path=local_path)
                        else:
                            transcript = transcriber.transcribe(audio_url, language="en")

                        if self._should_stop():
                            return
                        if transcript:
                            cache[audio_key] = transcript
                            self.ai_client.add_audio_transcript_if_new(transcript)
                            _record_section(transcript)
                            injected_any = True
                            print(f"   音频转写完成（{len(transcript)}字符）")
                        else:
                            cache[audio_key] = ""
                            print("   音频转写为空")

                        self._processed_audio_tabs.add(audio_key)
                finally:
                    if dl_pool is not None:
                        dl_pool.shutdown(wait=False, cancel_futures=True)
                    # 若中途停止/异常，清理已下载但未消费的临时音频文件
                    for fut in prefetched.values():
                        if fut.done() and not fut.cancelled():
                            try:
                                p = fut.result()
                                if p and p not in consumed_paths and os.path.exists(p):
                                    os.remove(p)
                            except Exception:
                                pass

            if injected_any and on_transcript:
                try:
                    on_transcript("audio")
                except Exception as exc:
                    print(f"   预取答案启动失败: {str(exc)[:60]}")

        except Exception as e:
            print(f"   预处理失败: {str(e)[:80]}")
            logger.error(f"音频预处理异常: {e}", exc_info=True)

    def _has_audio_on_page(self) -> bool:
        """检测页面是否包含音频元素或音频材料"""
        try:
            audios = self.driver.find_elements(By.TAG_NAME, 'audio')
            if any(a.is_displayed() or a.get_attribute('src') for a in audios):
                return True

            audio_containers = self.driver.find_elements(By.CSS_SELECTOR, '.audio-material-wrapper, .question-audio')
            if audio_containers:
                return True

            try:
                direction = self.driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
                text = direction.text.lower()
                if any(kw in text for kw in ['listen', 'audio', 'hear', 'talk', 'conversation']):
                    if self._extract_audio_url_from_page():
                        return True
            except:
                pass

            return False
        except:
            return False

    def _extract_audio_urls_from_page(self) -> List[str]:
        urls: List[str] = []
        try:
            elems = self.driver.find_elements(By.CSS_SELECTOR, 'audio, audio source')
        except Exception:
            return urls
        for elem in elems:
            try:
                src = (elem.get_attribute('src') or '').strip()
            except Exception:
                continue
            if not src:
                continue
            key = src.split('#')[0]
            if key and key not in urls:
                urls.append(key)
        return urls

    def _extract_audio_url_from_page(self) -> Optional[str]:
        urls = self._extract_audio_urls_from_page()
        return urls[0] if urls else None

    def _preprocess_video_if_needed(self, tab_name: str, l1_idx: int, l2_idx: int,
                                    skip_transcription: Optional[bool] = None,
                                    on_transcript: Optional[Callable[[str], None]] = None) -> str:
        """预处理当前视频，返回加入本页上下文的转写。"""
        if self._should_stop() or not self._has_video_on_page():
            return ""

        if skip_transcription is None:
            skip_transcription = self._skip_transcription(tab_name)
        skip_transcription = bool(skip_transcription)
        self.video_handler.skip_transcription = skip_transcription

        try:
            video_info = self.video_handler._get_video_info()
            video_url = (video_info or {}).get('url', '')
            video_key = video_url.split('#')[0] if video_url else f"{tab_name}|{l1_idx}|{l2_idx}"
            if video_key in self._processed_video_tabs or video_key in self._skipped_video_tabs:
                if skip_transcription or not video_url:
                    return ""
                transcript = self.video_handler.ensure_transcript(video_url)
                if not transcript or self._should_stop():
                    return ""
                self.ai_client.add_video_transcript_if_new(transcript)
                self._skipped_video_tabs.discard(video_key)
                self._processed_video_tabs.add(video_key)
                if on_transcript:
                    on_transcript(transcript)
                return transcript

            if skip_transcription:
                print("   只播放，不转写")
            else:
                print("   播放并转写视频...")
            injected = {'done': False}

            def _inject_transcript_then_notify(transcript):
                """先把转写写进 AI 上下文，再启动答案预取。

                预取与视频播放并行，如果启动时上下文里还没有视频转写，
                播放前已出现的题目就会只带着题干去请求答案，而这份缺内容
                的预取结果后面还会被直接使用。
                """
                if transcript:
                    self.ai_client.add_video_transcript_if_new(transcript)
                    injected['done'] = True
                if on_transcript:
                    on_transcript(transcript)

            self.video_handler._play_video_and_handle_popups(
                on_transcript=_inject_transcript_then_notify)

            if self._should_stop():
                return ""
            transcript = self.video_handler.video_transcript
            if transcript:
                if not injected['done']:
                    self.ai_client.add_video_transcript_if_new(transcript)
                section_key = getattr(self, '_current_section_key', None)
                if section_key:
                    bucket = self._section_transcript_cache.setdefault(section_key, [])
                    if transcript not in bucket:
                        bucket.append(transcript)
                print(f"   视频转写完成（{len(transcript)}字符）")
                self._processed_video_tabs.add(video_key)
            else:
                self._skipped_video_tabs.add(video_key)
                if not skip_transcription:
                    print("   视频转写失败")
            return transcript or ""

        except Exception as e:
            print(f"   预处理失败: {str(e)[:80]}")
            logger.error(f"视频预处理异常: {e}", exc_info=True)
            return ""

    def _has_video_on_page(self) -> bool:
        """检测可见视频，兼容无 <video> 标签的播放器。"""
        try:
            videos = self.driver.find_elements(By.TAG_NAME, 'video')
            if not videos:
                videos = self.driver.find_elements(
                    By.CSS_SELECTOR, '.video-js, .video-box, .question-video-player')
            return any(v.is_displayed() for v in videos)
        except Exception:
            return False

    def _get_submit_button_state(self) -> str:
        """
        检查提交按钮及加载态，区分三种状态：
          - 'active' : 可点击的提交按钮仍在页面上（尚未提交或校验未通过）
          - 'loading': 提交按钮处于 loading/disabled 状态或页面正在提交转圈（请求在途，继续等！）
          - 'gone'   : 提交按钮已真正消失且无加载遮罩
        """
        js = r"""
        const clean = t => (t || '').replace(/\s+/g, '').trim();
        const isVisible = el => {
            if (!el || !el.getClientRects || !el.getClientRects().length) return false;
            const style = window.getComputedStyle(el);
            return !!(style && style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0');
        };
        const isDisabledOrLoading = el => {
            const cls = (el.className || '').toString().toLowerCase();
            if (el.disabled || el.getAttribute('disabled') !== null) return true;
            if (el.getAttribute('aria-disabled') === 'true') return true;
            return cls.includes('disabled') || cls.includes('ban') || cls.includes('loading');
        };
        const badWords = ['未完成', '已完成', '取消', '返回', '重做', '再做一次', '重新作答', '再试一次', '下一题', '下一页', '上一题', '上一页', 'next', 'prev'];
        const submitWords = ['提交', '确认提交', '完成', 'submit', 'done', 'finish'];
        const isSubmit = el => {
            const raw = (el.innerText || el.textContent || el.value || '').toLowerCase();
            const compact = clean(raw);
            if (badWords.some(w => compact.includes(w) || raw.includes(w))) return false;
            if (submitWords.some(w => compact.includes(w) || raw.includes(w))) return true;
            return el.tagName === 'BUTTON' && (el.getAttribute('type') || '').toLowerCase() === 'submit';
        };
        const selectors = [
            '.submit-bar-pc--btn-1_Xvo',
            'button.submit-btn',
            'button[type="submit"]',
            '.question-common-course-page a.btn',
            '.question-common-course-page .btn',
            'a.btn',
            '.btn',
        ];
        let sawLoading = false;
        const inspect = el => {
            if (!isVisible(el)) return '';
            if (el.closest('.ant-modal-wrap, .ant-modal, [role="dialog"]')) return '';
            if (isDisabledOrLoading(el)) {
                sawLoading = true;
                return '';
            }
            return 'active';
        };
        for (const sel of selectors) {
            let nodes;
            try { nodes = document.querySelectorAll(sel); } catch (e) { continue; }
            for (const el of nodes) {
                if (!isSubmit(el)) continue;
                if (inspect(el) === 'active') return 'active';
            }
        }
        for (const el of document.querySelectorAll('button, a, div, span')) {
            if (!isVisible(el)) continue;
            if (el.closest('.ant-modal-wrap, .ant-modal, [role="dialog"]')) continue;
            const text = clean(el.innerText);
            if (text !== '提交' && text !== '交卷' && text !== '确认提交') continue;
            if (el.querySelector('button, a, div')) continue;
            if (inspect(el) === 'active') return 'active';
        }
        if (sawLoading || document.querySelector('.ant-spin-spinning')) return 'loading';
        return 'gone';
        """
        try:
            state = self.driver.execute_script(js)
            if state in ('active', 'loading', 'gone'):
                return state
        except Exception:
            pass
        return 'active' if self._find_visible_submit_button() is not None else 'gone'

    def _post_submit_diagnostic(self) -> str:
        """提交按钮消失但未检测到分数/重做/解析时，采集一行精简诊断（可见按钮、提示与页面摘要）。"""
        js = r"""
        const clean = t => (t || '').replace(/\s+/g, ' ').trim();
        const visible = el => {
            if (!el || !el.getClientRects || !el.getClientRects().length) return false;
            const st = window.getComputedStyle(el);
            return !!(st && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0');
        };
        const btns = [];
        const sel = 'button, a, [role="button"], div[class*="submit-bar"], div[class*="btn"], span[class*="btn"], [class*="footer"] span, [class*="footer"] div';
        for (const el of document.querySelectorAll(sel)) {
            if (!visible(el)) continue;
            if (el.querySelector && el.querySelector('button, a')) continue;
            const t = clean(el.innerText || el.textContent);
            if (t && t.length <= 18 && !btns.includes(t)) btns.push(t);
            if (btns.length >= 12) break;
        }
        const toasts = [];
        for (const el of document.querySelectorAll('.ant-message, .ant-notification, .ant-alert, [role="alert"], .toast, [class*="toast"]')) {
            if (!visible(el)) continue;
            const t = clean(el.innerText || el.textContent);
            if (t && !toasts.includes(t)) toasts.push(t.slice(0, 60));
            if (toasts.length >= 3) break;
        }
        let summary = '';
        const bodyText = clean((document.body && document.body.innerText) || '');
        const idx = bodyText.indexOf('返回修改');
        if (idx >= 0) {
            summary = bodyText.slice(Math.max(0, idx - 120), idx + 120);
        } else if (bodyText) {
            summary = bodyText.slice(-180);
        }
        return '按钮=' + JSON.stringify(btns)
            + (toasts.length ? ' 提示=' + JSON.stringify(toasts) : '')
            + (summary ? ' 摘要=' + JSON.stringify(summary) : '');
        """
        try:
            res = self.driver.execute_script(js)
            return res if isinstance(res, str) else ''
        except Exception:
            return ''

    @staticmethod
    def _has_legacy_review(text: str) -> bool:
        labels = {line.strip() for line in text.splitlines()}
        return {'答题情况', '继续学习'}.issubset(labels)

    def _legacy_unreviewed_url(self) -> str:
        """Record the current legacy task only if its result view is absent before clicking."""
        try:
            url = self.driver.current_url
            if urlsplit(url).hostname == 'ucontent.unipus.cn':
                body = self.driver.find_element(By.TAG_NAME, 'body').text
                if not self._has_legacy_review(body):
                    return url
        except (ValueError, WebDriverException):
            pass
        return ''

    def _wait_for_submit_complete(self, timeout: int = 18, *, legacy_unreviewed_url: str = '') -> bool:
        """等待本次提交的页面回执，课程完成状态随后由学习记录核验。"""
        if self.stop_requested.wait(0.35):
            return False
        confirmed = False
        start = time.time()

        while time.time() - start < timeout and not self._should_stop():
            try:
                dialog = self._handle_confirm_dialog_detail()
                if dialog.get('kind') == 'stopped':
                    return False
                if dialog.get('clicked'):
                    kind = dialog.get('kind')
                    if kind == 'blocked':
                        return False
                    if kind == 'confirm':
                        confirmed = True
                    continue

                state = self._get_submit_button_state()
                if state == 'gone':
                    if (self._read_task_score() is not None
                            or self._has_redo_button()
                            or bool(self._read_answer_analysis())):
                        print("   已收到页面作答回执，待学习记录确认")
                        self._handle_confirm_dialog()
                        return True
                if state == 'gone' or (confirmed and state == 'active'):
                    body_text = self.driver.find_element(By.TAG_NAME, 'body').text
                    if PublishPlayer.has_submission_status(body_text):
                        print("   已检测到提交回执提示，待学习记录确认")
                        return True
                    if (legacy_unreviewed_url and self.driver.current_url == legacy_unreviewed_url
                            and self._has_legacy_review(body_text)):
                        self._submission_unconfirmed = True
                        print("   已显示本次答题情况，待 U 校园学习记录确认")
                        return False

            except Exception:
                pass

            if self.stop_requested.wait(0.3):
                return False

        self._submission_unconfirmed = not self._should_stop()
        diag = self._post_submit_diagnostic()
        print(f"   等待超时（{timeout}s），未收到提交回执，待学习记录确认"
              + (f"（{diag}）" if diag else ""))
        return False

    def _find_visible_submit_button(self) -> Optional[Any]:
        """单次 JS 查找当前页可见且可点击的提交按钮，避免轮询时多次 Selenium 往返。"""
        js = r"""
        const clean = t => (t || '').replace(/\s+/g, '').trim();
        const isVisible = el => {
            if (!el || !el.getClientRects || !el.getClientRects().length) return false;
            const style = window.getComputedStyle(el);
            if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                return false;
            }
            return true;
        };
        const isDisabled = el => {
            const cls = (el.className || '').toString().toLowerCase();
            if (el.disabled || el.getAttribute('disabled') !== null) return true;
            if (el.getAttribute('aria-disabled') === 'true') return true;
            return cls.includes('disabled') || cls.includes('ban');
        };
        const badWords = ['未完成', '已完成', '取消', '返回', '重做', '再做一次', '下一题', '下一页', '上一题', '上一页', 'next', 'prev'];
        const submitWords = ['提交', '确认提交', '完成', 'submit', 'done', 'finish'];
        const isSubmit = el => {
            const raw = (el.innerText || el.textContent || el.value || '').toLowerCase();
            const compact = clean(raw);
            if (badWords.some(w => compact.includes(w) || raw.includes(w))) return false;
            if (submitWords.some(w => compact.includes(w) || raw.includes(w))) return true;
            return el.tagName === 'BUTTON' && (el.getAttribute('type') || '').toLowerCase() === 'submit';
        };
        const selectors = [
            '.submit-bar-pc--btn-1_Xvo',
            'button.submit-btn',
            'button[type="submit"]',
            '.question-common-course-page a.btn',
            '.question-common-course-page .btn',
            'a.btn',
            '.btn',
        ];
        for (const sel of selectors) {
            let nodes;
            try { nodes = document.querySelectorAll(sel); } catch (e) { continue; }
            for (const el of nodes) {
                if (!isVisible(el) || isDisabled(el)) continue;
                if (el.closest('.ant-modal-wrap, .ant-modal, [role="dialog"]')) continue;
                if (isSubmit(el)) return el;
            }
        }
        for (const el of document.querySelectorAll('button, a, div, span')) {
            if (!isVisible(el) || isDisabled(el)) continue;
            if (el.closest('.ant-modal-wrap, .ant-modal, [role="dialog"]')) continue;
            const text = clean(el.innerText);
            if (text !== '提交' && text !== '交卷' && text !== '确认提交') continue;
            if (el.querySelector('button, a, div')) continue;
            return el;
        }
        return null;
        """
        try:
            return self.driver.execute_script(js)
        except Exception:
            return None

    def _wait_for_content_change(self, previous_signature: str, timeout: int = 10) -> bool:
        start_time = time.time()
        check_interval = 0.5

        while time.time() - start_time < timeout and not self._should_stop():
            try:
                questions, _ = self.parser.parse_all()
                current_signature = self._generate_questions_signature(questions)

                if current_signature != previous_signature and current_signature != "empty":
                    print(f"         内容已变化: {previous_signature[:8]}... -> {current_signature[:8]}...")
                    return True

            except Exception as e:
                logger.debug(f"等待内容变化时出错: {e}")

            if self.stop_requested.wait(check_interval):
                return False

        print(f"         等待内容变化超时")
        return False


class UCampusBot:
    """U校园机器人 - 组装所有组件"""

    def __init__(self, config_path: str = 'config.json', skip_check: bool = False):
        self.config = Config.from_json(config_path)
        # 门禁与 driver 共用同一份规格，避免两边指向不同浏览器
        self.browser = get_spec(self.config.browser)
        self._shutdown_event = threading.Event()
        self._background_threads = []
        self.driver = None

        if not skip_check:
            self._ensure_environment()

        self.driver = self._create_driver()
        self.popup_watcher = PopupWatcher(self.driver)

    def _ensure_environment(self):
        checker = EnvironmentChecker(self.browser.key)

        if not checker.check_all():
            while True:
                choice = checker.show_fix_guide()

                if choice == '1':
                    checker.auto_install_browser()
                    sys.exit(0)

                elif choice == '2':
                    if checker.auto_install_ffmpeg():
                        sys.exit(0)

                elif choice == '3':
                    if checker.add_ffmpeg_to_path():
                        sys.exit(0)

                elif choice == '4':
                    ffmpeg_path = checker.manual_specify_path()
                    if ffmpeg_path:
                        bin_dir = os.path.dirname(ffmpeg_path)
                        checker._add_to_system_path(bin_dir)
                        print(f" FFmpeg 已添加到 PATH: {bin_dir}")
                        print("请重新运行程序")
                        input("按回车键退出...")
                        sys.exit(0)

                elif choice == '5':
                    self._show_detailed_help()
                    input("\n按回车键退出...")
                    sys.exit(1)

                elif choice == 'Q':
                    sys.exit(1)

                else:
                    print("无效选项，请重新选择")

    def _show_detailed_help(self):
        name = self.browser.display_name
        hints = " / ".join(self.browser.manual_install_hint)
        if self.browser.supports_auto_install:
            browser_fix = f"       解决：选择 [1] 自动安装，或手动访问 {hints}"
        else:
            browser_fix = f"       解决：这个浏览器不提供自动安装，请手动下载：{hints}"

        print(f"""
    【问题诊断】

    1. {name} 浏览器问题
       原因：{name} 未安装，或安装在非默认位置
{browser_fix}
       如需改用另一种浏览器：在 config.json 里修改 "browser"（edge / chrome）后重启

    2. FFmpeg 问题（语音识别必需）
       原因：未安装 FFmpeg 或未添加到系统 PATH
       解决：
          - 方法A（推荐）：选择 [2] 自动下载安装（约130MB）
          - 方法B：选择 [3] 将已安装的 FFmpeg 添加到 PATH
          - 方法C：手动下载 https://ffmpeg.org/download.html
            解压后将 bin 目录添加到系统环境变量 PATH

    3. 验证 FFmpeg 安装
       打开 CMD 输入: ffmpeg -version
       应显示版本信息，如 "ffmpeg version 6.0"

    【手动安装 FFmpeg 步骤】

    1. 访问 https://ffmpeg.org/download.html
    2. 点击 Windows 图标，选择 "Windows builds from gyan.dev"
    3. 下载 "ffmpeg-release-essentials.zip"
    4. 解压到 C:\ffmpeg
    5. 将 C:\ffmpeg\bin 添加到系统环境变量 PATH
    6. 重启终端，输入 ffmpeg -version 验证
    """)

    COMPAT_BROWSER_ARGS = ('--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                           '--disable-extensions')

    # 录音题需要：不再弹麦克风授权框 + AudioContext 无需用户手势即可启动
    MIC_BROWSER_ARGS = ('--use-fake-ui-for-media-stream',
                        '--autoplay-policy=no-user-gesture-required')

    # 防后台标签页休眠与渲染降频：切窗口时确保正常视频播放不会无故停在半路
    MEDIA_KEEPALIVE_BROWSER_ARGS = (
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding',
        '--disable-features=CalculateNativeWinOcclusion',
    )

    def _start_browser(self, temp_dir: str, extra_args=()):
        """按指定临时目录启动浏览器，并注入反检测脚本。"""
        options_cls, webdriver_cls, service_cls = driver_api(self.browser)
        options = options_cls()

        # 环境检查与 driver 读同一条检测结果：找到就用它启动，
        # 找不到才退到 Selenium Manager 自己解析。
        install_path = self.browser.detect_install_path()
        if install_path:
            options.binary_location = install_path

        options.add_argument('--disable-blink-features=AutomationControlled')
        # ipublish 播放器页面常有个别第三方资源一直不结束，默认的 normal 策略会让
        # driver.get() 一直等到整页 load 完成，进而触发 120~150s 读超时。
        # 改成 eager：DOMContentLoaded 后立即返回，后续由页面自身继续加载。
        try:
            options.page_load_strategy = 'eager'
        except Exception as exc:
            logger.warning(f"设置 page_load_strategy 失败: {str(exc)[:80]}")
        # 新版播放器在窄窗口下会使用需要原生插件的平板录音器。
        options.add_argument('--window-size=1600,1000')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        for argument in self.MIC_BROWSER_ARGS:
            options.add_argument(argument)
        for argument in self.MEDIA_KEEPALIVE_BROWSER_ARGS:
            options.add_argument(argument)
        for argument in extra_args:
            options.add_argument(argument)

        try:
            user_data_dir = tempfile.mkdtemp(prefix='ucampus_', dir=temp_dir or None)
        except Exception:
            user_data_dir = tempfile.mkdtemp(prefix='ucampus_')
        options.add_argument(f'--user-data-dir={user_data_dir}')
        if temp_dir:
            cache_dir = os.path.join(temp_dir, self.browser.profile_dir_name)
            options.add_argument(f'--disk-cache-dir={cache_dir}')
            options.add_argument(f'--crash-dumps-dir={temp_dir}')

        service_options = {}
        if sys.platform == 'win32':
            # Let Python handle Ctrl+C and close the driver in order; the driver
            # must not receive the console interrupt before cleanup starts.
            service_options['popen_kw'] = {'creation_flags': subprocess.CREATE_NEW_PROCESS_GROUP}
        try:
            driver = webdriver_cls(options=options, service=service_cls(**service_options))
        except NoSuchDriverException:
            if not install_path:
                raise
            # 指定了路径反而拿不到 driver（该路径不是 Selenium Manager 认识的
            # 发行版，或探测到的那一刻文件正好不可用）时，退回自动解析，
            # 避免这条分支比「不指定路径」更容易失败。
            print(f" 按 {install_path} 取不到 driver，改由自动解析重试...")
            options.binary_location = ""
            driver = webdriver_cls(options=options, service=service_cls(**service_options))
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator, 'webdriver', {get: () => false});"
            })
        except BaseException:
            # A failed tab can still leave a live browser behind. Close this
            # attempt before the caller retries with compatibility options.
            try:
                driver.quit()
            except Exception:
                pass
            raise
        # 视频播放等长操作会让单条 WebDriver 命令超过默认的 120s 读超时，
        # 一旦超时整条批量处理就中断。把读超时放宽到 10 分钟。
        try:
            # 1x 正常播放的视频可能长达 5~10 分钟，读超时必须大于脚本等待时间，否则会误判超时
            driver.command_executor.set_timeout(900)
        except Exception as exc:
            logger.warning(f"设置 WebDriver 读超时失败: {str(exc)[:80]}")
        # 把麦克风轨道换成「页面自带原声」的载体，供跟读录音题使用
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": RECORD_MIC_BOOTSTRAP_JS
            })
        except Exception as exc:
            logger.warning(f"录音注入脚本安装失败: {str(exc)[:100]}")
        return driver

    def _create_driver(self):
        # 系统临时目录不可写时，浏览器会报
        # "cannot create temp dir for unpacking extensions"，这里统一换成可写目录
        temp_dir = ensure_writable_temp_dir()

        # Selenium Manager 的驱动缓存也放到可写目录，避免用户目录 .cache 拒绝访问
        try:
            selenium_cache = os.path.join(BASE_DIR, 'logs', 'selenium_cache')
            os.makedirs(selenium_cache, exist_ok=True)
            os.environ.setdefault('SE_CACHE_PATH', selenium_cache)
        except Exception:
            pass

        try:
            return self._start_browser(temp_dir)
        except NoSuchDriverException as exc:
            # 通常意味着浏览器本身没装（或装在非默认路径），重试也没用，直接指路
            print(f"\n 找不到 {self.browser.display_name} 的 driver：{str(exc)[:120]}")
            print(f"   请确认已安装 {self.browser.display_name}，或在 config.json 里改 \"browser\"（edge / chrome）")
            for hint in self.browser.manual_install_hint:
                print(f"   下载：{hint}")
            raise
        except (SessionNotCreatedException, WebDriverException) as exc:
            # 受限环境（沙箱/受限用户/显卡驱动异常）下首次启动可能失败，降级重试一次：
            # 换新的临时 profile 并加上兼容参数
            print(f" 启动 {self.browser.display_name} 失败（{str(exc)[:120]}），尝试兼容模式重试...")
            return self._start_browser(temp_dir, extra_args=self.COMPAT_BROWSER_ARGS)
        except Exception as exc:
            # 驱动握手超时（urllib3 ReadTimeoutError）等非 WebDriverException 也要重试
            print(f" 启动 {self.browser.display_name} 超时（{str(exc)[:120]}），尝试兼容模式重试...")
            return self._start_browser(temp_dir, extra_args=self.COMPAT_BROWSER_ARGS)

    def start(self):
        solver = AISolver(self.driver, self.config)

        from unipus_helper.native_ui import NativeModernGUI
        self.gui = NativeModernGUI(self.driver, solver, self, globals())

        self._background_threads = [
            threading.Thread(target=self.popup_watcher.run, daemon=True, name="popup-watcher"),
            threading.Thread(target=self._background_login_flow, daemon=True, name="ucampus-login"),
        ]
        for worker in self._background_threads:
            worker.start()

        try:
            self.gui.mainloop()
        finally:
            self.request_shutdown()

        return True

    def request_shutdown(self):
        self._shutdown_event.set()
        self.popup_watcher.stop()

    def restart_browser(self):
        """关掉当前课程浏览器再拉起一个新的，返回新的 driver。"""
        old, self.driver = self.driver, None
        self.popup_watcher.stop()
        if old is not None:
            def quit_old():
                try:
                    old.quit()
                except Exception:
                    pass
            worker = threading.Thread(target=quit_old, daemon=True, name="browser-quit")
            worker.start()
            worker.join(timeout=6)
            if worker.is_alive():
                service = getattr(old, "service", None)
                process = getattr(service, "process", None)
                if process and process.poll() is None:
                    try:
                        if sys.platform == 'win32':
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                           capture_output=True, timeout=3,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                        else:
                            process.terminate()
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        self.driver = self._create_driver()
        self.popup_watcher = PopupWatcher(self.driver)
        threading.Thread(target=self.popup_watcher.run, daemon=True, name="popup-watcher").start()
        return self.driver

    def _background_login_flow(self):
        gui_log_queue.put(" 正在与 U校园 建立连接，请稍候...")
        try:
            success = self._login()
        except Exception as exc:
            if not self._shutdown_event.is_set():
                logger.error(f"登录线程异常: {exc}", exc_info=True)
            return
        if success and not self._shutdown_event.is_set():
            self.gui.after(0, self.gui.enable_scan_button)

    def _await_portal_entry(self, timeout: int = 180) -> None:
        """等到门户可用的首页并清掉一次性 ticket。

        2026-10 版门户的行为：SSO 跳回来带着 ?ticket=ST-...，前端用这张票去
        sso.unipus.cn/sso/serviceTicket/validate 换 token。票是一次性的，且前端
        调用时如缺 service 参数会返回 {"code":"2017"}，补上 service 又会因为票已
        被消费返回 {"code":"3308"} —— 两种情况 SPA 都停在「加载中」，既不会跳转
        也不会写 localStorage，于是登录态永远等不到。SSO 的 jwt cookie 其实已经
        下发，所以这里直接换成不带 ticket 的首页，让 SPA 用会话恢复。
        """
        deadline = time.time() + timeout
        reached = False
        while time.time() < deadline:
            if self._shutdown_event.is_set():
                raise TimeoutException("已停止，放弃等待门户跳转")
            url = ""
            try:
                url = self.driver.current_url or ""
            except WebDriverException:
                time.sleep(1)
                continue
            if url.startswith("https://ucloud.unipus.cn"):
                reached = True
                break
            # 票已下发、jwt cookie 已存在，就不用死等 SPA 自己跳转
            if self._has_live_auth(self.driver):
                reached = True
                break
            time.sleep(1)

        if not reached:
            raise TimeoutException(f"{timeout}s 内没有进入 U校园门户")

        for attempt in range(1, 4):
            url = ""
            try:
                url = self.driver.current_url or ""
            except WebDriverException:
                url = ""
            if "ticket=" in url or not url.startswith(UCLOUD_HOME_URL):
                gui_log_queue.put(" 清理一次性 ticket，重新进入门户首页...")
                try:
                    self.driver.get(UCLOUD_HOME_URL)
                except WebDriverException as exc:
                    gui_log_queue.put(f" 首页导航异常: {str(exc)[:60]}")
            try:
                WebDriverWait(self.driver, 45).until(self._home_ready)
                return
            except TimeoutException:
                if attempt < 3:
                    gui_log_queue.put(f" 门户首页仍在加载，重试 {attempt}/3 ...")
                    time.sleep(2)

        raise TimeoutException("U校园门户首页加载超时（ticket 校验失败或页面卡在加载中）")

    _LOGIN_GATE_JS = """
    return (() => {
      const norm = s => (s || '').replace(/\\s+/g, '');
      const nodes = [...document.querySelectorAll('div,span,a,button')];
      const texts = nodes.map(e => norm(e.innerText || ''));
      const captcha = !!document.querySelector(
          '.geetest_panel, .geetest_slider, .geetest_popup_wrap, iframe[src*="geevisit"], iframe[src*="geetest"]')
          || texts.some(t => t.includes('拖动滑块'));
      const netErr = texts.some(t => t.includes('网络超时'));
      const loginBtn = nodes.find(e => e.tagName === 'BUTTON' && norm(e.innerText) === '登录');
      return {captcha: captcha, netErr: netErr, loginBtn: !!loginBtn};
    })()
    """

    _CLICK_LOGIN_JS = """
    return (() => {
      const norm = s => (s || '').replace(/\\s+/g, '');
      const b = [...document.querySelectorAll('button')].find(x => norm(x.innerText) === '登录');
      if (!b) return 'no-button';
      b.click();
      return 'clicked';
    })()
    """

    _CLICK_RETRY_JS = """
    return (() => {
      const norm = s => (s || '').replace(/\\s+/g, '');
      const hit = [...document.querySelectorAll('div,span,a,button')]
          .filter(e => norm(e.innerText) === '请点击此处重试' || norm(e.innerText) === '刷新验证');
      if (!hit.length) return 'no-retry';
      hit[0].click();
      return 'clicked-retry';
    })()
    """

    _SLIDER_CANVAS_JS = """
    return (() => {
      const out = {};
      for (const c of document.querySelectorAll('canvas')) {
        try { out[c.className] = c.toDataURL('image/png'); } catch (e) {}
      }
      const bg = document.querySelector('.geetest_canvas_bg');
      const btn = document.querySelector('.geetest_slider_button');
      if (bg && btn) {
        const br = bg.getBoundingClientRect(), tr = btn.getBoundingClientRect();
        out.__geo = JSON.stringify({bgw: br.width, canvasW: bg.width,
                                    x: tr.x, y: tr.y, w: tr.width, h: tr.height});
      }
      return out;
    })()
    """

    _SLIDER_REFRESH_JS = """
    return (() => {
      const hit = [...document.querySelectorAll('div,span,a')]
          .filter(e => (e.innerText || '').trim() === '刷新验证');
      if (!hit.length) return false;
      hit[0].click();
      return true;
    })()
    """

    def _auto_solve_slider(self) -> str:
        """尝试自动拖动极验滑块（canvas 差分定位 + ActionChains 拖动）。"""
        try:
            import base64
            import io as _io
            import random
            import numpy as np
            from PIL import Image, ImageChops
            from selenium.webdriver.common.action_chains import ActionChains
            from selenium.webdriver.common.by import By
        except Exception as exc:  # 缺依赖就退回人工
            return "跳过自动过滑块: %s" % exc

        def load(data_url):
            if not isinstance(data_url, str) or not data_url.startswith("data:image"):
                return None
            return Image.open(_io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))

        try:
            data = self.driver.execute_script(self._SLIDER_CANVAS_JS) or {}
            bg = load(data.get("geetest_canvas_bg geetest_absolute"))
            full = load(data.get("geetest_canvas_fullbg geetest_fade geetest_absolute"))
            sl = load(data.get("geetest_canvas_slice geetest_absolute"))
            geo = json.loads(data.get("__geo") or "{}")
            if not (bg and full and sl and geo):
                return "没有可用的验证码画布"
            diff = np.array(ImageChops.difference(bg.convert("RGB"), full.convert("RGB"))).sum(axis=2)
            cols = (diff > 60).sum(axis=0)
            runs, cur = [], None
            for x, c in enumerate(cols):
                if c >= 25:
                    cur = [x, x] if cur is None else [cur[0], x]
                elif cur:
                    runs.append(tuple(cur))
                    cur = None
            if cur:
                runs.append(tuple(cur))
            if not runs:
                return "定位不到凹槽"
            hole_left = float(max(runs, key=lambda r: r[1] - r[0])[0])
            alpha = np.array(sl.convert("RGBA"))[:, :, 3]
            pxs = np.where(alpha > 200)[1]
            piece_left = float(pxs.min()) if len(pxs) else 0.0
            scale = geo["bgw"] / geo["canvasW"]
            delta = (hole_left - piece_left) * scale
            if not (10 < delta < geo["bgw"]):
                return "位移异常: %.1f" % delta

            button = self.driver.find_element(By.CSS_SELECTOR, ".geetest_slider_button")
            ActionChains(self.driver).move_to_element(button).click_and_hold().perform()
            moved = 0.0
            steps = 26
            for i in range(1, steps + 1):
                t = i / steps
                eased = 1 - (1 - t) ** 2.4
                target = delta * eased
                step = target - moved
                moved = target
                ActionChains(self.driver).move_by_offset(step, random.uniform(-0.8, 0.8)).perform()
                time.sleep(random.uniform(0.012, 0.026))
            ActionChains(self.driver).move_by_offset(delta - moved, 0).perform()
            time.sleep(0.1)
            ActionChains(self.driver).release().perform()
            return "已自动拖动 %.1fpx" % delta
        except Exception as exc:  # noqa: BLE001
            return "自动过滑块失败: %s" % str(exc)[:80]

    def _login_gate_state(self) -> dict:
        try:
            return self.driver.execute_script(self._LOGIN_GATE_JS) or {}
        except WebDriverException as exc:
            logger.warning("[login-gate] 读取页面状态失败: %s", str(exc)[:160])
            return {}

    def _handle_login_gate(self, timeout: int = 300) -> bool:
        """登录被极验滑块/网络超时挡住时的兜底处理。

        新版登录页在提交前先过极验：滑块没拖动就不会发出 /sso/0.1/sso/cip/login，
        页面只留一句「网络超时请点击此处重试」，看起来像账号密码错误。这里持续
        轮询页面状态，按情况提示、自动点「重试 / 刷新验证 / 登录」，直到进入门户。
        """
        deadline = time.time() + timeout
        last_hint = 0.0
        last_retry = 0.0
        last_click = time.time()
        seen_captcha = False
        last_beat = time.time()
        logger.info("[login-gate] 开始轮询登录门槛（极验/网络超时），最多 %ss", timeout)

        while time.time() < deadline:
            if self._shutdown_event.is_set():
                return False
            state = self._login_gate_state()
            if not state:
                if time.time() - last_beat > 15:
                    logger.info("[login-gate] 页面状态读取为空，继续等待")
                    last_beat = time.time()
                time.sleep(2)
                continue

            url = ""
            try:
                url = self.driver.current_url or ""
            except WebDriverException:
                url = ""
            if url.startswith("https://ucloud.unipus.cn"):
                return True
            if self._has_live_auth(self.driver):
                return True

            now = time.time()
            if state.get("captcha"):
                seen_captcha = True
                if now - last_hint > 20:
                    gui_log_queue.put("【需要人工】登录页出现极验滑块，请在浏览器中拖动滑块完成验证，程序会自动等你...")
                    auto_result = self._auto_solve_slider()
                    logger.info("[login-gate] %s", auto_result)
                    gui_log_queue.put(" " + auto_result)
                    try:
                        self.driver.switch_to.window(self.driver.current_window_handle)
                        self.driver.maximize_window()
                    except WebDriverException:
                        pass
                    last_hint = now
                    logger.info("[login-gate] 检测到极验滑块，等待人工拖动")
            elif state.get("netErr"):
                if now - last_retry > 10:
                    result = self.driver.execute_script(self._CLICK_RETRY_JS)
                    gui_log_queue.put(f" 登录页提示网络超时，已尝试重试（{result}）")
                    last_retry = now
            elif seen_captcha and state.get("loginBtn") and now - last_click > 8:
                # 滑块过了但没自动提交，补一次点击
                result = self.driver.execute_script(self._CLICK_LOGIN_JS)
                gui_log_queue.put(f" 滑块验证完成，重新提交登录（{result}）")
                last_click = now
            if now - last_beat > 30:
                logger.info("[login-gate] 仍在等待登录，页面状态=%s", state)
                last_beat = now
            time.sleep(2)

        gui_log_queue.put(" 等待登录超时：极验滑块未完成或登录接口一直返回网络超时")
        logger.warning("[login-gate] 等待登录超时退出")
        return False

    def _login(self) -> bool:
        try:
            login_url = self.config.url or DEFAULT_LOGIN_URL
            self.driver.get(login_url)
            try:
                username = WebDriverWait(self.driver, 8).until(
                    EC.element_to_be_clickable((By.ID, 'username')))
            except TimeoutException:
                authenticated = False
                if self.driver.current_url.startswith("https://ucloud.unipus.cn/"):
                    try:
                        WebDriverWait(self.driver, 45).until(self._has_live_auth)
                        authenticated = True
                    except TimeoutException:
                        pass

                if authenticated:
                    username = None
                    gui_log_queue.put(" 检测到已有 U校园 登录会话，继续使用当前会话。")
                elif login_url != DEFAULT_LOGIN_URL:
                    gui_log_queue.put(" 登录地址未显示账号表单，正在使用兼容登录入口...")
                    self.driver.get(DEFAULT_LOGIN_URL)
                    username = WebDriverWait(self.driver, 120).until(
                        EC.element_to_be_clickable((By.ID, 'username')))
                else:
                    raise

            if username is not None:
                password = WebDriverWait(self.driver, 20).until(
                    EC.element_to_be_clickable((By.ID, 'password')))
                agreement_check = self.driver.find_element(By.ID, 'agreement')
                # 「记住我」会把上次账号预填进输入框，而 clear() 对 React 受控
                # 输入框不生效，直接把账号追加成两遍导致登录失败，这里统一走
                # 原生 setter + input 事件写入，并回读校验。
                self._fill_login_input(username, self.config.username, '账号')
                self._fill_login_input(password, self.config.password, '密码')
                if not agreement_check.is_selected():
                    agreement_control = WebDriverWait(self.driver, 20).until(
                        EC.element_to_be_clickable((
                            By.CSS_SELECTOR,
                            'label.usso-agreements-checkbox .usso-checkbox'
                        )))
                    agreement_control.click()
                    # 新版账号中心点击协议会弹「服务协议与隐私政策」确认框，
                    # 必须再点一次「同意」，否则协议不会勾选、登录会被拦下。
                    try:
                        for agree_button in self.driver.find_elements(
                                By.CSS_SELECTOR, '.usso-modal-agreement .usso-btn-primary'):
                            if agree_button.is_displayed():
                                agree_button.click()
                                gui_log_queue.put(" 已确认服务协议与隐私政策")
                                break
                    except Exception:
                        pass

                login_btn = WebDriverWait(self.driver, 20).until(
                    EC.element_to_be_clickable((
                        By.CSS_SELECTOR,
                        '[id^="rc-tabs-"][id$="-panel-1"] form button'
                    )))
                login_btn.click()

                gui_log_queue.put(" 如果遇到验证码，请在弹出的浏览器中手动进行人机验证。")
                gui_log_queue.put("【等待】正在智能轮询登录状态...")
                if not self._handle_login_gate(timeout=300):
                    raise TimeoutException("登录未完成：极验滑块未通过或登录接口持续超时")
                self._await_portal_entry(timeout=90)

            self.anti_anti_cheat()

            try:
                zhidaole_button = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.XPATH,
                                                                                                      '/html/body/div[3]/div/div[2]/div/div[2]/div/div/div/div[4]/button')))
                zhidaole_button.click()
            except Exception:
                pass

            try:
                anti_cheat_announce_button = WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((By.XPATH,
                                                    '/html/body/div[4]/div/div/div/div[2]/div/div/div[4]/div[5]')))
                anti_cheat_announce_button.click()
            except Exception:
                pass

            gui_log_queue.put(" 登录验证握手成功！")
            return True

        except Exception as e:
            if self._shutdown_event.is_set():
                return False
            error_msg = str(e)
            gui_log_queue.put(f" 登录执行流阻断: {error_msg[:50]}")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return False

    def _sync_auth_state(self, legacy_token: Optional[str], configured_jwt: Optional[str]) -> Dict[str, Any]:
        """同步旧版存储、新版门户状态和共享 JWT cookie。"""
        cookie = self.driver.get_cookie("jwt") or {}
        cookie_jwt = cookie.get("value") or ""
        return self.driver.execute_script("""
            const configuredLegacy = arguments[0];
            const configuredJwt = arguments[1];
            const cookieJwt = arguments[2] || '';
            let legacy = null;
            const legacyRaw = localStorage.getItem('__token');
            if (legacyRaw) {
                try { legacy = JSON.parse(legacyRaw); }
                catch (_) { localStorage.removeItem('__token'); }
            }

            let portal = {};
            try { portal = JSON.parse(localStorage.getItem('PORTAL_STATE_PERSISTENT') || '{}') || {}; }
            catch (_) {}

            const liveJwt = legacy && legacy.jwt;
            const portalJwt = portal.Authorization || (portal.tokenInfo && portal.tokenInfo.jwt);
            const existingJwt = liveJwt || portalJwt || cookieJwt || '';
            let jwt = existingJwt || configuredJwt || '';
            let source = liveJwt ? 'legacy' : portalJwt ? 'portal' : cookieJwt ? 'cookie' : configuredJwt ? 'configured' : 'none';

            if (!existingJwt && configuredLegacy) {
                try {
                    legacy = JSON.parse(configuredLegacy);
                    jwt = legacy.jwt || jwt;
                    source = 'configured-legacy';
                    localStorage.setItem('__token', JSON.stringify(legacy));
                } catch (_) {}
            }

            if (jwt) {
                if (!portalJwt) {
                    portal.Authorization = jwt;
                    portal.tokenInfo = Object.assign({}, portal.tokenInfo || {}, {jwt});
                    localStorage.setItem('PORTAL_STATE_PERSISTENT', JSON.stringify(portal));
                }
                if (!cookieJwt) {
                    document.cookie = 'jwt=' + jwt + '; domain=.unipus.cn; path=/; Secure; SameSite=None';
                }
            }

            return {
                source,
                hasLegacy: !!(legacy && legacy.jwt),
                hasPortal: !!(portal.Authorization || (portal.tokenInfo && portal.tokenInfo.jwt))
            };
        """, legacy_token, configured_jwt, cookie_jwt)

    @staticmethod
    def _fill_login_input(element, value: str, label: str = '输入框') -> bool:
        """可靠写入登录表单：原生 setter + input/change 事件，最后回读校验。

        账号中心是 React 受控组件，「记住我」会预填旧账号，而 Selenium 的
        clear() 不一定能清掉受控状态（会变成把新值追加到旧值后面），
        所以这里先整体替换 value，再派发事件让框架同步 state。
        """
        script = """
            const el = arguments[0], value = arguments[1];
            el.focus();
            const proto = Object.getPrototypeOf(el);
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) { desc.set.call(el, value); } else { el.value = value; }
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            return el.value;
        """
        try:
            written = element.parent.execute_script(script, element, value)
        except Exception as exc:
            print(f"  {label}写入异常: {str(exc)[:60]}")
            written = None
        if written == value:
            return True

        # 兜底：聚焦后全选删除，再逐字符输入
        try:
            element.click()
            element.send_keys(Keys.CONTROL, 'a')
            element.send_keys(Keys.DELETE)
            element.send_keys(value)
            written = element.get_attribute('value') or ''
        except Exception as exc:
            print(f"  {label}键盘写入异常: {str(exc)[:60]}")
            return False
        if written != value:
            print(f"  {label}写入校验失败: 期望 {value!r}, 实际 {written!r}")
            return False
        return True

    @staticmethod
    def _has_live_auth(driver) -> bool:
        cookie = driver.get_cookie("jwt") or {}
        if cookie.get("value"):
            return True
        try:
            return bool(driver.execute_script("""
                let legacy = null;
                let portal = null;
                try { legacy = JSON.parse(localStorage.getItem('__token') || 'null'); } catch (_) {}
                try { portal = JSON.parse(localStorage.getItem('PORTAL_STATE_PERSISTENT') || 'null'); } catch (_) {}
                return !!((legacy && legacy.jwt) ||
                    (portal && (portal.Authorization || (portal.tokenInfo && portal.tokenInfo.jwt))));
            """))
        except Exception:
            return False

    @staticmethod
    def _home_ready(driver) -> bool:
        try:
            cookie = driver.get_cookie("jwt") or {}
            return bool(driver.execute_script("""
                if (location.hostname !== 'ucloud.unipus.cn') return false;
                let legacy = null;
                let portal = null;
                try { legacy = JSON.parse(localStorage.getItem('__token') || 'null'); } catch (_) {}
                try { portal = JSON.parse(localStorage.getItem('PORTAL_STATE_PERSISTENT') || 'null'); } catch (_) {}
                const loading = document.querySelector('#root-loading');
                const loadingVisible = loading && getComputedStyle(loading).display !== 'none';
                const root = document.querySelector('#root');
                const rendered = !!(root && (root.innerText || '').trim().length > 20);
                const authenticated = !!((legacy && legacy.jwt) ||
                    (portal && (portal.Authorization || (portal.tokenInfo && portal.tokenInfo.jwt))) || arguments[0]);
                return authenticated && rendered && !loadingVisible;
            """, cookie.get("value") or ""))
        except Exception:
            return False

    def anti_anti_cheat(self):
        """优先使用本次 SSO 会话，并兼容旧版完整 token 与新版 JWT。"""
        legacy_token, configured_jwt = _parse_auth_token(self.config.token_full)
        if self.config.token_full and not configured_jwt:
            print(" 配置的 token_full 格式无效，改用本次登录会话")

        try:
            WebDriverWait(self.driver, 45).until(self._has_live_auth)
        except TimeoutException:
            if not configured_jwt:
                raise
            print(" 未检测到实时登录凭据，使用配置中的 token")

        state = self._sync_auth_state(legacy_token, configured_jwt)
        print(f" 登录认证来源: {state.get('source', 'unknown')}")

        # 一次性 ticket 被消费后 SPA 会永远停在「加载中」，统一走首页清理逻辑
        self._await_portal_entry(timeout=60)
        self._sync_auth_state(legacy_token, configured_jwt)



class PopupWatcher:
    """弹窗监控器"""

    def __init__(self, driver):
        self.driver = driver
        self._stop_event = threading.Event()
        # 任务处理期间必须让出 driver，否则两条线程同时发 WebDriver 命令会互相堵塞
        self._paused = threading.Event()

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            if self._paused.is_set():
                self._stop_event.wait(0.5)
                continue
            try:
                self._click_known_buttons()
            except WebDriverException as e:
                if not self._stop_event.is_set():
                    logger.warning(f"弹窗监控已停止，浏览器会话不可用: {str(e)[:120]}")
                break
            except Exception as e:
                if self._stop_event.is_set():
                    break
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")
                logger.warning(f"弹窗监控操作失败: {error_msg[:120]}")
            self._stop_event.wait(0.5)

    def _click_known_buttons(self):
        js = """
        function findBtn(w) {
            const selectors = [
                '.know-box .iKnow',
                '.ant-modal-confirm-btns .ant-btn-primary',
                '.system-info-cloud-ok-button'
            ];
            for (let sel of selectors) {
                const b = w.document.querySelector(sel);
                if (b) return b;
            }
            return null;
        }
        function clickBtn(btn) {
            ['mouseover','mousedown','mouseup','click'].forEach(ev => {
                btn.dispatchEvent(new MouseEvent(ev, {bubbles:true}));
            });
            btn.click();
        }
        let btn = findBtn(window);
        if (btn) { clickBtn(btn); return true; }
        for (let i=0; i<window.frames.length; i++) {
            try {
                btn = findBtn(window.frames[i]);
                if (btn) { clickBtn(btn); return true; }
            } catch(e) {}
        }
        return false;
        """
        self.driver.execute_script(js)

if __name__ == '__main__':
    print('*' * 25 + "Unipus-Helper" + '*' * 25)

    # 先把临时目录换成可写的：受限环境下浏览器启动会因为系统 TEMP 不可写而失败
    ensure_writable_temp_dir()

    skip_check = '--skip-check' in sys.argv

    if not skip_check:
        print("\n 提示：")
        print("   - 首次运行需要检查环境")
        print("   - 语音识别需要 FFmpeg（约130MB，可自动安装）")
        print("   - 如检查通过但无法启动，使用 --skip-check 跳过")

    logger = setup_logging()

    bot = None
    exit_code = 0
    try:
        if not os.path.exists(os.path.join(BASE_DIR, 'config.json')):
            from unipus_helper.native_ui import setup_first_run
            qt_app, configured = setup_first_run()
            if not configured:
                sys.exit(0)
        bot = UCampusBot(os.path.join(BASE_DIR, 'config.json'), skip_check=skip_check)
        bot.start()
    except KeyboardInterrupt:
        print("\n 正在关闭工作台与课程浏览器...")
    except Exception as e:
        error_msg = str(e)
        print(f"\n 程序运行失败: {error_msg[:100]}")
        logger.error(f"程序异常: {error_msg}", exc_info=True)
        exit_code = 1
    finally:
        if bot:
            bot.request_shutdown()
            gui = getattr(bot, 'gui', None)
            if gui and hasattr(gui, 'close'):
                gui.close()
            elif bot.driver:
                try:
                    bot.driver.quit()
                except Exception:
                    pass
    sys.exit(exit_code)
