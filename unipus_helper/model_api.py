"""OpenAI-compatible connection checks and request error classification."""

from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlsplit

from openai import APIConnectionError, OpenAI


MODEL_REQUEST_TIMEOUT = 60.0
MODEL_RETRY_BUDGET = 120.0

_DEEPSEEK_MODELS = {
    "deepseek-flash", "deepseek-v4-pro",
    "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
}


@dataclass(frozen=True)
class ModelCheck:
    ok: bool
    message: str
    config_error: bool = False


def model_settings_error(base_url: str, api_key: str, model: str) -> str:
    if not (base_url and api_key and model):
        return "请填写 API 地址、API Key 和模型名称"
    try:
        endpoint = urlsplit(base_url)
        endpoint.port
        if endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username is not None:
            return "API 地址应为完整的 HTTP 或 HTTPS 地址"
    except ValueError:
        return "API 地址格式无效"
    if endpoint.hostname == "api.deepseek.com":
        if endpoint.path.rstrip("/") == "/anthropic":
            return "请使用 DeepSeek 的 OpenAI 兼容地址 https://api.deepseek.com"
        canonical = model.casefold()
        if canonical in _DEEPSEEK_MODELS and canonical != model:
            return f"DeepSeek 模型名称区分大小写，请填写 {canonical}"
    return ""


def make_model_client(base_url: str, api_key: str, *, timeout: float = MODEL_REQUEST_TIMEOUT) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)


def model_error_status(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    return status if type(status) is int else None


def model_error_retryable(error: Exception) -> bool:
    status = model_error_status(error)
    if status is not None:
        return status in (408, 409, 429) or 500 <= status < 600
    return isinstance(error, (APIConnectionError, TimeoutError, ConnectionError))


def describe_model_error(error: Exception) -> ModelCheck:
    status = model_error_status(error)
    if status in (400, 404, 422):
        return ModelCheck(False, f"请求参数被拒绝 · HTTP {status}，请检查模型名称、接口地址及模型支持的参数", True)
    if status in (401, 403):
        return ModelCheck(False, f"接口鉴权失败 · HTTP {status}，请检查 API Key 或接口权限", True)
    if status == 402:
        return ModelCheck(False, "接口余额不足 · HTTP 402", True)
    if status == 429:
        return ModelCheck(False, "接口限流 · HTTP 429，请稍后重试")
    if status:
        return ModelCheck(False, f"接口响应异常 · HTTP {status}")
    if model_error_retryable(error):
        return ModelCheck(False, "接口连接失败或超时，请检查网络和 API 地址")
    return ModelCheck(False, f"接口请求失败 · {type(error).__name__}")


def probe_model(base_url: str, api_key: str, model: str) -> ModelCheck:
    base_url, api_key, model = (str(value or "").strip() for value in (base_url, api_key, model))
    error = model_settings_error(base_url, api_key, model)
    if error:
        return ModelCheck(False, error, True)
    probe_options = {}
    if urlsplit(base_url).hostname == "api.deepseek.com":
        probe_options["extra_body"] = {"thinking": {"type": "disabled"}}
    started = monotonic()
    try:
        with make_model_client(base_url, api_key, timeout=15.0) as client:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with OK."}],
                max_tokens=128,
                **probe_options,
            )
    except Exception as exc:
        return describe_model_error(exc)
    choices = getattr(response, "choices", None)
    if isinstance(response, str):
        return ModelCheck(False, "接口返回了文本页面，请填写 OpenAI 兼容 API 地址", True)
    message = getattr(choices[0], "message", None) if isinstance(choices, (list, tuple)) and choices else None
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        return ModelCheck(False, "接口未返回可用正文，请检查地址、模型及网关配置")
    return ModelCheck(True, f"接口正常 · {monotonic() - started:.1f} s")
