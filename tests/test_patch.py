"""Your own change, and an AI's draft, checked like a recipe's patch."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from conftest import HAVE_CLANG, HAVE_MAKE, run_cli
from test_llm import FakeChat, reply
from test_output_param import _project, _run

from weaver.analysis.inventory import load_inventory
from weaver.config import AIConfig, load_project
from weaver.errors import WeaverError
from weaver.ledger import Ledger
from weaver.patch import apply_file, candidate_from_patch, parse_diff

needs_build = pytest.mark.skipif(not (HAVE_CLANG and HAVE_MAKE), reason="clang and make required")

# twice() writes its result through a pointer that main() only initialises: no recipe can convert it
TWICE = """\
--- a/main.c
+++ b/main.c
@@ -30,4 +30,4 @@
-static void twice(int a, int *out)
+static int twice(int a)
 {
-    *out = 2 * a;
+    return 2 * a;
 }
@@ -150,5 +150,3 @@
     int z2;
     int s2 = read_value(5, &z2);
-    int d0;
-    d0 = 1;
-    twice(1, &d0);
+    (void)twice(1);
     square(7, &v);
"""

# square() keeps its output parameter and now also lets it escape into a global
ESCAPE = """\
--- a/main.c
+++ b/main.c
@@ -13,5 +13,6 @@
 static void square(int a, int *out)
 {
     *out = a * a;
+    g_keep = out;
 }
"""


def _fid(root: Path, function: str, name: str) -> str:
    inv = load_inventory(load_project(root))
    return next(f["id"] for f in inv["findings"] if f.get("function") == function and f.get("name") == name)


def _facts(txn: dict) -> dict:
    return next(r for r in txn["validation"]["records"] if r["name"] == "pointer facts")


# -- diffs ------------------------------------------------------------------------------------------------------


def test_hunks_are_placed_by_content():
    text = "a\nb\nc\nd\ne\nf\ng\n"
    (fp,) = parse_diff("--- a/x.c\n+++ b/x.c\n@@ -40,3 +40,3 @@\n c\n-d\n+D\n e\n")
    new, notes = apply_file(text, fp)  # stated at line 40: found at line 3
    assert new == "a\nb\nc\nD\ne\nf\ng\n" and notes == ["x.c: hunk 1 applied at line 3, not 40 as stated"]
    (fp,) = parse_diff("--- a/x.c\n+++ b/x.c\n@@ -2,2 +2,2 @@\n b   \n-c\n+C\n")  # trailing blanks differ
    assert apply_file(text, fp)[0] == "a\nb\nC\nd\ne\nf\ng\n"
    (fp,) = parse_diff("--- a/x.c\n+++ b/x.c\n@@ -1,1 +1,1 @@\n-zz\n+y\n")
    with pytest.raises(WeaverError, match="hunk 1 .* does not match"):
        apply_file(text, fp)
    crlf = "int a;\r\nint b;\r\nint c;"  # line endings kept, and no newline added at the end
    (fp,) = parse_diff("--- a/x.c\n+++ b/x.c\n@@ -2,2 +2,2 @@\n int b;\n-int c;\n+long c;\n")
    assert apply_file(crlf, fp)[0] == "int a;\r\nint b;\r\nlong c;"
    with pytest.raises(WeaverError, match="creates"):
        parse_diff("--- /dev/null\n+++ b/new.c\n@@ -0,0 +1 @@\n+x\n")
    with pytest.raises(WeaverError, match="no unified diff"):
        parse_diff("just prose")


# -- your own change --------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def proj(tmp_path_factory):
    return _project(tmp_path_factory.mktemp("patch"))


@needs_build
def test_blocked_patches_say_why(proj):
    inv = load_inventory(load_project(proj))
    p = load_project(proj)
    bad = candidate_from_patch(p, inv, TWICE.replace("*out = 2 * a;", "*out = 3 * a;"), [])
    assert not bad["eligible"] and "hunk 1" in " ".join(bad["preconditions"][0]["evidence"])
    mk = candidate_from_patch(p, inv, "--- a/Makefile\n+++ b/Makefile\n@@ -1,1 +1,1 @@\n-CC ?= cc\n+CC ?= gcc\n", [])
    analysed = next(x for x in mk["preconditions"] if x["id"] == "PATCH.analysed")
    assert analysed["status"] == "unresolved" and "Makefile" in analysed["evidence"][0]
    elsewhere = candidate_from_patch(p, inv, TWICE, [_fid(proj, "read_value", "out"), "P-0000000000"])
    t = next(x for x in elsewhere["preconditions"] if x["id"] == "PATCH.targets")
    assert "is declared in lib.c, which the patch does not edit" in " ".join(t["evidence"])
    assert "P-0000000000 is not a pointer finding" in " ".join(t["evidence"])
    txn = Ledger(p).propose_patch(TWICE.replace("*out = 2 * a;", "*out = 3 * a;"))
    assert txn["state"] == "blocked" and "PATCH.applies violated" in txn["history"][-1]["note"]


@needs_build
def test_a_change_that_keeps_its_pointer_fails_and_its_escape_is_listed(proj):
    target = _fid(proj, "square", "out")
    txn = Ledger(load_project(proj)).propose_patch(ESCAPE, [target], "square keeps a copy")
    assert txn["state"] == "proposed"
    txn = Ledger(load_project(proj)).validate(txn["id"])
    fx = _facts(txn)
    assert txn["state"] == "rejected" and fx["outcome"] == "failed"
    assert "still exists after the patch" in fx["detail"]
    assert fx["facts"]["targets"] == [{"finding": target, "name": "out", "status": "present"}]
    assert any("its value now leaves the function" in x["text"] for x in fx["facts"]["review"])


@needs_build
def test_a_change_that_breaks_a_pinned_contract_fails(proj):
    target = _fid(proj, "square", "out")
    assert run_cli(proj, "contract", "pin", target, "--expect", "no-escape", "--reason", "stays local") == 0
    try:
        txn = Ledger(load_project(proj)).propose_patch(ESCAPE, [], "no target named")
        txn = Ledger(load_project(proj)).validate(txn["id"])
        fx = _facts(txn)
        assert txn["state"] == "rejected" and "no-escape" in fx["detail"] and "no longer holds" in fx["detail"]
    finally:
        load_project(proj).contracts_path.unlink(missing_ok=True)


# -- an AI's draft ----------------------------------------------------------------------------------------------


def _answer(patch: str) -> str:
    return (
        "### Intent\ntwice() returns its result; main() discards it, as it never read d0 (main.c:80).\n\n"
        f"### Patch\n```diff\n{patch}```\n\n### What to check\nThe differential run.\n\n"
        "### Assumptions and limits\nNone."
    )


@pytest.fixture()
def keydir(tmp_path, monkeypatch):
    monkeypatch.setenv("WEAVER_CONFIG_DIR", str(tmp_path / "cfg"))
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(v, raising=False)


@needs_build
def test_drafts_are_off_unless_turned_on_and_a_dry_run_shows_the_source(proj, keydir):
    from weaver.llm.draft import GUIDE_PATH, draft

    p = load_project(proj)
    target = _fid(proj, "twice", "out")
    with pytest.raises(WeaverError, match="AI drafts are off"):
        draft(dataclasses.replace(p, ai=AIConfig(enabled=True)), target)  # explanations on is not enough
    req = draft(p, target, dry_run=True)
    assert req["enabled"] is False and req["system"] == GUIDE_PATH.read_text()
    assert "definition of twice()" in req["user"] and "caller main()" in req["user"]
    assert "    *out = 2 * a;" in req["user"] and "twice(1, &d0);" in req["user"]  # the exact text to patch


@needs_build
def test_a_draft_that_does_not_apply_is_retried_once_then_validated(proj, keydir):
    from weaver.llm.draft import draft
    from weaver.llm.keys import store_key

    target = _fid(proj, "twice", "out")
    wrong = TWICE.replace(" {\n-    *out", " {\n-    *res")  # the model misquotes a line
    server = FakeChat([reply(_answer(wrong)), reply(_answer(TWICE))])
    try:
        ai = AIConfig(enabled=True, drafts=True, provider="openai-compatible", base_url=server.url, model="m1")
        store_key(ai.key_id, "sk-test")
        p = dataclasses.replace(load_project(proj), ai=ai)
        res = draft(p, target)
    finally:
        server.close()
    assert len(server.requests) == 2
    retry = server.requests[1]["body"]["messages"][-1]["content"]
    assert "Weaver could not use its patch: main.c: hunk 1" in retry
    led = Ledger(p)
    txn = led.load(res["txn"])
    assert txn["state"] == "proposed" and txn["origin"]["kind"] == "ai" and txn["origin"]["attempts"] == 2
    assert txn["origin"]["model"] == "m1" and txn["candidate"]["recheck"]["targets"] == [target]
    assert "twice() returns its result" in txn["origin"]["intent"]
    txn = led.validate(txn["id"])  # the same gate as a hand-written change
    assert txn["state"] == "validated" and _facts(txn)["facts"]["targets"][0]["status"] == "removed"
    log = json.loads(Path(txn["origin"]["transcript"]).read_text())
    assert log["guide_version"] == 1 and len(log["attempts"]) == 2 and log["result"] == "patch"


@needs_build
def test_a_draft_without_a_patch_proposes_nothing(proj, keydir):
    from weaver.llm.draft import draft

    answer = (
        "### Intent\nA design decision is needed.\n\n### Patch\nNone. The buffer is shared.\n\n### What to check\nNone."
    )
    server = FakeChat([reply(answer)])
    try:
        ai = AIConfig(enabled=True, drafts=True, provider="openai-compatible", base_url=server.url, model="m1")
        res = draft(dataclasses.replace(load_project(proj), ai=ai), _fid(proj, "twice", "out"))
    finally:
        server.close()
    assert "txn" not in res and "no patch" in res["error"] and len(server.requests) == 1


# -- accepted: last, since it changes the project ---------------------------------------------------------------


@needs_build
def test_your_own_change_is_checked_accepted_and_preserves_behaviour(proj):
    before = _run(proj)
    target = _fid(proj, "twice", "out")
    diff = proj / "twice.diff"
    diff.write_text(TWICE)
    assert run_cli(proj, "patch", str(diff), "--removes", target, "--title", "twice() returns its result") == 0
    led = Ledger(load_project(proj))
    txn = [t for t in led.all() if t.get("title") == "twice() returns its result"][-1]
    notes = txn["candidate"]["notes"]
    assert "main.c: hunk 1 applied at line 23, not 30 as stated" in notes  # placed by content
    txn = led.validate(txn["id"])
    fx = _facts(txn)
    assert txn["state"] == "validated" and txn["validation"]["strength"] == "behavioural"
    assert fx["facts"]["targets"][0]["status"] == "removed" and fx["facts"]["review"] == []
    assert [x["name"] for x in fx["facts"]["removed"]] == ["out"]
    assert run_cli(proj, "accept", txn["id"]) == 0 and run_cli(proj, "refresh") == 0
    main = (proj / "main.c").read_text()
    assert "static int twice(int a)" in main and "(void)twice(1);" in main and "int d0;" not in main
    inv = load_inventory(load_project(proj))
    assert not any(f.get("function") == "twice" for f in inv["findings"])
    assert _run(proj) == before
    assert run_cli(proj, "revert", txn["id"]) == 0 and "twice(1, &d0);" in (proj / "main.c").read_text()
