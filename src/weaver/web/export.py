"""A read-only snapshot of the web interface: one HTML file, no server.

The exporter asks the same routes the browser asks (``route(app, "GET", …)``) for
every view a reader can reach without changing anything: project state, the
pointer list and map, each pointer's details and neighbourhood, the source of
each analysed file, the ledger with every transaction card, the settings and,
when a snapshot exists, the change-impact report against the latest one.  The
answers are embedded next to the unchanged ``app.js``, which serves them in
snapshot mode.  Actions that would change the project (compile, propose,
validate, accept, save settings) explain how to run them locally instead.

The embedded data is the analysed source code and its evidence: treat an
exported page like the source tree it was made from.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from weaver.config import Project
from weaver.web import api
from weaver.web.server import STATIC, App, route

SCHEMA = "weaver.ui-snapshot/1"


def snapshot_dataset(
    project: Project,
    scope: list[str] | None = None,
    label: str | None = None,
    description: str = "",
    max_files: int = 400,
    max_file_bytes: int = 400_000,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Record every read-only API response the interface needs for ``project``."""
    say = log or (lambda _m: None)
    app = App(None, scope)
    app.project = project
    app.cache = api.Cache(app.scope)
    responses: dict[str, Any] = {}

    def get(path: str, q: dict[str, str] | None = None) -> Any:
        key = path + ("?" + "&".join(f"{k}={v}" for k, v in q.items()) if q else "")
        try:
            responses[key] = route(app, "GET", "/api/" + path, q or {}, {})
        except FileNotFoundError:
            return None
        return responses[key]

    state = get("state")
    state["recent"], state["running"] = [], []
    say("evaluating recipes over the whole program ...")
    plist = get("pointers")
    get("map")
    get("settings")
    for prof in (get("simplify") or {}).get("profiles", []):
        get("simplify", {"profile": prof["id"]})
    ids = [p["id"] for p in plist["pointers"]]
    say(f"{len(ids)} pointer(s) in scope")
    for fid in ids:
        get(f"pointer/{fid}")
        get(f"neighborhood/{fid}")
    files: list[str] = []
    for p in plist["pointers"]:
        if p["file"] not in files:
            files.append(p["file"])
    first = get("source", {"file": files[0]}) if files else None
    for f in (first or {}).get("files", []):
        if f not in files:
            files.append(f)
    root = project.root.resolve()
    kept = 0
    for f in files:
        if kept >= max_files:
            break
        p = root / f
        if p.is_file() and p.stat().st_size <= max_file_bytes and get("source", {"file": f}) is not None:
            kept += 1
    say(f"{kept} source file(s)")
    proposals: dict[str, str] = {}
    for t in get("ledger") or []:
        get(f"ledger/{t['id']}")
        proposals[t["finding_id"]] = t["id"]  # the latest transaction for each pointer

    impact = None
    snaps = state.get("snapshots") or []
    if snaps:
        from weaver.impact import impact as run_impact

        say(f"change impact since {snaps[-1]['name']} ...")
        impact = run_impact(project, snaps[-1]["name"], revalidate=False, log=lambda _m: None)

    eligible = [p["id"] for p in plist["pointers"] if any(r["eligible"] for r in p["recipes"].values())]
    blocked = [p["id"] for p in plist["pointers"] if p["recipes"]]
    name = label or project.name
    data = {
        "id": re.sub(r"[^A-Za-z0-9_.~-]+", "-", name).strip("-").lower() or "project",
        "label": name,
        "description": description,
        "scope": app.scope,
        "select": (eligible or blocked or ids or [None])[0],
        "proposals": proposals,
        "responses": responses,
        "impact": impact,
    }
    # absolute paths of the machine that exported the page are not the reader's business
    text = json.dumps(data)
    for rootpath in {str(project.root), str(root)}:
        text = text.replace(json.dumps(rootpath)[1:-1], project.name)
    return json.loads(text)


def render_page(
    datasets: list[dict[str, Any]],
    title: str = "Weaver",
    fragment: bool = False,
    repo: str | None = None,
    branch: str | None = None,
) -> str:
    """One self-contained page: the interface's CSS and JS with the recorded datasets.

    ``fragment`` leaves out the doctype, ``<html>``, ``<head>`` and ``<body>`` for hosts that
    wrap a page in their own document.  ``repo`` and ``branch`` go into the page's
    instructions for running Weaver locally.
    """
    index = (STATIC / "index.html").read_text()
    body = index.split("<body>", 1)[1].split("</body>", 1)[0].strip()
    css = (STATIC / "app.css").read_text()
    js = (STATIC / "app.js").read_text().replace("</script", "<\\/script")
    web = None
    if repo and repo.startswith("https://github.com/"):
        web = repo.removesuffix(".git") + (f"/tree/{branch}" if branch else "")
    snap = {"schema": SCHEMA, "datasets": datasets, "repo": repo, "branch": branch, "web": web}
    # '<' only occurs inside JSON strings, where its \u escape is the same character: the script cannot end early
    data = json.dumps(snap, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c")
    icon = re.search(r'<link rel="icon"[^>]*>', index)
    viewport = '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
    head = [
        f"<title>{title}</title>",
        "" if fragment else viewport,
        icon.group(0) if icon and not fragment else "",
        f"<style>\n{css}\n</style>",
    ]
    parts = [
        "\n".join(x for x in head if x),
        body,
        f"<script>window.WEAVER_SNAPSHOT = {data};</script>",
        f"<script>\n{js}\n</script>",
    ]
    if fragment:
        return "\n".join(parts) + "\n"
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        + parts[0]
        + "\n</head>\n<body>\n"
        + "\n".join(parts[1:])
        + "\n</body>\n</html>\n"
    )


def load_dataset(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if "responses" not in data:
        raise ValueError(f"{path} is not a dataset written by 'weaver export-ui --json'")
    return data
