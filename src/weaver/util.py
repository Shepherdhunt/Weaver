"""Small shared helpers: hashing, atomic writes, subprocess capture."""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def short_hash(*parts: Any, length: int = 12) -> str:
    """Deterministic short identifier derived from JSON-serialisable parts."""
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:length]


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes, mode: int | None = None) -> None:
    """Write ``data`` to ``path`` via a temporary file and rename.

    The replacement never modifies the existing inode, so hard links and
    concurrent readers of the old file are unaffected.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None and path.exists():
        mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    atomic_write_bytes(path, text.encode())


def write_json(path: str | os.PathLike[str], obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=False, default=_json_default) + "\n")


def read_json(path: str | os.PathLike[str]) -> Any:
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as f:
            return json.load(f)
    with open(path) as f:
        return json.load(f)


def append_jsonl(path: str | os.PathLike[str], obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, sort_keys=False, default=_json_default) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: str | os.PathLike[str]) -> list[Any]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _json_default(o: Any) -> Any:
    if isinstance(o, Path):
        return str(o)
    if hasattr(o, "to_json"):
        return o.to_json()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


@dataclass
class RunResult:
    argv: list[str]
    cwd: str
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def stderr_text(self, limit: int | None = 20000) -> str:
        text = self.stderr.decode(errors="replace")
        return text if limit is None or len(text) <= limit else text[:limit] + "\n...[truncated]"

    def stdout_text(self, limit: int | None = 20000) -> str:
        text = self.stdout.decode(errors="replace")
        return text if limit is None or len(text) <= limit else text[:limit] + "\n...[truncated]"

    def record(self) -> dict[str, Any]:
        return {
            "argv": self.argv,
            "cwd": self.cwd,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "stderr": self.stderr_text(4000),
        }


def run(
    argv: Sequence[str],
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    stdout_path: str | os.PathLike[str] | None = None,
    input: bytes | None = None,
) -> RunResult:
    """Run a command, capturing output; never raises for a non-zero exit."""
    cwd_s = str(cwd) if cwd is not None else os.getcwd()
    full_env = None
    if env is not None:
        full_env = dict(os.environ)
        full_env.update(env)
    try:
        if stdout_path is not None:
            with open(stdout_path, "wb") as out:
                p = subprocess.run(
                    list(argv),
                    cwd=cwd_s,
                    env=full_env,
                    stdout=out,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    input=input,
                )
            return RunResult(list(argv), cwd_s, p.returncode, b"", p.stderr)
        p = subprocess.run(list(argv), cwd=cwd_s, env=full_env, capture_output=True, timeout=timeout, input=input)
        return RunResult(list(argv), cwd_s, p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired as e:
        return RunResult(list(argv), cwd_s, -1, e.stdout or b"", e.stderr or b"", timed_out=True)
    except FileNotFoundError as e:
        return RunResult(list(argv), cwd_s, 127, b"", str(e).encode())
    except PermissionError as e:
        return RunResult(list(argv), cwd_s, 126, b"", str(e).encode())


def is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def rel_or_abs(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> str:
    """Path relative to ``root`` when inside it, otherwise absolute."""
    p = Path(path).resolve()
    try:
        return p.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def uniq(items: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out
