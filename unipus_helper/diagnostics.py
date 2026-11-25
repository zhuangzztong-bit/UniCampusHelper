"""Redact configured credentials and authorization data from diagnostics."""

import logging
import re
from threading import Lock
from urllib.parse import quote, quote_plus

from .config_store import read_config


_SECRET_FIELDS = ("username", "password", "api_key", "token_full", "voice_clone_api_key")
_SECRET_PATTERN = (
    r"(?:password|api[-_]?key|voice_clone_api_key|__token|token(?:_full)?|"
    r"access_?token|refresh_?token|authorization|cookie|set-cookie|ticket)"
)
_QUERY = re.compile(r'(?i)(?P<label>[?&](?:password|api[-_]?key|token|access_token|ticket|code|'
                    r'signature|x-amz-signature|x-amz-credential)=)[^&#\s"\']+')
_HEADER = re.compile(r"(?im)(?P<label>^[ \t]*(?:authorization|proxy-authorization|cookie|set-cookie)[ \t]*:[ \t]*)[^\r\n]+")
_NAMED = re.compile(rf'''(?i)(?P<label>(?<!\w)["']?{_SECRET_PATTERN}["']?[ \t]*[:=][ \t]*)'''
                    r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&}\]]+)''')
_AUTH = re.compile(r"(?i)(?P<label>\b(?:bearer|basic)\s+)[a-z0-9._~+/=-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_MASK = "••••"


class LogRedactor:
    def __init__(self):
        self._lock = Lock()
        self._values = ()

    def add_config(self, config):
        if config is None:
            return
        getter = config.get if isinstance(config, dict) else lambda name: getattr(config, name, "")
        values = set()
        for field in _SECRET_FIELDS:
            value = str(getter(field) or "").strip()
            if value:
                values.update((value, quote(value, safe="", errors="replace"), quote_plus(value, errors="replace")))
        if values:
            with self._lock:
                self._values = tuple(sorted(values.union(self._values), key=len, reverse=True))

    def add_config_file(self, path):
        try:
            values = read_config(path)
        except (OSError, ValueError):
            return
        self.add_config(values)

    def __call__(self, message):
        message = str(message)
        # Mask authorization schemes before redacting unquoted name/value pairs.
        for pattern in (_HEADER, _QUERY, _AUTH, _NAMED):
            message = pattern.sub(r"\g<label>" + _MASK, message)
        message = _JWT.sub(_MASK, message)
        with self._lock:
            values = self._values
        for value in values:
            if len(value) < 4:
                message = re.sub(rf"(?<!\w){re.escape(value)}(?!\w)", _MASK, message)
            else:
                message = message.replace(value, _MASK)
        return message


redact_log = LogRedactor()


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact_log(super().format(record))
