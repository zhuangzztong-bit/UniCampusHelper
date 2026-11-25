"""Read JSON configuration and replace it only after a complete write."""

import json
import os
import tempfile
from pathlib import Path


def read_config(path):
    path = Path(path)
    try:
        with path.open(encoding="utf-8-sig") as stream:
            values = json.load(stream)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} 第 {exc.lineno} 行、第 {exc.colno} 列的 JSON 格式无效") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name} 需要使用 UTF-8 编码") from exc
    if not isinstance(values, dict):
        raise ValueError(f"{path.name} 的根节点需要是 JSON 对象")
    return values


def write_config(path, values):
    path = Path(path)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".config-", suffix=".tmp", delete=False,
        ) as stream:
            pending = Path(stream.name)
            json.dump(values, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        if pending is not None:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass
