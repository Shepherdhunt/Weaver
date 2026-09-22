"""Compiler/linker wrapper used during build capture.

Usage (normally through a generated shim)::

    python -m weaver.capture.wrapper --real /path/to/cc --log capture.jsonl -- ARGS...

The wrapper calls the original tool with the original arguments, stdin, stdout
and stderr, records the invocation, and exits with the tool's own status
(re-raising the tool's terminating signal where applicable).  It never changes
the build: analysis outputs are produced later, in a separate directory.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time

from weaver.capture.compdb import expand_response_files
from weaver.toolchain.options import relevant_env
from weaver.util import sha256_bytes


def _record(log: str, rec: dict) -> None:
    line = (json.dumps(rec) + "\n").encode()
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    real = log = role = None
    while argv and argv[0] != "--":
        opt = argv.pop(0)
        if opt == "--real":
            real = argv.pop(0)
        elif opt == "--log":
            log = argv.pop(0)
        elif opt == "--role":
            role = argv.pop(0)
        else:
            print(f"weaver wrapper: unknown option {opt}", file=sys.stderr)
            return 2
    if argv and argv[0] == "--":
        argv.pop(0)
    if not real or not log:
        print("weaver wrapper: --real and --log are required", file=sys.stderr)
        return 2

    cmd = [real] + argv
    cwd = os.getcwd()
    started = time.time()
    try:
        proc = subprocess.run(cmd)
        rc = proc.returncode
    except OSError as e:
        print(f"weaver wrapper: cannot execute {real}: {e}", file=sys.stderr)
        rc = 127

    try:
        expanded, rfs = expand_response_files(cmd, cwd)
        st = os.stat(real)
        _record(
            log,
            {
                "time": started,
                "duration": time.time() - started,
                "role": role,
                "tool": real,
                "tool_realpath": os.path.realpath(real),
                "tool_size": st.st_size,
                "tool_mtime": st.st_mtime,
                "cwd": cwd,
                "argv": cmd,
                "expanded": expanded,
                "response_files": [{"path": r.path, "sha256": r.sha256, "tokens": r.tokens} for r in rfs],
                "env": relevant_env(dict(os.environ)),
                "returncode": rc,
                "argv_sha256": sha256_bytes(json.dumps(cmd).encode()),
            },
        )
    except Exception as e:  # recording must never break the build
        print(f"weaver wrapper: warning: could not record invocation: {e}", file=sys.stderr)

    if rc < 0:
        # Terminated by a signal: terminate ourselves the same way.
        signal.signal(-rc, signal.SIG_DFL)
        os.kill(os.getpid(), -rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
