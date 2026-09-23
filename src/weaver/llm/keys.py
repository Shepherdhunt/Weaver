"""Bring-your-own-key storage for AI explanations.

Keys never go into ``weaver.yaml`` (which is usually committed) or the project's
state directory (which travels with the project).  A key comes from the
environment variable the configuration names, or from a per-user file readable
only by its owner (``~/.config/weaver/keys.json``, mode 0600), keyed by provider
or endpoint.  The web interface and ``weaver ai key`` can write that file; nothing
ever reads a key back out to a page or a log.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from weaver.config import AIConfig


def keys_path() -> Path:
    base = os.environ.get("WEAVER_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "weaver"
    )
    return Path(base) / "keys.json"


def _load() -> dict[str, str]:
    try:
        data = json.loads(keys_path().read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save(data: dict[str, str]) -> None:
    p = keys_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def store_key(key_id: str, key: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("empty key")
    data = _load()
    data[key_id] = key
    _save(data)


def forget_key(key_id: str) -> bool:
    data = _load()
    if key_id not in data:
        return False
    del data[key_id]
    _save(data)
    return True


def local_endpoint(ai: AIConfig) -> bool:
    """A model server on this machine (Ollama, vLLM, LM Studio...), which usually needs no key."""
    host = urlparse(ai.effective_base_url or "").hostname or ""
    return ai.provider != "anthropic" and host in ("localhost", "127.0.0.1", "::1")


def resolve_key(ai: AIConfig) -> tuple[str | None, str]:
    """(key, where it came from).  The environment wins over the stored key."""
    if os.environ.get(ai.key_env):
        return os.environ[ai.key_env], f"environment variable {ai.key_env}"
    stored = _load().get(ai.key_id)
    if stored:
        return stored, f"stored on this machine ({keys_path()})"
    if local_endpoint(ai):
        return None, "not needed (local model server)"
    return None, "missing"


def key_status(ai: AIConfig) -> str:
    return resolve_key(ai)[1]
