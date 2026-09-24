"""The output-parameter recipe: both forms end to end, and each precondition's counterexample."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import HAVE_CLANG, HAVE_MAKE, run_cli

from weaver.analysis.inventory import load_inventory
from weaver.config import load_project
from weaver.recipes import CATALOG, RecipeContext

needs_build = pytest.mark.skipif(not (HAVE_CLANG and HAVE_MAKE), reason="clang and make required")

LIB_H = """\
#ifndef LIB_H
#define LIB_H

#define READ_OK 0
typedef int value_t;
typedef struct { int a; } rec_t;

/* Reads a value for a key; returns 0, 1 for a clamped key, or -1 without an output. */
int read_value(int key, value_t *out);
int partial(int a, int *out);
int get_rec(rec_t *out);
int reads_back(int *io);
int in_cond(int *out);
int count_pos(int a, int b, int *out);

#endif
"""

LIB_C = """\
#include <stddef.h>
#include "lib.h"

int read_value(int key, value_t *out)
{
    if (out == NULL)
        return -1;
    if (key > 10) {
        *out = 0;
        return 1;
    }
    *out = key * 3;
    return READ_OK;
}

int partial(int a, int *out)
{
    if (a > 0) {
        *out = a;
        return 0;
    }
    return -1;
}

int reads_back(int *io)
{
    *io = *io + 1;
    return 0;
}

int in_cond(int *out)
{
    *out = 3;
    return 1;
}

int get_rec(rec_t *out)
{
    rec_t r = {5};
    *out = r;
    return 0;
}

int count_pos(int a, int b, int *out)
{
    if (a > 0)
        *out = a;
    if (b > 0)
        return 1;
    *out = b;
    return 0;
}
"""

MAIN_C = """\
#include <stdio.h>
#include "lib.h"

int g_value;
int *g_keep;

struct pair
{
    int a;
    int b;
};

static void square(int a, int *out)
{
    *out = a * a;
}

static void fill_global(int *out)
{
    *out = 42;
}

static void twice(int a, int *out)
{
    *out = 2 * a;
}

static void leaf(int a, int *out)
{
    *out = a + 1;
}

static int mid(int a, int *o)
{
    if (o == NULL)
        return -1;
    leaf(a, o);
    return 0;
}

static void leaf2(int *out)
{
    *out = 1;
}

static void keeps(int *o)
{
    g_keep = o;
    leaf2(o);
}

static void maybe_set(int a, int *out)
{
    if (a)
        *out = a;
}

static void set_if(int a, int *out)
{
    if (!a)
        return;
    *out = a;
}

static int wrap(int k)
{
    int z = 0;
    return read_value(k, &z);
}

int main(void)
{
    int s, v = 0, w = 0, p = 0, r = 5, c = 0, d = 0;
    rec_t rr;
    get_rec(&rr);
    int unused;
    square(3, &unused);
    int z2;
    int s2 = read_value(5, &z2);
    int d0;
    d0 = 1;
    twice(1, &d0);
    square(7, &v);
    s = read_value(4, &w);
    int t = read_value(20, &d);
    fill_global(&g_value);
    int q = partial(1, &p);
    reads_back(&r);
    if (in_cond(&c))
        printf("cond %d\\n", c);
    printf("v=%d s=%d w=%d t=%d d=%d g=%d q=%d p=%d r=%d wrap=%d\\n", v, s, w, t, d, g_value, q, p, r, wrap(2));
    printf("rec=%d s2=%d\\n", rr.a, s2);
    struct pair pr = {0, 0};
    square(5, &pr.b);
    int lv = 0;
    int ms = mid(4, &lv);
    int kk = 0;
    keeps(&kk);
    int m1 = 9, m2 = 9, m3 = 9, m4 = 9;
    maybe_set(0, &m1);
    maybe_set(6, &m2);
    set_if(0, &m3);
    set_if(8, &m4);
    int k1 = 7, k2 = 7;
    int c1 = count_pos(-1, 2, &k1);
    int c2 = count_pos(5, 2, &k2);
    printf("pr=%d lv=%d ms=%d kk=%d m=%d,%d,%d,%d\\n", pr.b, lv, ms, kk, m1, m2, m3, m4);
    printf("k=%d,%d c=%d,%d\\n", k1, k2, c1, c2);
    return 0;
}
"""

MAKEFILE = """\
CC ?= cc
CFLAGS ?= -O2 -std=c11 -Wall -Werror
BUILD ?= build

$(BUILD)/app: main.c lib.c lib.h | $(BUILD)
\t$(CC) $(CFLAGS) -o $@ main.c lib.c

$(BUILD):
\tmkdir -p $(BUILD)
"""


def _project(tmp: Path) -> Path:
    from weaver.capture.shims import finalize, make_shim

    root = tmp / "outp"
    root.mkdir()
    for name, text in (("lib.h", LIB_H), ("lib.c", LIB_C), ("main.c", MAIN_C), ("Makefile", MAKEFILE)):
        (root / name).write_text(text)
    cap = root / ".weaver" / "capture"
    shim = make_shim(cap / "shims", "cc", shutil.which("clang"), cap / "log.jsonl")
    subprocess.run(["make", "-s", f"CC={shim}", "BUILD=build"], cwd=root, check=True)
    finalize(cap / "log.jsonl", root / "build")
    cfg = {
        "schema": "weaver.project/1",
        "project": {"name": "outp", "workspace_exclude": [".git", "build", "out"]},
        "acceptance": {"require": ["compile", "mechanical-recheck", "differential-testing"]},
        "profiles": [
            {
                "id": "clang",
                "compile_commands": "build/compile_commands.json",
                "validation": {
                    "build": {
                        "run": ["make", "-s", "-C", "{workspace}", "CC=clang", "BUILD=out"],
                        "cwd": "{workspace}",
                    },
                    "compare": [{"name": "app", "run": ["./out/app"], "cwd": "{workspace}"}],
                },
            }
        ],
    }
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    assert run_cli(root, "collect") == 0 and run_cli(root, "inventory") == 0
    return root


def _eval(root: Path) -> dict[str, object]:
    proj = load_project(root)
    inv = load_inventory(proj)
    ctx = RecipeContext(proj, inv)
    out = {}
    for f in inv["findings"]:
        if CATALOG["output-param"].applicable(f):
            out[f["function"]] = (f, CATALOG["output-param"].evaluate(ctx, f))
    return out


def _pre(res, pid):
    return next(p for p in res.preconditions if p.id == pid)


def _run(root: Path) -> str:
    subprocess.run(["make", "-s", "CC=clang", "BUILD=check"], cwd=root, check=True)
    return subprocess.run(["./check/app"], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture(scope="module")
def outp(tmp_path_factory):
    return _project(tmp_path_factory.mktemp("outparam"))


@needs_build
def test_blockers_are_explained(outp):
    r = _eval(outp)
    assert set(r) == {
        "read_value", "partial", "reads_back", "in_cond", "square", "fill_global", "get_rec", "twice", "leaf",
        "leaf2", "maybe_set", "set_if", "count_pos",
    }  # fmt: skip
    _, res = r["reads_back"]
    assert _pre(res, "OP.write-only").status == "violated"
    _, res = r["in_cond"]
    assert "used inside a larger expression" in " ".join(_pre(res, "OP.call-sites").evidence)
    _, res = r["fill_global"]
    assert "passes &g_value" in " ".join(_pre(res, "OP.private-target").evidence)
    _, res = r["twice"]  # the caller only assigns d0: dropping it would need dead-store elimination
    assert "the caller never reads d0" in " ".join(_pre(res, "OP.call-sites").evidence)
    _, res = r["get_rec"]  # a typedef is resolved through the unit: rec_t is a structure, value_t a scalar
    assert "pointee 'rec_t' is not a scalar type" in _pre(res, "OP.parameter-type").evidence
    _, res = r["read_value"]
    assert _pre(res, "OP.parameter-type").status == "established"
    _, res = r["leaf2"]  # keeps() stores the pointer it forwards: the chain is not private
    assert "keeps()'s parameter 'o', which keeps() also uses otherwise" in " ".join(
        _pre(res, "OP.private-target").evidence
    )


@needs_build
def test_optional_output_and_leaf_first(outp):
    r = _eval(outp)
    _, res = r["partial"]  # returns early without writing: the record says whether it wrote
    assert res.eligible and "output: optional (has_value)" in res.notes
    assert "never before 1" in " ".join(_pre(res, "OP.written-before-return").evidence)
    _, res = r["count_pos"]
    assert res.eligible and "on some paths before 1" in " ".join(_pre(res, "OP.written-before-return").evidence)
    _, res = r["leaf"]  # mid() passes on its own parameter; main() passes &lv
    assert res.eligible, [p.to_json() for p in res.preconditions if p.status != "established"]
    assert "forward the caller's own pointer parameter" in " ".join(_pre(res, "OP.private-target").evidence)
    assert any("mid()'s parameter 'o' is then only written" in n for n in res.notes)
    assert "mid" not in r  # mid() only passes 'o' on: not a candidate until leaf() returns the value


@needs_build
def test_every_form_is_applied_and_preserves_behaviour(outp):
    before = _run(outp)
    r = _eval(outp)
    _, sq = r["square"]
    assert sq.eligible, [p.to_json() for p in sq.preconditions if p.status != "established"]
    assert sq.capabilities_required == []
    _, rv = r["read_value"]
    assert rv.eligible, [p.to_json() for p in rv.preconditions if p.status != "established"]
    assert rv.capabilities_required == ["value_records"]

    # convert, validate and accept every eligible candidate, re-analysing after each: leaf() comes first, and
    # mid() becomes eligible once its parameter is only written
    assert run_cli(outp, "auto", "--recipe", "output-param", "--max", "20") == 0
    done = {f for f in ("square", "read_value", "partial", "count_pos", "leaf", "mid", "maybe_set", "set_if")}
    assert not done & set(_eval(outp)), sorted(done & set(_eval(outp)))

    main = (outp / "main.c").read_text()
    lib_c, lib_h = (outp / "lib.c").read_text(), (outp / "lib.h").read_text()
    assert "static int square(int a)" in main and "v = square(7);" in main and "return out;" in main
    assert "} read_value_result_t;" in lib_h
    assert "read_value_result_t read_value(int key);" in lib_h
    assert "if (0)" in lib_c and ".value = out" in lib_c
    assert "value_t value;" in lib_h and "{.status = (READ_OK), .value = out}" in lib_c  # a macro as the status
    ws = r"\s+"
    call4 = (
        rf"\{{{ws}read_value_result_t (read_value_r\d+) = read_value\(4\);{ws}s = \1\.status;{ws}w = \1\.value;{ws}\}}"
    )
    assert re.search(call4, main)
    assert re.search(
        rf"read_value_result_t (read_value_r\d+) = read_value\(20\);{ws}int t = \1\.status;{ws}d = \1\.value;", main
    )
    assert "return read_value(k).status;" in main and "int z = 0;" not in main  # z was never read
    assert lib_h.index("} read_value_result_t;") < lib_h.index("/* Reads a value")  # above the doc comment
    # outputs the caller never reads are discarded with their variables (set-but-unused is an error here)
    assert "(void)square(3);" in main and "int unused;" not in main
    assert "int s2 = read_value(5).status;" in main and "int z2;" not in main
    assert "pr.b = square(5);" in main  # a field of a private local
    # optional outputs: the caller assigns only what was written
    assert re.search(rf"int q = (partial_r\d+)\.status;{ws}if \(\1\.has_value\) p = \1\.value;", main)
    assert "_Bool has_value;" in lib_h and "int count_pos_written = 0;" not in lib_c
    assert "_Bool out_written = 0;" in lib_c and "*out" not in lib_c.split("count_pos", 1)[1]
    assert ".has_value = out_written}" in lib_c and "out = b; out_written = 1;" in lib_c
    assert "{ out = a; out_written = 1; }" in main  # braces keep the flag under the if
    assert "return (set_if_result_t){.has_value = 0};" in main
    assert "return (set_if_result_t){.value = out, .has_value = 1};" in main
    # leaf-first: leaf() returned the value into mid()'s parameter, then mid() returned it itself
    assert "o = leaf(a);" in main and "leaf(a, o)" not in main and "static mid_result_t mid(int a)" in main
    assert re.search(rf"(mid_r\d+) = mid\(4\);{ws}int ms = \1\.status;{ws}lv = \1\.value;", main)
    assert _run(outp) == before  # the program prints exactly what it printed before
