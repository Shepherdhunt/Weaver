"""The local web interface: request guards, view models, and the workflow driven through jobs."""

from __future__ import annotations

import http.client
import json
import shutil
import threading
import time

import pytest
import yaml
from conftest import FIXTURE, SINGLE_THREADED, build_project, needs_clang, run_cli, validation_for
from test_pipeline import finding_id

from weaver.web.server import App, make_server


class Client:
    def __init__(self, app: App):
        self.app = app
        self.httpd = make_server(app, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def raw(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        conn.close()
        return out

    def api(self, path, body=None, status=200):
        st, _, data = self.raw(
            "POST" if body is not None else "GET", "/api/" + path, body, {"X-Weaver-Token": self.app.token}
        )
        assert st == status, (st, data[:500])
        return json.loads(data)

    def wait(self, job, timeout=300):
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = self.api(f"jobs/{job['id']}")
            if j["state"] != "running":
                assert j["state"] == "done", (j["error"], j["log"][-20:])
                return j
            time.sleep(0.2)
        raise TimeoutError(job)


@pytest.fixture(scope="module")
def analysed(tmp_path_factory):
    root = build_project(
        tmp_path_factory.mktemp("web"),
        [{"id": "clang", "cc": "clang", "validation": validation_for("clang")}],
        extra={
            "preservation": SINGLE_THREADED,
            "acceptance": {"require": ["compile", "mechanical-recheck", "differential-testing"]},
        },
    )
    assert run_cli(root, "collect") == 0 and run_cli(root, "inventory") == 0
    return root


@pytest.fixture()
def client(analysed):
    c = Client(App(str(analysed)))
    yield c
    c.close()


@needs_clang
def test_request_guards(client):
    key = client.app.token
    st, _, body = client.raw("GET", "/")
    assert st == 401 and b"weaver serve" in body and key.encode() not in body  # the page never holds the key
    assert client.raw("GET", "/?token=wrong")[0] == 401
    # the printed link exchanges the key for an HttpOnly, SameSite=Strict cookie and drops it from the URL
    st, headers, _ = client.raw("GET", "/?token=" + key)
    assert st == 303 and headers["Location"] == "/"
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    cookie = headers["Set-Cookie"].split(";")[0]
    assert cookie.startswith(f"weaver-{client.port}=")
    st, headers, body = client.raw("GET", "/", headers={"Cookie": cookie})
    assert st == 200 and "default-src 'self'" in headers["Content-Security-Policy"] and key.encode() not in body
    browser = {"Cookie": cookie, "X-Weaver-Request": "1"}
    assert client.raw("GET", "/api/state", headers=browser)[0] == 200
    assert client.raw("GET", "/api/state")[0] == 401  # no key
    assert client.raw("GET", "/api/state", headers={"X-Weaver-Token": "nope"})[0] == 401
    assert client.raw("GET", "/api/state", headers={"Cookie": cookie})[0] == 401  # no custom header: not this page
    other = {"Cookie": f"weaver-{client.port + 1}={key}", "X-Weaver-Request": "1"}
    assert client.raw("GET", "/api/state", headers=other)[0] == 401  # another server's cookie name
    # DNS rebinding: a foreign Host header is refused even with the key
    assert client.raw("GET", "/api/state", headers={**browser, "Host": "evil.test"})[0] == 403
    assert client.raw("GET", "/api/state", headers={"X-Weaver-Token": key, "Host": "evil.test"})[0] == 403
    assert client.raw("GET", "/?token=" + key, headers={"Host": f"evil.test:{client.port}"})[0] == 403
    # simple (form) POSTs are refused
    st, _, _ = client.raw("POST", "/api/compile", b"{}", {**browser, "Content-Type": "text/plain"})
    assert st == 415
    assert client.raw("GET", "/static/../server.py")[0] == 404
    client.api("source?file=../../../../etc/passwd", status=404)


def test_port_selection():
    """By default the first free port from 61847 on; an explicit port that is taken is an error."""
    from weaver.errors import WeaverError

    app = App()
    first = make_server(app)
    try:
        taken = first.server_address[1]
        assert first.server_address[0] == "127.0.0.1"
        second = make_server(app)  # 'taken' is in use now: the next free port
        try:
            assert second.server_address[1] != taken
        finally:
            second.server_close()
        with pytest.raises(WeaverError, match="in use"):
            make_server(app, port=taken)
    finally:
        first.server_close()


@needs_clang
def test_views(client, analysed):
    st = client.api("state")
    assert st["project"]["name"] == "demo" and st["inventory"]
    pl = client.api("pointers")
    assert len(pl["pointers"]) == 45 and pl["removed"] == []
    by = {(p["function"], p["name"]): p for p in pl["pointers"]}
    assert by[("la_basic", "p")]["class"] == "writes" and by[("la_basic", "p")]["recipes"]["local-alias"]["eligible"]
    assert by[("p_scale", "factor")]["class"] == "read-only"
    assert by[("la_escape", "ep")]["class"] == "escapes"

    d = client.api("pointer/" + by[("p_scale", "factor")]["id"])
    assert d["recipes"]["scalar-input"]["eligible"]
    assert [(a["caller"], a["object"]) for a in d["call_args"]] == [("main", "k")]
    assert d["uses"][0]["text"] == "deref (read)" and "*factor" in d["uses"][0]["source"]

    m = client.api("map")
    params = next(f for f in m["files"] if f["file"] == "src/params.c")
    scale = next(fn for fn in params["functions"] if fn["name"] == "p_scale")
    assert scale["callers"] == ["src/main.c::main"] and scale["pointers"][0]["eligible"]

    nb = client.api("neighborhood/" + by[("p_sum2", "a")]["id"])
    assert any(n["type"] == "object" and n["label"] == "la" for n in nb["nodes"])
    assert any(e["type"] == "calls" for e in nb["edges"])

    src = client.api("source?file=src/alias.c")
    assert any(mk["name"] == "p" and mk["role"] == "declaration" for mk in src["marks"])
    assert src["unexamined"]  # the '#ifdef NEVER_DEFINED' branch


@needs_clang
def test_transaction_workflow_through_jobs(tmp_path):
    root = build_project(
        tmp_path,
        [{"id": "clang", "cc": "clang", "validation": validation_for("clang")}],
        extra={"acceptance": {"require": ["compile", "mechanical-recheck", "differential-testing"]}},
    )
    run_cli(root, "collect")
    run_cli(root, "inventory")
    c = Client(App(str(root)))
    try:
        fid = finding_id(root, "la_basic", "p")
        t = c.api("propose", {"finding": fid, "recipe": "local-alias"})
        assert t["state"] == "proposed" and "+    total += 2;" in t["patch"]["diff"] and t["card"]
        # a second modifying operation is refused while one runs
        c.app.busy.acquire()
        try:
            c.api("compile", {}, status=400)
        finally:
            c.app.busy.release()
        c.wait(c.api(f"txn/{t['id']}/validate", {}))
        assert c.api(f"ledger/{t['id']}")["state"] == "validated"
        c.wait(c.api(f"txn/{t['id']}/accept", {}))
        pl = c.api("pointers")
        assert fid not in {p["id"] for p in pl["pointers"]}
        assert [(r["txn"], r["name"]) for r in pl["removed"]] == [(t["id"], "p")]
        assert "unsigned *p" not in (root / "src/alias.c").read_text()

        # pin, snapshot, edit, compare
        a = finding_id(root, "la_global", "gp")
        c.api("contracts", {"finding": a, "expect": ["no-escape"], "reason": "test"})
        c.api("snapshots", {"name": "base"})
        alias = root / "src/alias.c"
        alias.write_text(alias.read_text().replace("    (*gp)++;\n", "    (*gp)++;\n    util_touch(gp);\n", 1))
        assert c.api("state")["stale_files"] == ["src/alias.c"]
        c.wait(c.api("compile", {"flow": False}))
        rep = c.wait(c.api("impact", {"since": "base"}))["result"]
        assert rep["risk"] == "high"
        assert any(x["status"] == "violated" for x in rep["contracts"])
    finally:
        c.close()


@needs_clang
def test_setup_and_capture_from_the_browser(tmp_path):
    root = tmp_path / "fresh"
    shutil.copytree(FIXTURE, root)
    c = Client(App())
    try:
        assert c.api("state")["project"] is None
        e = c.api("open", {"path": str(root)}, status=409)
        assert e["needs_setup"]
        c.api(
            "setup",
            {
                "path": str(root),
                "build": "make -B CC={cc} BUILD=build/w",
                "compiler": "clang",
                "clean": "make clean BUILD=build/w",
                "run": "./build/w/demo",
                "concurrency": "single-threaded",
            },
        )
        cfg = yaml.safe_load((root / "weaver.yaml").read_text())
        prof = cfg["profiles"][0]
        assert prof["capture"]["command"] == "make -B CC={cc} BUILD=build/w"
        assert prof["validation"]["build"]["run"] == "make -B CC=clang BUILD=build/w"
        assert "differential-testing" in cfg["acceptance"]["require"]
        job = c.wait(c.api("compile", {"flow": False}))
        assert any("captured 5 compile command(s)" in line for line in job["log"])
        st = c.api("state")
        assert st["inventory"] and st["project"]["profiles"][0]["compile_commands_exist"]
        assert len(c.api("pointers")["pointers"]) == 45
    finally:
        c.close()


def test_test_detection_setup_and_settings_editor(tmp_path):
    root = tmp_path / "fresh"
    shutil.copytree(FIXTURE, root)
    with (root / "Makefile").open("a") as mk:
        mk.write("\ncheck: $(BUILD)/demo\n\t./$(BUILD)/demo > /dev/null\n")
    c = Client(App())
    try:
        found = c.api(f"detect-tests?path={root}&build=make+-B+CC%3D%7Bcc%7D&cc=clang")
        assert [t["run"] for t in found] == ["make check"]
        c.api("detect-tests?path=/nonexistent/dir", status=404)
        c.api(
            "setup",
            {
                "path": str(root),
                "build": "make -B CC={cc} BUILD=build/w",
                "compiler": "clang",
                "tests": ["make check BUILD=build/w"],
            },
        )
        cfg = yaml.safe_load((root / "weaver.yaml").read_text())
        val = cfg["profiles"][0]["validation"]
        assert val["tests"] == [{"name": "make-check", "run": "make check BUILD=build/w", "cwd": "{workspace}"}]
        assert val["build"]["run"] == "make -B CC=clang BUILD=build/w"
        assert "testing" in cfg["acceptance"]["require"]
        assert c.api("state")["validation"]["level"] == "behavioural"

        st = c.api("settings")
        prof = st["profiles"][0]
        assert prof["tests"][0]["name"] == "make-check" and st["strength"]["level"] == "behavioural"
        # drop the tests: validation becomes compile-only and the state says so
        res = c.api("settings", {"profiles": [{"id": prof["id"], "tests": []}]})
        assert res["settings"]["strength"]["level"] == "compile-only"  # 'testing' is required but nothing runs
        assert any("nothing runs the changed program" in w for w in res["warnings"])
        res = c.api(
            "settings",
            {
                "acceptance": {"require": ["compile", "mechanical-recheck"]},
                "profiles": [{"id": prof["id"], "tests": []}],
            },
        )
        assert c.api("state")["validation"]["level"] == "compile-only"
        assert (root / "weaver.yaml.bak").exists()
        e = c.api("settings", {"acceptance": {"require": ["testing"]}}, status=409)  # 'compile' is mandatory
        assert "compile" in e["error"]
    finally:
        c.close()


@needs_clang
def test_read_only_snapshot_export(analysed, tmp_path):
    """'weaver export-ui' records the views, scoped or not, into one page that needs no server."""
    from weaver.config import load_project
    from weaver.web.export import render_page, snapshot_dataset

    ds = snapshot_dataset(load_project(analysed), label="Demo")
    r = ds["responses"]
    assert ds["id"] == "demo" and r["state"]["project"]["root"] == "demo"  # no path of the exporting machine
    assert str(analysed) not in json.dumps(ds)
    ids = [p["id"] for p in r["pointers"]["pointers"]]
    assert len(ids) == 45 and all(f"pointer/{i}" in r and f"neighborhood/{i}" in r for i in ids)
    assert "source?file=src/alias.c" in r and "settings" in r and r["ledger"] == []
    assert ds["select"] in ids and r["pointer/" + ds["select"]]["recipes"]  # opens on a pointer with a verdict

    scoped = snapshot_dataset(load_project(analysed), scope=["src/params.c"])
    assert {p["file"] for p in scoped["responses"]["pointers"]["pointers"]} == {"src/params.c"}
    assert [f["file"] for f in scoped["responses"]["map"]["files"]] == ["src/params.c"]

    page = render_page([ds, scoped], "Weaver Playtest", fragment=True, repo="https://github.com/o/r.git", branch="b")
    assert page.startswith("<title>Weaver Playtest</title>") and "<html" not in page and "/static/" not in page
    data = page.split("window.WEAVER_SNAPSHOT = ", 1)[1].split(";</script>", 1)[0]
    assert "<" not in data  # the embedded source cannot close the script element
    snap = json.loads(data)
    assert snap["web"] == "https://github.com/o/r/tree/b" and len(snap["datasets"]) == 2
    assert "#include <stdio.h>" in "\n".join(snap["datasets"][0]["responses"]["source?file=src/main.c"]["lines"])
    assert render_page([ds]).startswith("<!doctype html>")


@needs_clang
def test_ai_settings_and_key_through_the_browser(client, tmp_path, monkeypatch):
    """AI explanations are off until switched on; a stored key is kept per user and never sent back."""
    monkeypatch.setenv("WEAVER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert client.api("state")["ai"]["enabled"] is False
    assert client.api("settings")["ai"]["enabled"] is False
    local = {"provider": "openai-compatible", "base_url": "https://models.example.com/v1"}
    res = client.api("settings", {"ai": {"enabled": True, "model": "m1", **local}})
    assert res["settings"]["ai"]["enabled"] and res["settings"]["ai"]["key"] == "missing"
    st = client.api("state")["ai"]
    assert st == {"enabled": True, "drafts": False, "provider": "openai-compatible", "model": "m1", "key": "missing"}
    # drafting patches is a second switch: it sends whole functions
    assert client.api("settings", {"ai": {"drafts": True}})["settings"]["ai"]["drafts"] is True
    assert client.api("state")["ai"]["drafts"] is True and "drafts: true" in client.app.project.config_path.read_text()
    assert client.api("ai-key", {"key": "sk-secret-123", **local})["key"].startswith("stored on this machine")
    for view in ("settings", "state"):
        assert "sk-secret-123" not in json.dumps(client.api(view))
    assert "sk-secret-123" not in client.app.project.config_path.read_text()
    assert client.api("ai-key", {"forget": True, **local})["key"] == "missing"
    client.api("settings", {"ai": {"enabled": True, "provider": "openai-compatible", "model": ""}}, status=409)
    client.api("settings", {"ai": {"enabled": False}})
    assert client.api("state")["ai"]["enabled"] is False and client.api("state")["ai"]["drafts"] is False


@needs_clang
def test_checking_your_own_change_through_the_browser(client):
    diff = (
        "--- a/src/util.c\n+++ b/src/util.c\n@@ -10,4 +10,4 @@\n int util_seen(void)\n {\n"
        "-    return last_seen ? *last_seen : -1;\n+    return last_seen != 0 ? *last_seen : -1;\n }\n"
    )
    t = client.api("patch", {"diff": diff, "title": "explicit null test", "removes": []})
    assert t["state"] == "proposed" and t["recipe"] == "patch" and t["origin"] == {"kind": "manual"}
    assert "a hand-written change" in t["card"] and "+    return last_seen != 0" in t["patch"]["diff"]
    row = next(r for r in client.api("ledger") if r["id"] == t["id"])
    assert row["title"] == "explicit null test" and row["recipe"] == "patch"
    bad = client.api("patch", {"diff": diff.replace("last_seen ?", "seen ?"), "title": "stale"})
    assert bad["state"] == "blocked" and "does not match" in json.dumps(bad["candidate"]["preconditions"])
    for x in (t, bad):
        client.api(f"txn/{x['id']}/skip", {})
