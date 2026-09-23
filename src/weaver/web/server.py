"""``weaver serve``: a local web interface over the same evidence as the CLI.

Security model: the server binds to 127.0.0.1 by default, rejects requests
whose Host header is not the loopback address it serves (DNS-rebinding
defence), and requires a per-process random token on every API call.  The
token is embedded in the page it serves; other origins cannot read it.
Long operations run as background jobs with streamed logs; only one
modifying job runs at a time.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import time
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from weaver.config import Project, load_project
from weaver.errors import ConfigError, WeaverError
from weaver.web import api

STATIC = Path(__file__).resolve().parent / "static"


class Job:
    def __init__(self, kind: str, title: str):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.title = title
        self.state = "running"
        self.log: list[str] = []
        self.result: Any = None
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None

    def write(self, msg: str) -> None:
        for line in str(msg).splitlines() or [""]:
            self.log.append(line)

    def to_json(self, since: int = 0) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "log": self.log[since:],
            "log_size": len(self.log),
            "result": self.result,
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
        }


class App:
    def __init__(self, project_path: str | None = None):
        self.token = secrets.token_urlsafe(24)
        self.project: Project | None = None
        self.jobs: dict[str, Job] = {}
        self.busy = threading.Lock()
        self.cache = api.Cache()
        self.recent: list[str] = []
        if project_path:
            self.open(project_path)

    # -- project ----------------------------------------------------------------
    def open(self, path: str) -> Project:
        self.project = load_project(path)
        self.cache = api.Cache()
        root = str(self.project.root)
        self.recent = [root] + [r for r in self.recent if r != root][:7]
        return self.project

    def exclusive(self, fn: Callable[[], Any]) -> Any:
        """Run a short modifying operation, refusing while a modifying job runs."""
        if not self.busy.acquire(blocking=False):
            raise WeaverError("another operation is running; wait for it to finish")
        try:
            return fn()
        finally:
            self.busy.release()

    def need_project(self) -> Project:
        if self.project is None:
            raise WeaverError("no project is open")
        # Pick up edits to weaver.yaml between requests.
        self.project = load_project(self.project.config_path)
        return self.project

    # -- jobs ----------------------------------------------------------------------
    def start(self, kind: str, title: str, fn: Callable[[Job], Any], exclusive: bool = True) -> Job:
        job = Job(kind, title)
        if exclusive and not self.busy.acquire(blocking=False):
            raise WeaverError("another operation is running; wait for it to finish")
        self.jobs[job.id] = job

        def runner() -> None:
            try:
                job.result = fn(job)
                job.state = "done"
            except Exception as e:  # noqa: BLE001 - reported to the UI
                job.state = "failed"
                job.error = f"{type(e).__name__}: {e}"
                if not isinstance(e, WeaverError):
                    job.write(traceback.format_exc())
            finally:
                job.finished = time.time()
                if exclusive:
                    self.busy.release()

        threading.Thread(target=runner, daemon=True).start()
        return job


def _json(obj: Any) -> bytes:
    return json.dumps(obj, default=str).encode()


def make_handler(app: App, host: str) -> type[BaseHTTPRequestHandler]:
    names = {"127.0.0.1", "localhost", "[::1]", f"[{host}]" if ":" in host else host}

    class Handler(BaseHTTPRequestHandler):
        server_version = "weaver"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            pass

        # -- plumbing -----------------------------------------------------------
        def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                    "connect-src 'self'; frame-ancestors 'none'",
                )
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, msg: str, **extra: Any) -> None:
            self._send(status, _json({"error": msg, **extra}))

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 5_000_000:
                raise WeaverError("request too large")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")

        def _guard(self) -> bool:
            port = self.server.server_address[1]
            if self.headers.get("Host") not in {f"{n}:{port}" for n in names}:
                self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
                return False
            if self.path.startswith("/api/") and not secrets.compare_digest(
                self.headers.get("X-Weaver-Token", ""), app.token
            ):
                self._error(HTTPStatus.FORBIDDEN, "missing or invalid token")
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON required")
                return
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if method == "GET" and not u.path.startswith("/api/"):
                    return self._static(u.path)
                body = self._body() if method == "POST" else {}
                out = route(app, method, u.path, q, body)
                self._send(HTTPStatus.OK, _json(out))
            except FileNotFoundError as e:
                self._error(HTTPStatus.NOT_FOUND, str(e))
            except ConfigError as e:
                self._error(HTTPStatus.CONFLICT, str(e), needs_setup="no weaver.yaml" in str(e))
            except (WeaverError, KeyError, ValueError, FileExistsError) as e:
                self._error(HTTPStatus.BAD_REQUEST, str(e))
            except Exception as e:  # noqa: BLE001
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(e).__name__}: {e}")

        def _static(self, path: str) -> None:
            if path in ("/", "/index.html"):
                html = (STATIC / "index.html").read_text().replace("__WEAVER_TOKEN__", app.token)
                return self._send(HTTPStatus.OK, html.encode(), "text/html; charset=utf-8")
            name = path.removeprefix("/static/")
            p = (STATIC / name).resolve()
            if not str(p).startswith(str(STATIC) + "/") or not p.is_file():
                return self._error(HTTPStatus.NOT_FOUND, "not found")
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(HTTPStatus.OK, p.read_bytes(), ctype)

    return Handler


def route(app: App, method: str, path: str, q: dict[str, str], body: dict[str, Any]) -> Any:
    from weaver.ledger import Ledger

    parts = [p for p in path.split("/") if p][1:]  # drop "api"
    head = parts[0] if parts else ""

    if (method, head) == ("GET", "state"):
        if app.project is None:
            return {
                "project": None,
                "recent": app.recent,
                "running": [j.to_json() for j in app.jobs.values() if j.state == "running"],
            }
        st = api.project_state(app.need_project())
        st["recent"] = app.recent
        st["running"] = [j.to_json() for j in app.jobs.values() if j.state == "running"]
        return st
    if (method, head) == ("POST", "open"):
        app.open(body["path"])
        return {"ok": True}
    if (method, head) == ("POST", "setup"):
        cfg = api.setup_project(
            body["path"],
            body["build"],
            body.get("compiler") or "cc",
            body.get("clean"),
            body.get("secondary"),
            body.get("concurrency"),
            body.get("name"),
            body.get("run"),
        )
        app.open(str(cfg))
        return {"ok": True, "config": str(cfg)}
    if head == "jobs" and method == "GET":
        if len(parts) == 1:
            return [j.to_json(len(j.log)) for j in sorted(app.jobs.values(), key=lambda j: -j.started)][:20]
        job = app.jobs.get(parts[1])
        if job is None:
            raise FileNotFoundError(f"no job {parts[1]}")
        return job.to_json(int(q.get("since", "0")))

    proj = app.need_project()
    if (method, head) == ("POST", "compile"):
        from weaver.pipeline import refresh

        capture = bool(body.get("capture", any(p.capture for p in proj.profiles)))

        def work(job: Job) -> Any:
            return refresh(proj, log=job.write, capture=capture, flow=body.get("flow"))

        return app.start("compile", "Compile and analyse", work).to_json()
    if (method, head) == ("POST", "flow"):
        from weaver.flow.svf import run_flow

        def work_flow(job: Job) -> Any:
            out = {}
            for p in proj.profiles:
                r = run_flow(proj, p, force=bool(body.get("force")))
                job.write(f"[{p.id}] flow {r['status']}" + (f": {r.get('reason')}" if r.get("reason") else ""))
                out[p.id] = r["status"]
            return out

        return app.start("flow", "Points-to analysis (SVF)", work_flow).to_json()
    if (method, head) == ("GET", "pointers"):
        return api.pointer_list(proj, app.cache)
    if (method, head) == ("GET", "pointer"):
        return api.pointer_detail(proj, parts[1], app.cache)
    if (method, head) == ("GET", "neighborhood"):
        return api.neighborhood(proj, parts[1], app.cache)
    if (method, head) == ("GET", "map"):
        return api.map_model(proj, app.cache)
    if (method, head) == ("GET", "source"):
        return api.source_view(proj, q["file"], app.cache)
    if (method, head) == ("GET", "ledger"):
        from weaver.card import render_card

        txns = Ledger(proj).all()
        if len(parts) > 1:
            t = Ledger(proj).load(parts[1])
            return {**t, "card": render_card(t)}
        return [{k: t.get(k) for k in ("id", "state", "recipe", "finding_id", "finding", "created_at")} for t in txns]
    if (method, head) == ("POST", "propose"):
        from weaver.card import render_card

        t = app.exclusive(lambda: Ledger(proj).propose(body["finding"], body.get("recipe")))
        return {**t, "card": render_card(t)}
    if (method, head) == ("POST", "txn"):
        tid, action = parts[1], parts[2]
        led = Ledger(proj)
        if action == "validate":

            def work_val(job: Job) -> Any:
                job.write(f"validating {tid} in isolated workspaces ...")
                t = led.validate(tid)
                for r in (t.get("validation") or {}).get("records", []):
                    job.write(f"{r['outcome']:>13}  {r['kind']:<22} {r['name']}: {r['detail'][:200]}")
                job.write(f"state: {t['state']}")
                return {"state": t["state"]}

            return app.start("validate", f"Validate {tid}", work_val).to_json()
        if action in ("accept", "revert"):
            from weaver.pipeline import refresh

            def work_apply(job: Job) -> Any:
                t = led.accept(tid) if action == "accept" else led.revert(tid)
                job.write(f"{tid} {t['state']}; re-analysing ...")
                refresh(proj, log=job.write)
                return {"state": t["state"]}

            return app.start(action, f"{action.title()} {tid}", work_apply).to_json()
        if action == "skip":
            t = app.exclusive(lambda: led.skip(tid, body.get("reason") or "skipped in the web interface"))
            return {"state": t["state"]}
        raise KeyError(f"unknown action {action}")
    if (method, head) == ("GET", "snapshots"):
        from weaver.impact import list_snapshots

        return list_snapshots(proj)
    if (method, head) == ("POST", "snapshots"):
        from weaver.impact import save_snapshot, snapshot_from_git

        if body.get("git"):

            def work_git(job: Job) -> Any:
                return snapshot_from_git(proj, body["git"], body.get("name") or None, log=job.write)

            return app.start("snapshot", f"Snapshot of {body['git']}", work_git).to_json()
        return app.exclusive(lambda: save_snapshot(proj, body.get("name") or None))
    if (method, head) == ("POST", "impact"):
        from weaver.impact import impact

        def work_imp(job: Job) -> Any:
            job.write(f"comparing with snapshot {body['since']} ...")
            return impact(proj, body["since"], revalidate=bool(body.get("revalidate")), log=job.write)

        return app.start("impact", f"Change impact since {body['since']}", work_imp, exclusive=False).to_json()
    if (method, head) == ("POST", "contracts"):
        from weaver.impact import pin_contract

        app.cache = api.Cache()
        return app.exclusive(
            lambda: pin_contract(proj, body["finding"], list(body["expect"]), body.get("reason") or "")
        )
    if (method, head) == ("POST", "explain"):
        from weaver.llm.client import explain

        dry = bool(body.get("dry_run"))

        def work_llm(job: Job) -> Any:
            text = explain(proj, body["finding"], dry_run=dry)
            job.write(text)
            return {"text": text}

        return app.start("explain", "Explain candidate", work_llm, exclusive=False).to_json()
    raise FileNotFoundError(f"no route {method} {path}")


def make_server(app: App, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(app, host))
    httpd.daemon_threads = True
    return httpd


def serve(project_path: str | None, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    app = App(project_path)
    httpd = make_server(app, host, port)
    url = f"http://{'localhost' if host in ('127.0.0.1', '::1') else host}:{httpd.server_address[1]}/"
    print(f"Weaver web interface on {url}  (Ctrl-C to stop)", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "WARNING: listening beyond loopback; anyone who can reach this port and load the page can drive "
            "Weaver on this machine.",
            flush=True,
        )
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
