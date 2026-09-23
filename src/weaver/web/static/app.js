/* Weaver web interface.
 *
 * Plain DOM, no build step, no network access beyond this server.  All text from
 * the project (source code, identifiers, compiler output) is inserted with
 * textContent, never as HTML.
 */
"use strict";

// A page written by 'weaver export-ui' carries recorded answers instead of a server.
const SNAP = window.WEAVER_SNAPSHOT || null;
const TOKEN = SNAP ? "" : document.querySelector('meta[name="weaver-token"]').content;
const CLASSES = ["read-only", "writes", "escapes", "reassigned", "unused"];
const CLASS_LABEL = {
  "read-only": "read-only", writes: "writes through", escapes: "escapes",
  reassigned: "reassigned", unused: "unused", unknown: "unknown",
};
const CLASS_HELP = {
  "read-only": "only reads its target",
  writes: "writes its target",
  escapes: "its value leaves the function (argument, copy, return, cast)",
  reassigned: "is reassigned or advanced",
  unused: "has no uses",
};

const S = {
  state: null, pointers: [], removed: [], map: null,
  selected: null, detail: null, view: "map", graphMode: "auto",
  filters: { classes: new Set(), eligible: false, q: "", kinds: new Set() },
  source: null, sourceFile: null, focusLine: null,
  snapshots: [], impact: null, ledger: [], jobs: new Map(), dataset: 0,
};

// ---------------------------------------------------------------- utilities
async function api(path, body) {
  if (SNAP) return snapApi(path, body);
  const opt = { headers: { "X-Weaver-Token": TOKEN } };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch("/api/" + path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(data.error || r.statusText);
    e.data = data;
    throw e;
  }
  return data;
}

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k.startsWith("on")) el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const kid of kids.flat()) {
    if (kid === undefined || kid === null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const SVGNS = "http://www.w3.org/2000/svg";
function sv(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid) el.append(kid);
  return el;
}

function toast(msg, ms = 3200) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), ms);
}

function fail(e) {
  if (e && e.snapshot) return readOnly(e.snapshot);
  console.error(e);
  toast(e.message || String(e), 6000);
}

function classColor(cls) {
  return getComputedStyle(document.documentElement).getPropertyValue(`--c-${cls}`).trim() || "#888";
}

function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

// ---------------------------------------------------------------- boot
async function boot() {
  try {
    S.state = await api("state");
  } catch (e) { return fail(e); }
  renderTop();
  if (!S.state.project) return renderWelcome();
  renderWorkspace();
  if (SNAP) S.impact = S.impact || snapData().impact || null;
  await loadData();
  if (SNAP && !S.selected && snapData().select && window.innerWidth > 1100) await select(snapData().select);
  for (const j of S.state.running || []) followJob(j);
}

async function refreshState() {
  S.state = await api("state");
  renderTop();
}

async function loadData() {
  if (!S.state.inventory) {
    S.pointers = []; S.map = null;
    renderList(); renderView(); renderDetail();
    return;
  }
  try {
    const [pl, map] = await Promise.all([api("pointers"), api("map")]);
    S.pointers = pl.pointers; S.removed = pl.removed; S.map = map;
  } catch (e) { fail(e); }
  if (S.selected && !S.pointers.some((p) => p.id === S.selected)) {
    // the selected pointer no longer exists (removed by an accepted transaction, or the code changed)
    S.selected = null; S.detail = null;
    if (S.graphMode === "pointer") S.graphMode = "auto";
  }
  renderList(); renderView();
  if (S.selected) await select(S.selected, { keepView: true });
  else renderDetail();
}

// ---------------------------------------------------------------- top bar
function renderTop() {
  const info = clear(document.getElementById("project-info"));
  const acts = clear(document.getElementById("top-actions"));
  const st = S.state;
  if (!st || !st.project) return;
  const p = st.project;
  info.append(h("span", { class: "name", text: p.name }), h("span", { class: "path", title: p.root, text: p.root }));
  for (const prof of p.profiles) {
    const fl = (st.flow || {})[prof.id] || {};
    const g = fl.gcc || null;
    const parts = [];
    if (fl.run && fl.run.status !== "unavailable") parts.push(fl.complete ? "SVF ✓" : `SVF ${fl.run.status}`);
    if (g && g.status !== "unavailable") parts.push(g.complete ? "GCC ✓" : `GCC ${g.current}/${g.images}`);
    const anyOk = fl.complete || (g && g.complete);
    const flowTxt = parts.length ? "points-to " + parts.join(" ") : "no points-to";
    info.append(h("span", { class: "pill", title: `compile database: ${prof.compile_commands}\npoints-to backends: ${(st.flow_backends || []).join(", ") || "none"}` +
        (g && g.reason ? `\nGCC: ${g.reason}` : "") + (fl.run && fl.run.reason ? `\nSVF: ${fl.run.reason}` : "") },
      h("span", { class: "dot", style: { color: prof.compile_commands_exist ? "var(--ok)" : "var(--faint)" } }),
      prof.id, h("span", { style: { color: anyOk ? "var(--ok)" : "var(--faint)" }, text: " · " + flowTxt })));
  }
  if (st.stale_files && st.stale_files.length) {
    info.append(h("span", { class: "pill warn", title: st.stale_files.join("\n") },
      h("span", { class: "dot" }), `${st.stale_files.length} file(s) changed since analysis`));
  }
  const val = st.validation;
  if (val) {
    const weak = val.level !== "behavioural";
    info.append(h("button", { class: "pill" + (weak ? " warn" : ""), onclick: () => openSettings(),
      title: (val.notes || []).join("\n") || "tests run on every transaction and the acceptance policy requires them" },
      h("span", { class: "dot", style: { color: weak ? "var(--warn)" : "var(--ok)" } }),
      val.level === "compile-only" ? "validation: compile only" : val.level === "behavioural-optional" ? "tests not required" : "validation: tests"));
  }
  const busy = (st.running || []).some((j) => j.state === "running");
  acts.append(...[
    h("button", { class: "btn primary", disabled: busy, onclick: () => compile(),
      title: "Rebuild through capture shims (if configured), collect compiler evidence, check fidelity, " +
             "build the inventory and run points-to analysis" }, "▶ Compile & analyse"),
    h("button", { class: "btn", disabled: busy || !st.inventory || !(st.flow_backends || []).length, onclick: () => runFlow(),
      title: "Run points-to analysis as a separate job: " + ((st.flow_backends || []).join(" and ") || "no backend selected") +
        (st.svf_available || !(st.flow_backends || []).includes("svf") ? "" : " (SVF is not installed: pip install weaver[flow])") },
      "Points-to"),
    h("button", { class: "btn ghost", onclick: () => openSettings(), title: "Validation commands and acceptance policy" }, "Settings"),
    SNAP ? null : h("button", { class: "btn ghost", onclick: () => { S.state.project = null; renderWelcome(true); },
      title: "Open another project" }, "Open…"),
    SNAP && SNAP.datasets.length > 1 ? h("select", { class: "snap-ds", id: "snap-dataset", "aria-label": "Example project",
      onchange: (e) => switchDataset(Number(e.target.value)) },
      SNAP.datasets.map((d, i) => h("option", { value: i, selected: i === S.dataset, text: d.label }))) : null,
    SNAP ? h("button", { class: "btn", onclick: () => runLocally(),
      title: "This page is a read-only recording. See how to run Weaver on your own code." }, "Run it locally") : null,
  ].filter(Boolean));
}

// ---------------------------------------------------------------- welcome
function renderWelcome(keepProject) {
  const app = clear(document.getElementById("app"));
  if (!keepProject) clear(document.getElementById("project-info"));
  clear(document.getElementById("top-actions"));
  const openPath = h("input", { type: "text", placeholder: "/path/to/project (containing weaver.yaml)", id: "open-path" });
  const openCard = h("div", { class: "card" },
    h("h2", { text: "Open a project" }),
    h("div", { class: "field" }, h("label", { for: "open-path", text: "Project directory" }), openPath),
    h("button", { class: "btn primary", onclick: async () => {
      try {
        await api("open", { path: openPath.value.trim() });
        S.selected = null;
        boot();
      } catch (e) {
        if (e.data && e.data.needs_setup) {
          document.getElementById("setup-path").value = openPath.value.trim();
          toast("No weaver.yaml there yet — describe how to build it on the right.");
        } else fail(e);
      }
    } }, "Open"),
    (S.state && S.state.recent && S.state.recent.length) ? h("ul", { class: "recent" },
      S.state.recent.map((r) => h("li", {}, h("button", { onclick: () => { openPath.value = r; } }, r)))) : null,
  );
  const f = (id, label, attrs, hint) => h("div", { class: "field" },
    h("label", { for: id, text: label }), h("input", { id, ...attrs }), hint ? h("div", { class: "hint", text: hint }) : null);
  const setupCard = h("div", { class: "card" },
    h("h2", { text: "Set up a new project" }),
    f("setup-path", "Project directory", { placeholder: "/path/to/c/project" }),
    f("setup-build", "Build command", { value: "make -B CC={cc}" },
      "{cc} is replaced by a recording shim around the compiler; use a full rebuild so every file is seen."),
    f("setup-cc", "Production compiler", { value: "gcc" }, "The executable your build really uses (gcc, clang, a cross compiler…)."),
    f("setup-clean", "Clean command (optional)", { placeholder: "make clean" }),
    h("div", { class: "field" }, h("label", { text: "Tests (recommended)" }),
      h("div", { id: "setup-tests", class: "hint", text: "Without tests a change is accepted once it compiles and re-checks; nothing runs it." }),
      h("button", { class: "btn small", onclick: () => detectSetupTests() }, "Detect test commands")),
    f("setup-run", "Program run to compare (optional)", { placeholder: "./build/app --self-test" },
      "Run in the unpatched and patched trees after rebuilding with the real compiler; exit status and output must match."),
    f("setup-secondary", "Analysis Clang for non-Clang compilers (optional)", { placeholder: "clang" },
      "Used as a labelled secondary frontend; fidelity checks decide how far its facts are trusted."),
    h("div", { class: "field" }, h("label", { for: "setup-conc", text: "Concurrency model (preservation contract)" }),
      h("select", { id: "setup-conc" },
        h("option", { value: "" , text: "not declared (interface recipes stay blocked)" }),
        h("option", { value: "single-threaded", text: "single-threaded: nothing else writes during a call" }))),
    h("button", { class: "btn primary", onclick: async () => {
      const v = (id) => document.getElementById(id).value.trim();
      try {
        await api("setup", { path: v("setup-path"), build: v("setup-build"), compiler: v("setup-cc"),
          clean: v("setup-clean") || null, secondary: v("setup-secondary") || null, concurrency: v("setup-conc") || null,
          run: v("setup-run") || null,
          tests: [...document.querySelectorAll("#setup-tests input[type=checkbox]:checked")].map((c) => c.value)
            .concat(v("setup-test-extra") ? [v("setup-test-extra")] : []) });
        await boot();
        compile();
      } catch (e) { fail(e); }
    } }, "Create weaver.yaml and compile"),
  );
  app.append(h("div", { class: "welcome" },
    h("h1", { text: "Remove C pointers one reviewable step at a time" }),
    h("p", { class: "lead", text: "Weaver reads what your production compiler actually builds, maps every pointer and " +
      "what it does, and proposes small refactors only when their preconditions are established. Every change is " +
      "validated in isolation and can be reverted." }),
    h("div", { class: "cards" }, openCard, setupCard),
    h("div", { class: "steps" },
      [["1 · Load", "Open a project or describe its build."],
       ["2 · Compile", "Capture real compile commands; collect ASTs, macros and points-to evidence."],
       ["3 · Explore", "See every pointer, colored by what it does to its target."],
       ["4 · Refactor", "Propose, validate and accept one transaction at a time."],
       ["5 · Guard", "Snapshot, then explain how later edits changed pointer behavior."]]
        .map(([t, d]) => h("div", { class: "step" }, h("b", { text: t }), d))),
  ));
}

async function detectSetupTests() {
  const v = (id) => document.getElementById(id).value.trim();
  const box = clear(document.getElementById("setup-tests"));
  let found;
  try {
    found = await api(`detect-tests?path=${encodeURIComponent(v("setup-path"))}&build=${encodeURIComponent(v("setup-build"))}&cc=${encodeURIComponent(v("setup-cc"))}`);
  } catch (e) { box.append(h("span", { text: e.message })); return; }
  if (!found.length) box.append(h("div", { text: "No test entry point found (Makefile test/check target, CTest, Meson, test script). Add one below if the project has tests." }));
  for (const t of found) {
    box.append(h("label", { class: "check", title: t.why },
      h("input", { type: "checkbox", value: t.run, checked: true }), " ", h("code", { text: t.run }),
      h("span", { class: "d-sub", text: ` — ${t.why}${t.per_test ? " (compared test by test)" : ""}` })));
  }
  box.append(h("input", { id: "setup-test-extra", placeholder: "another test command (optional)" }));
}

// ---------------------------------------------------------------- settings
async function openSettings() {
  let st;
  try { st = await api("settings"); } catch (e) { return fail(e); }
  const cmdRow = (list, c, render) => {
    const name = h("input", { value: c.name || "", placeholder: "name", class: "s-name", "aria-label": "name" });
    const run = h("input", { value: c.run || "", placeholder: "shell command, run in the workspace", class: "s-run", "aria-label": "command" });
    const timeout = h("input", { value: c.timeout || "", placeholder: "600", class: "s-timeout", "aria-label": "timeout (s)", type: "number", min: 1 });
    const row = h("div", { class: "s-row" }, name, run, timeout,
      h("button", { class: "btn small ghost", "aria-label": "Remove", onclick: () => { list.splice(list.indexOf(c), 1); render(); } }, "✕"));
    for (const [el, k] of [[name, "name"], [run, "run"], [timeout, "timeout"]])
      el.addEventListener("input", () => { c[k] = k === "timeout" ? (Number(el.value) || null) : el.value; });
    return row;
  };
  const profBoxes = st.profiles.map((p) => {
    const box = h("div", { class: "s-prof" });
    const build = h("input", { value: (p.build && p.build.run) || "", placeholder: p.capture_build || "build command, run in each workspace", class: "s-run wide" });
    build.addEventListener("input", () => { p.build = { ...(p.build || {}), run: build.value }; });
    const render = () => {
      clear(box);
      box.append(h("h4", { text: `Profile ${p.id}` }),
        h("div", { class: "field" }, h("label", { text: "Validation build (baseline and candidate workspaces)" }), build,
          !p.build && p.capture_build ? h("button", { class: "btn small", onclick: () => { build.value = p.capture_build; p.build = { run: p.capture_build }; } }, "Use the capture build with the real compiler") : null),
        h("div", { class: "field" }, h("label", { text: "Tests — a test that passes on the unpatched tree and fails with the patch rejects the change" }),
          p.tests.map((c) => cmdRow(p.tests, c, render)),
          h("button", { class: "btn small ghost", onclick: () => { p.tests.push({ name: `test${p.tests.length}`, run: "" }); render(); } }, "+ test")),
        h("div", { class: "field" }, h("label", { text: "Differential runs — exit status and stdout must be identical on both trees" }),
          p.compare.map((c) => cmdRow(p.compare, c, render)),
          h("button", { class: "btn small ghost", onclick: () => { p.compare.push({ name: `compare${p.compare.length}`, run: "" }); render(); } }, "+ differential run")));
      const have = new Set(p.tests.map((t) => (t.run || "").split(" ")[0]));
      const sug = p.suggestions.filter((x) => !have.has(x.run.split(" ")[0]));
      if (sug.length) box.append(h("div", { class: "field" }, h("label", { text: "Detected in the project" }),
        sug.map((x) => h("div", { class: "s-sug" }, h("code", { text: x.run }), h("span", { class: "d-sub", text: " " + x.why }),
          h("button", { class: "btn small", onclick: () => {
            p.tests.push({ name: x.name, run: x.run });
            if (!build.value && p.capture_build) { build.value = p.capture_build; p.build = { run: p.capture_build }; }
            render();
          } }, "Add")))));
    };
    render();
    return box;
  });
  const req = new Set(st.acceptance.require);
  const kindHelp = { compile: "patched units compile with the production compiler", "mechanical-recheck": "the patched AST satisfies the recipe's post-conditions",
    testing: "configured tests pass (per test, against the baseline)", "differential-testing": "differential runs match the baseline" };
  const policy = h("div", { class: "field" }, h("label", { text: "A transaction is validated only when these passed" }),
    st.kinds.map((k) => h("label", { class: "check" }, h("input", { type: "checkbox", checked: req.has(k), disabled: k === "compile",
      onchange: (e) => { e.target.checked ? req.add(k) : req.delete(k); } }), ` ${k} `, h("span", { class: "d-sub", text: kindHelp[k] || "" }))));
  const prov = h("input", { type: "checkbox", checked: st.acceptance.allow_provisional });
  const minEv = h("select", {}, st.evidence_levels.map((l) => h("option", { value: l, text: l, selected: l === st.acceptance.min_evidence })));
  const taskModel = st.concurrency && typeof st.concurrency === "object";
  const conc = h("select", {}, h("option", { value: "", text: "not declared (interface recipes stay blocked)", selected: !st.concurrency }),
    h("option", { value: "single-threaded", text: "single-threaded: nothing else writes during a call (checked: no thread is started)", selected: st.concurrency === "single-threaded" }),
    taskModel ? h("option", { value: "__tasks__", selected: true,
      text: `task model: ${(st.concurrency.tasks || []).length} task(s) declared in weaver.yaml (see weaver tasks)` }) : null);
  const backend = h("select", {}, [["auto", "auto: every backend that is available"], ["svf", "SVF only"], ["gcc", "GCC IPA points-to only"], ["none", "none (reviewed models only)"]]
    .map(([v, t]) => h("option", { value: v, text: t, selected: (st.flow.backend || "auto") === v })));
  const level = st.strength.level;
  const body = [
    h("div", { class: "banner" + (level === "behavioural" ? " ok" : "") },
      h("b", { text: level === "compile-only" ? "Compile-only validation. " : level === "behavioural-optional" ? "Tests are optional. " : "Behavioural validation. " }),
      level === "behavioural" ? "Every transaction runs the configured tests in both trees before it can be accepted." : (st.strength.notes || []).join(" ")),
    ...profBoxes,
    h("h4", { text: "Acceptance policy" }), policy,
    h("div", { class: "field" }, h("label", { class: "check" }, prov, " allow accepting provisional transactions (a required check could not run)")),
    h("div", { class: "field" }, h("label", { text: "Minimum evidence for a candidate's facts" }), minEv),
    h("h4", { text: "Preservation contract" }), h("div", { class: "field" }, conc),
    h("h4", { text: "Flow evidence backend" }), h("div", { class: "field" }, backend,
      h("div", { class: "hint", text: "SVF is an optional AGPL-licensed tool run as a separate process; GCC IPA points-to uses the production compiler itself." })),
    h("div", { class: "hint", text: `Saving rewrites ${st.config} (comments are not kept; the previous file is saved as ${st.config.split("/").pop()}.bak).` }),
  ];
  modal("Validation and acceptance settings", body, (close) => [
    h("button", { class: "btn primary", onclick: async () => {
      try {
        const res = await api("settings", {
          acceptance: { require: [...req], allow_provisional: prov.checked, min_evidence: minEv.value },
          ...(conc.value === "__tasks__" ? {} : { concurrency: conc.value || null }), flow: { backend: backend.value },
          profiles: st.profiles.map((p) => ({ id: p.id, build: p.build || { run: "" }, tests: p.tests, compare: p.compare })),
        });
        close();
        toast("Settings saved" + (res.warnings.length ? " — " + res.warnings.join(" ") : ""), res.warnings.length ? 9000 : 3200);
        await refreshState();
        await loadData();
      } catch (e) { fail(e); }
    } }, "Save"),
    h("button", { class: "btn ghost", onclick: close }, "Cancel"),
  ]);
}

// ---------------------------------------------------------------- workspace
function renderWorkspace() {
  const app = clear(document.getElementById("app"));
  app.append(h("div", { class: "workspace", id: "ws" },
    h("aside", { class: "pane left", id: "left" }),
    h("section", { class: "pane center" },
      h("nav", { class: "tabs", id: "tabs", role: "tablist" }),
      h("div", { class: "view", id: "view" })),
    h("aside", { class: "pane right", id: "right" })));
  renderTabs();
}

const VIEWS = [
  ["map", "Map"], ["graph", "Graph"], ["source", "Source"], ["changes", "Changes"], ["ledger", "Transactions"],
];

function renderTabs() {
  const tabs = clear(document.getElementById("tabs"));
  for (const [id, label] of VIEWS) {
    tabs.append(h("button", { class: "tab" + (S.view === id ? " on" : ""), role: "tab", "aria-selected": S.view === id,
      onclick: () => { S.view = id; renderTabs(); renderView(); } }, label,
      id === "ledger" && S.state && S.state.transactions
        ? h("span", { class: "count", text: Object.values(S.state.transactions).reduce((a, b) => a + b, 0) || "" }) : null));
  }
}

function filtered() {
  const q = S.filters.q.toLowerCase();
  return S.pointers.filter((p) => {
    if (S.filters.classes.size && !S.filters.classes.has(p.class)) return false;
    if (S.filters.kinds.size && !S.filters.kinds.has(p.kind)) return false;
    if (S.filters.eligible && !Object.values(p.recipes).some((r) => r.eligible)) return false;
    if (q && !`${p.name} ${p.function || ""} ${p.file} ${p.type}`.toLowerCase().includes(q)) return false;
    return true;
  });
}

function renderList() {
  const left = document.getElementById("left");
  if (!left) return;
  clear(left);
  const search = h("input", { type: "search", placeholder: "Filter by name, function, file, type…", value: S.filters.q,
    "aria-label": "Filter pointers", oninput: (e) => { S.filters.q = e.target.value; renderListBody(); renderView(); } });
  const classFilters = h("div", { class: "filters" }, CLASSES.map((c) => h("button", {
    class: "filter" + (S.filters.classes.has(c) ? " on" : ""), title: CLASS_HELP[c],
    onclick: () => { S.filters.classes.has(c) ? S.filters.classes.delete(c) : S.filters.classes.add(c); renderList(); renderView(); },
  }, h("span", { class: `dot cls-${c}` }), CLASS_LABEL[c])));
  const kinds = [...new Set(S.pointers.map((p) => p.kind))].sort();
  const kindFilters = h("div", { class: "filters" },
    h("button", { class: "filter" + (S.filters.eligible ? " on" : ""), style: { color: "var(--c-eligible)" },
      onclick: () => { S.filters.eligible = !S.filters.eligible; renderList(); renderView(); } }, "✦ eligible"),
    kinds.map((k) => h("button", { class: "filter" + (S.filters.kinds.has(k) ? " on" : ""),
      onclick: () => { S.filters.kinds.has(k) ? S.filters.kinds.delete(k) : S.filters.kinds.add(k); renderList(); renderView(); } }, k)));
  left.append(h("div", { class: "list-head" }, search, classFilters, kindFilters, h("div", { class: "list-summary", id: "list-summary" })),
    h("div", { class: "plist", id: "plist", role: "listbox", "aria-label": "Pointers" }));
  renderListBody();
}

function renderListBody() {
  const box = document.getElementById("plist");
  if (!box) return;
  clear(box);
  const rows = filtered();
  const elig = rows.filter((p) => Object.values(p.recipes).some((r) => r.eligible)).length;
  document.getElementById("list-summary").textContent =
    `${rows.length} of ${S.pointers.length} pointer(s) · ${elig} eligible · ${S.removed.length} removed`;
  if (!S.pointers.length) {
    box.append(h("div", { class: "pad", style: { color: "var(--muted)" } },
      S.state.inventory ? "No pointers found." : "Nothing analysed yet. Use “Compile & analyse”."));
    return;
  }
  let group = null;
  for (const p of rows) {
    const g = `${p.file}${p.function ? " · " + p.function + "()" : p.record ? " · " + p.record : ""}`;
    if (g !== group) { box.append(h("div", { class: "pgroup", text: g })); group = g; }
    const eligible = Object.entries(p.recipes).filter(([, r]) => r.eligible).map(([k]) => k);
    box.append(h("div", { class: "prow" + (S.selected === p.id ? " sel" : ""), role: "option", tabindex: 0,
      "aria-selected": S.selected === p.id, onclick: () => select(p.id),
      onkeydown: (e) => { if (e.key === "Enter") select(p.id); } },
      h("span", { class: `dot cls-${p.class}`, title: CLASS_LABEL[p.class] }),
      h("div", {}, h("div", { class: "n", text: p.name || "(unnamed)" }),
        h("div", { class: "m", text: `${p.kind} · ${p.type || ""} · line ${p.line}` })),
      eligible.length ? h("span", { class: "tag elig", title: `eligible: ${eligible.join(", ")}`, text: "✦ " + eligible[0] }) : null));
  }
}

function renderView() {
  const v = document.getElementById("view");
  if (!v) return;
  clear(v);
  if (!S.state.inventory && S.view !== "ledger") {
    v.append(h("div", { class: "welcome" },
      h("h1", { text: "Compile the project to begin" }),
      h("p", { class: "lead", text: "Weaver will rebuild through recording shims (when a capture command is configured), " +
        "collect compiler artifacts for every translation unit, check a secondary frontend's fidelity if one is used, " +
        "build the pointer inventory, and run points-to analysis when SVF is installed." }),
      h("button", { class: "btn primary", onclick: () => compile() }, "▶ Compile & analyse")));
    return;
  }
  if (SNAP && !S.snapNoteClosed) {
    const d = snapData();
    const note = h("div", { class: "banner info" },
      h("button", { class: "btn small ghost", style: { float: "right" }, "aria-label": "Dismiss",
        onclick: () => { S.snapNoteClosed = true; note.remove(); } }, "✕"),
      h("b", { text: `Snapshot: ${d.label}. ` }), d.description ? d.description + " " : "",
      "Everything here is browsable: select pointers, open transactions, switch views. Actions that would change the code ",
      "show what they do and how to run them locally.");
    v.append(note);
  }
  ({ map: renderMap, graph: renderGraph, source: renderSource, changes: renderChanges, ledger: renderLedger })[S.view](v);
}

// ---------------------------------------------------------------- map view
function renderMap(v) {
  const visible = new Set(filtered().map((p) => p.id));
  const total = S.pointers.length + S.removed.length;
  const pct = total ? Math.round((100 * S.removed.length) / total) : 0;
  const elig = S.pointers.filter((p) => Object.values(p.recipes).some((r) => r.eligible)).length;
  v.append(h("div", { class: "map-head" },
    h("div", { class: "legend" },
      CLASSES.map((c) => h("span", { title: CLASS_HELP[c] }, h("span", { class: `dot cls-${c}` }), CLASS_LABEL[c])),
      h("span", {}, h("span", { class: "dot", style: { color: "var(--c-eligible)" } }), "ring = eligible refactor"),
      h("span", {}, h("span", { class: "dot", style: { color: "var(--faint)" } }), "dashed = removed")),
    h("div", { class: "progress", title: "pointers removed by accepted transactions" },
      `${S.removed.length} removed · ${S.pointers.length} remaining · ${elig} eligible`,
      h("div", { class: "bar" }, h("i", { style: { width: pct + "%" } })), `${pct}%`)));
  const grid = h("div", { class: "map" });
  const removedBy = {};
  for (const r of S.removed) (removedBy[`${r.file}::${r.function}`] ||= []).push(r);
  for (const f of (S.map && S.map.files) || []) {
    const counts = f.counts || {};
    const n = Object.values(counts).reduce((a, b) => a + b, 0);
    const card = h("div", { class: "fcard" },
      h("h3", {}, h("span", { text: f.file }), h("span", { class: "cnt", text: `${n} pointer(s)` })),
      h("div", { class: "stack", title: Object.entries(counts).map(([k, c]) => `${c} ${CLASS_LABEL[k]}`).join(", ") },
        CLASSES.filter((c) => counts[c]).map((c) => h("i", { style: { width: `${(100 * counts[c]) / Math.max(n, 1)}%`, background: `var(--c-${c})` } }))));
    for (const fn of f.functions) {
      const key = `${f.file}::${fn.name}`;
      const ptrs = fn.pointers.filter((p) => visible.has(p.id));
      const q = S.filters.q.toLowerCase();
      const gone = (removedBy[key] || []).filter((r) => !q || `${r.name} ${r.function || ""} ${r.file}`.toLowerCase().includes(q));
      if (!ptrs.length && !gone.length && (S.filters.q || S.filters.classes.size || S.filters.eligible || S.filters.kinds.size)) continue;
      const row = h("div", { class: "fn", dataset: { key },
        onmouseenter: () => highlightCalls(key, fn, true), onmouseleave: () => highlightCalls(key, fn, false) },
        h("div", { class: "fn-name" }, h("b", { text: fn.name === "(file scope)" ? fn.name : fn.name + "()" }),
          h("small", { text: [fn.calls && fn.calls.length ? `calls ${fn.calls.length}` : null,
            fn.callers && fn.callers.length ? `called by ${fn.callers.length}` : null,
            fn.indirect_calls ? `${fn.indirect_calls} indirect` : null].filter(Boolean).join(" · ") })),
        h("div", { class: "chips" },
          ptrs.map((p) => h("button", { class: `chip ${p.class}${p.eligible ? " elig" : ""}${S.selected === p.id ? " sel" : ""}`,
            title: `${p.name}: ${p.type} (${p.kind}) — ${CLASS_HELP[p.class] || p.class}${p.eligible ? " — eligible refactor" : ""}`,
            onclick: () => select(p.id) }, p.name || "?", h("span", { class: "k", text: p.kind === "parameter" ? "param" : p.kind === "local" ? "" : p.kind }))),
          gone.map((r) => h("span", { class: "chip removed", title: `removed by ${r.txn} (${r.recipe})` }, r.name))));
      card.append(row);
    }
    grid.append(card);
  }
  v.append(grid);
}

function highlightCalls(key, fn, on) {
  const related = new Set([...(fn.callers || [])]);
  for (const f of S.map.files) for (const g of f.functions) if ((fn.calls || []).includes(g.name)) related.add(`${f.file}::${g.name}`);
  document.querySelectorAll(".fn").forEach((el) => {
    el.classList.toggle("hl", on && related.has(el.dataset.key));
    el.classList.toggle("dim", on && !related.has(el.dataset.key) && el.dataset.key !== key);
  });
}

// ---------------------------------------------------------------- graph view
async function renderGraph(v) {
  const wrap = h("div", { class: "graph-wrap" });
  v.append(wrap);
  const mode = S.graphMode === "auto" ? (S.selected ? "pointer" : "calls") : S.graphMode;
  const tools = h("div", { class: "graph-tools" },
    h("button", { class: "btn small" + (mode === "pointer" ? " primary" : ""), disabled: !S.selected,
      onclick: () => { S.graphMode = "pointer"; renderView(); } }, "Selected pointer"),
    h("button", { class: "btn small" + (mode === "calls" ? " primary" : ""),
      onclick: () => { S.graphMode = "calls"; renderView(); } }, "Call graph"),
    h("span", { text: mode === "pointer"
      ? "Solid blue: SVF points-to · dashed: syntactic hypothesis · red: address passed to a callee. Drag to arrange, scroll to zoom."
      : "Functions colored by their riskiest pointer; size grows with pointer count. Click a function to list its pointers." }));
  wrap.append(tools);
  let data;
  try {
    data = mode === "pointer" ? await api("neighborhood/" + S.selected) : callGraphData();
  } catch (e) { return fail(e); }
  drawGraph(wrap, data, mode);
}

function callGraphData() {
  const nodes = [], edges = [], byName = {};
  const rank = { escapes: 5, writes: 4, reassigned: 3, "read-only": 2, unused: 1 };
  for (const f of S.map.files) for (const fn of f.functions) {
    if (fn.name === "(file scope)") continue;
    const worst = fn.pointers.reduce((a, p) => (rank[p.class] > (rank[a] || 0) ? p.class : a), null);
    const id = `fn:${f.file}::${fn.name}`;
    nodes.push({ id, type: "function", label: fn.name + "()", class: worst || "unused", size: fn.pointers.length, file: f.file, fn });
    (byName[fn.name] ||= []).push(id);
  }
  for (const f of S.map.files) for (const fn of f.functions) for (const c of fn.calls || []) for (const t of byName[c] || [])
    edges.push({ from: `fn:${f.file}::${fn.name}`, to: t, type: "calls" });
  return { nodes, edges };
}

function drawGraph(wrap, data, mode) {
  const svg = sv("svg", { class: "graph", role: "img", "aria-label": mode === "pointer" ? "Pointer relationships" : "Call graph" });
  wrap.append(svg);
  const rect = svg.getBoundingClientRect();
  const W = rect.width || 900, H = rect.height || 600;
  const nodes = data.nodes.map((n) => ({ ...n }));
  const idx = new Map(nodes.map((n, i) => [n.id, i]));
  const edges = data.edges.filter((e) => idx.has(e.from) && idx.has(e.to));
  layout(nodes, edges, W, H, mode);

  const defs = sv("defs", {}, sv("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "10", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
    sv("path", { d: "M0 0L10 5L0 10z", fill: "var(--faint)" })));
  const g = sv("g", {});
  svg.append(defs, g);
  let view = { x: 0, y: 0, k: 1 };
  const apply = () => g.setAttribute("transform", `translate(${view.x},${view.y}) scale(${view.k})`);
  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const pt = svg.getBoundingClientRect();
    const mx = e.clientX - pt.left, my = e.clientY - pt.top;
    const k2 = Math.min(3, Math.max(0.25, view.k * (e.deltaY < 0 ? 1.12 : 0.89)));
    view.x = mx - ((mx - view.x) * k2) / view.k; view.y = my - ((my - view.y) * k2) / view.k; view.k = k2;
    apply();
  }, { passive: false });
  let pan = null;
  svg.addEventListener("pointerdown", (e) => { if (e.target === svg) { pan = { x: e.clientX - view.x, y: e.clientY - view.y }; svg.setPointerCapture(e.pointerId); } });
  svg.addEventListener("pointermove", (e) => { if (pan) { view.x = e.clientX - pan.x; view.y = e.clientY - pan.y; apply(); } });
  svg.addEventListener("pointerup", () => { pan = null; });

  const edgeEls = edges.map((e) => {
    const cls = ["edge", e.type, e.evidence || ""].join(" ");
    const line = sv("line", { class: cls, "marker-end": e.type === "declared-in" ? "" : "url(#arrow)" });
    line.append(sv("title", { text: `${e.type}${e.evidence ? " (" + e.evidence + ")" : ""}${e.line ? " at line " + e.line : ""}${e.via ? " — " + e.via : ""}` }));
    g.append(line);
    return line;
  });
  const nodeEls = nodes.map((n) => {
    const r = n.type === "function" ? 7 + Math.min(12, (n.size || 0) * 2) : n.main ? 13 : 10;
    const color = n.type === "object" ? "var(--panel-3)" : n.type === "function" && mode === "pointer" ? "var(--faint)" : `var(--c-${n.class || "unknown"})`;
    const shape = n.type === "object"
      ? sv("rect", { class: "shape", x: -r, y: -r, width: 2 * r, height: 2 * r, rx: 3, fill: color, stroke: n.source === "svf" || n.source === "both" ? "var(--accent)" : "var(--faint)" })
      : n.type === "function" && mode === "pointer"
        ? sv("rect", { class: "shape", x: -r - 4, y: -r + 2, width: 2 * r + 8, height: 2 * r - 4, rx: 8, fill: color })
        : sv("circle", { class: "shape", r, fill: color });
    const el = sv("g", { class: "node" + (n.main ? " focus" : ""), tabindex: 0 }, shape,
      sv("text", { x: r + 5, y: 4, text: n.label || n.id }),
      sv("title", { text: n.type === "object" ? `object ${n.label}${n.kind ? " (" + n.kind + ")" : ""} — ${n.source === "both" ? "SVF points-to and syntactic hypothesis agree" : n.source === "svf" ? "SVF points-to" : "syntactic hypothesis"}` : n.label }));
    let drag = null;
    el.addEventListener("pointerdown", (e) => { e.stopPropagation(); drag = { x: e.clientX, y: e.clientY, moved: false }; el.setPointerCapture(e.pointerId); });
    el.addEventListener("pointermove", (e) => {
      if (!drag) return;
      n.x += (e.clientX - drag.x) / view.k; n.y += (e.clientY - drag.y) / view.k;
      drag.x = e.clientX; drag.y = e.clientY; drag.moved = true; place();
    });
    el.addEventListener("pointerup", () => {
      if (drag && !drag.moved) onNodeClick(n);
      drag = null;
    });
    el.addEventListener("mouseenter", () => focusNode(n.id, true));
    el.addEventListener("mouseleave", () => focusNode(n.id, false));
    g.append(el);
    return el;
  });
  function focusNode(id, on) {
    const near = new Set([id]);
    edges.forEach((e) => { if (e.from === id) near.add(e.to); if (e.to === id) near.add(e.from); });
    nodeEls.forEach((el, i) => el.classList.toggle("dim", on && !near.has(nodes[i].id)));
    edgeEls.forEach((el, i) => {
      const hit = edges[i].from === id || edges[i].to === id;
      el.classList.toggle("dim", on && !hit); el.classList.toggle("hl", on && hit);
    });
  }
  function place() {
    nodes.forEach((n, i) => nodeEls[i].setAttribute("transform", `translate(${n.x},${n.y})`));
    edges.forEach((e, i) => {
      const a = nodes[idx.get(e.from)], b = nodes[idx.get(e.to)];
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1, rb = 14;
      edgeEls[i].setAttribute("x1", a.x); edgeEls[i].setAttribute("y1", a.y);
      edgeEls[i].setAttribute("x2", b.x - (dx / d) * rb); edgeEls[i].setAttribute("y2", b.y - (dy / d) * rb);
    });
  }
  function onNodeClick(n) {
    if (n.type === "pointer" && n.id.startsWith("P-")) select(n.id);
    else if (n.fn) { S.filters.q = n.fn.name; renderList(); toast(`Listing pointers of ${n.fn.name}()`); }
  }
  place();
  // fit
  const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y);
  if (nodes.length) {
    const minx = Math.min(...xs) - 60, maxx = Math.max(...xs) + 160, miny = Math.min(...ys) - 40, maxy = Math.max(...ys) + 40;
    view.k = Math.min(1.6, Math.min(W / (maxx - minx), H / (maxy - miny)));
    view.x = (W - (maxx + minx) * view.k) / 2; view.y = (H - (maxy + miny) * view.k) / 2;
  }
  apply();
  if (!nodes.length) svg.append(sv("text", { x: 20, y: 30, fill: "var(--muted)", text: "Nothing to show." }));
}

function layout(nodes, edges, W, H, mode) {
  const n = nodes.length;
  const idx = new Map(nodes.map((nd, i) => [nd.id, i]));
  nodes.forEach((nd, i) => {
    const a = (2 * Math.PI * i) / Math.max(n, 1);
    const r = nd.main ? 0 : Math.min(W, H) * 0.3;
    nd.x = W / 2 + r * Math.cos(a) + (Math.random() - 0.5) * 10;
    nd.y = H / 2 + r * Math.sin(a) + (Math.random() - 0.5) * 10;
    nd.vx = 0; nd.vy = 0;
  });
  const L = mode === "pointer" ? 130 : 90, iters = Math.min(600, 120 + n * 4);
  for (let it = 0; it < iters; it++) {
    const t = 1 - it / iters;
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const a = nodes[i], b = nodes[j];
      let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 0.01;
      const f = 2600 / d2;
      const d = Math.sqrt(d2);
      dx /= d; dy /= d;
      a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
    }
    for (const e of edges) {
      const a = nodes[idx.get(e.from)], b = nodes[idx.get(e.to)];
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
      const f = (d - L) * 0.02;
      a.vx += (dx / d) * f; a.vy += (dy / d) * f; b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
    }
    for (const nd of nodes) {
      nd.vx += (W / 2 - nd.x) * 0.002; nd.vy += (H / 2 - nd.y) * 0.002;
      if (nd.main) { nd.vx *= 0.2; nd.vy *= 0.2; }
      nd.x += Math.max(-30, Math.min(30, nd.vx)) * t; nd.y += Math.max(-30, Math.min(30, nd.vy)) * t;
      nd.vx *= 0.55; nd.vy *= 0.55;
    }
  }
}

// ---------------------------------------------------------------- source view
async function renderSource(v) {
  const files = S.source ? S.source.files : [...new Set(S.pointers.map((p) => p.file))].sort();
  if (!S.sourceFile) S.sourceFile = (S.detail && S.detail.finding.file) || files[0];
  const sel = h("select", { "aria-label": "File", onchange: (e) => { S.sourceFile = e.target.value; S.focusLine = null; renderView(); } },
    files.map((f) => h("option", { value: f, selected: f === S.sourceFile, text: f })));
  const head = h("div", { class: "src-head" }, sel,
    h("span", { class: "legend" },
      h("span", {}, h("span", { class: "mk read", text: "p" }), "read"),
      h("span", {}, h("span", { class: "mk write", text: "p" }), "write through"),
      h("span", {}, h("span", { class: "mk escape", text: "p" }), "escapes"),
      h("span", {}, h("span", { class: "mk declaration", text: "p" }), "declaration"),
      h("span", { title: "compiled by no analysed configuration" }, h("span", { style: { display: "inline-block", width: "18px", height: "10px",
        background: "repeating-linear-gradient(-45deg, transparent 0 4px, color-mix(in srgb, var(--faint) 30%, transparent) 4px 6px)" } }), "unexamined")));
  v.append(head);
  if (!S.sourceFile) return;
  let src;
  try { src = await api("source?file=" + encodeURIComponent(S.sourceFile)); } catch (e) { return fail(e); }
  S.source = src;
  const unexamined = new Set();
  for (const [a, b] of src.unexamined) for (let i = a; i <= b; i++) unexamined.add(i);
  const changed = new Set();
  if (S.impact && S.impact.changed_files[src.file]) for (const hk of S.impact.changed_files[src.file]) for (let i = hk.new[0]; i <= hk.new[1]; i++) changed.add(i);
  const byLine = {};
  for (const m of src.marks) (byLine[m.line] ||= []).push(m);
  const code = h("div", { class: "code" });
  src.lines.forEach((text, i) => {
    const ln = i + 1;
    const marks = (byLine[ln] || []).filter((m) => m.col).sort((a, b) => a.col - b.col);
    const line = h("div", { class: "ln" + (unexamined.has(ln) ? " unexamined" : "") + (S.focusLine === ln ? " focus" : "") + (changed.has(ln) ? " changed" : ""), id: `L${ln}` },
      h("span", { class: "no", text: ln }),
      h("span", { class: "gut" }, marks.length ? h("i", { style: { "--c": `var(--c-${marks[0].class})` } }) : null));
    const body = h("span", {});
    let pos = 0;
    for (const m of marks) {
      const start = m.col - 1;
      if (start < pos) continue;
      body.append(text.slice(pos, start));
      body.append(h("span", { class: `mk ${m.role}${S.selected === m.finding ? " sel" : ""}`,
        title: `${m.name}: ${m.role === "declaration" ? "declaration" : m.text || m.role} — ${CLASS_LABEL[m.class] || m.class}`,
        onclick: () => select(m.finding, { keepView: true }) }, text.slice(start, start + m.len)));
      pos = start + m.len;
    }
    body.append(text.slice(pos));
    line.append(body);
    code.append(line);
  });
  v.append(code);
  if (S.focusLine) requestAnimationFrame(() => { const el = document.getElementById(`L${S.focusLine}`); if (el) el.scrollIntoView({ block: "center" }); });
}

function gotoLine(file, line) {
  S.sourceFile = file; S.focusLine = line; S.view = "source";
  renderTabs(); renderView();
}

// ---------------------------------------------------------------- details panel
async function select(id, opts = {}) {
  S.selected = id;
  document.querySelectorAll(".prow").forEach((el) => el.classList.remove("sel"));
  renderListBody();
  const row = document.querySelector(".prow.sel");
  if (row) row.scrollIntoView({ block: "nearest" });
  try {
    S.detail = await api("pointer/" + id);
  } catch (e) { return fail(e); }
  renderDetail();
  const ws = document.getElementById("ws");
  if (ws) ws.classList.add("show-details");
  if (S.view === "graph") renderView();
  else if (S.view === "map" && !opts.keepView) document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("sel", c.title.startsWith(S.detail.finding.name + ":")));
  else if (S.view === "source" && !opts.keepView) { S.sourceFile = S.detail.finding.file; S.focusLine = S.detail.finding.line; renderView(); }
}

function icon(status) {
  return status === "established" ? h("span", { class: "icon-ok", title: "established", text: "✓" })
    : status === "violated" ? h("span", { class: "icon-bad", title: "violated", text: "✗" })
    : h("span", { class: "icon-unk", title: "unresolved", text: "?" });
}

function renderDetail() {
  const right = document.getElementById("right");
  if (!right) return;
  clear(right);
  const box = h("div", { class: "details" });
  right.append(box);
  const d = S.detail;
  if (!d || d.finding.id !== S.selected) {
    box.append(h("div", { class: "empty" },
      h("p", { text: "Select a pointer in the list, the map, the graph or the source view." }),
      h("p", { text: "Each pointer is colored by what it does to its target. A violet ring marks pointers with an eligible refactor." })));
    return;
  }
  const f = d.finding;
  box.append(
    h("div", { class: "d-title" }, h("h2", { text: f.name || "(unnamed)" }),
      h("span", { class: `badge cls-${d.class}`, title: CLASS_HELP[d.class] }, h("span", { class: "dot" }), CLASS_LABEL[d.class] || d.class),
      h("button", { class: "btn small ghost close-details", "aria-label": "Close details",
        onclick: () => document.getElementById("ws").classList.remove("show-details") }, "✕")),
    h("div", { class: "d-sub" },
      `${f.kind} · `, h("code", { text: f.type || "" }), ` · `,
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); gotoLine(f.file, f.line); } }, `${f.file}:${f.line}`),
      f.function ? ` · in ${f.function}()` : "", ` · evidence ${f.evidence_status}`,
      f.typedef_hidden ? " · hidden behind a typedef" : ""),
  );

  // recipes
  const recs = Object.entries(d.recipes);
  const rsec = h("div", { class: "section" }, h("h4", { text: "Refactoring recipes" }));
  if (!recs.length) rsec.append(h("div", { class: "d-sub", text: "No recipe in this milestone applies to this kind of pointer yet." }));
  for (const [rid, r] of recs) {
    const list = h("ul", { class: "pre-list" }, r.preconditions.map((p) => h("li", {}, icon(p.status),
      h("details", { open: p.status !== "established" },
        h("summary", { text: p.description }),
        h("div", { class: "ev" }, p.evidence.map((e) => h("div", { text: e })),
          p.resolve_by ? h("div", { style: { color: "var(--accent)" }, text: "→ " + p.resolve_by }) : null)))));
    rsec.append(h("div", { class: "recipe" },
      h("div", { class: "recipe-head" }, h("b", { text: rid }),
        r.eligible ? h("span", { class: "badge", style: { color: "var(--c-eligible)" }, text: `✦ eligible · ${r.edits} edit(s)` })
          : h("span", { class: "badge", style: { color: "var(--muted)" }, text: "blocked" })),
      list,
      h("div", { class: "recipe-actions" },
        h("button", { class: "btn small" + (r.eligible ? " primary" : ""), onclick: () => propose(f.id, rid),
          title: r.eligible ? "Open a transaction and preview the patch" : "Record the rejection in the ledger" },
          r.eligible ? "Propose…" : "Record as blocked"),
        h("button", { class: "btn small", onclick: () => explain(f.id), title: "Ask the LLM planner to explain (never to edit)" }, "Explain"))));
  }
  box.append(rsec);

  // uses
  const usec = h("div", { class: "section" }, h("h4", { text: `Uses (${d.uses.length})` }));
  const ul = h("ul", { class: "uses" });
  for (const u of d.uses) ul.append(h("li", { onclick: () => gotoLine(f.file, u.line), title: "Show in source" },
    h("span", { class: "l", text: u.line }), h("div", {}, h("div", { text: u.text + (u.in_macro ? " (in a macro)" : "") }), u.source ? h("code", { text: u.source }) : null)));
  if (!d.uses.length) ul.append(h("li", {}, h("span", {}), h("div", { text: "no uses in the analysed configurations" })));
  usec.append(ul);
  box.append(usec);

  // targets
  const tsec = h("div", { class: "section" }, h("h4", { text: "What it may point to" }));
  if (f.kind === "parameter") {
    const args = d.call_args || [];
    tsec.append(h("div", { class: "d-sub", text: "Syntactic hypotheses (what each analysed call passes):" }),
      h("div", { class: "targets" }, args.length
        ? args.map((a) => h("button", { class: "tchip", title: `${a.caller}() at ${a.file}:${a.line} — show in source`,
            onclick: () => gotoLine(a.file, a.line) }, a.null ? "NULL" : a.object ? "&" + a.object : a.text || "?",
            h("span", { style: { color: "var(--faint)" }, text: ` · ${a.caller}():${a.line}` })))
        : h("span", { class: "d-sub", text: "no analysed call passes this parameter" })));
  } else {
    const ast = (f.possible_targets || []).map((t) => (t.object && t.object.name) || t.name || t.callee || t.source);
    tsec.append(h("div", { class: "d-sub", text: "Syntactic hypotheses (initializers and assignments):" }),
      h("div", { class: "targets" }, ast.length ? ast.map((t) => h("span", { class: "tchip", text: t })) : h("span", { class: "d-sub", text: "none recorded" })));
  }
  for (const s of d.svf_targets) {
    tsec.append(h("div", { class: "d-sub", text: `SVF points-to (${s.profile}${s.complete ? "" : ", incomplete"}):` }),
      h("div", { class: "targets" }, s.targets.length ? s.targets.map((o) => h("span", { class: "tchip svf", title: `${o.kind || "object"} ${o.file ? "at " + o.file + ":" + o.line : ""}`, text: o.name || `#${o.id}` }))
        : h("span", { class: "d-sub", text: "points to nothing in the analysed program" })));
  }
  if (!d.svf_targets.length) tsec.append(h("div", { class: "d-sub", text: "No points-to evidence yet (run “Points-to”)." }));
  box.append(tsec);

  if (d.callers && d.callers.length) {
    box.append(h("div", { class: "section" }, h("h4", { text: "Callers" }), h("div", { class: "targets" }, d.callers.map((c) => h("span", { class: "tchip", text: c.split("::").pop() + "()" })))));
  }

  // contracts
  const csec = h("div", { class: "section" }, h("h4", { text: "Contracts" }));
  for (const c of d.contracts) csec.append(h("div", { class: "d-sub", text: `${c.id}: must stay ${c.expect.join(", ")}${c.reason ? " — " + c.reason : ""}` }));
  const exps = [["read-only", "read-only"], ["no-escape", "doesn't escape"], ["no-identity", "not compared"], ["no-reassign", "not reassigned"], ["borrowed", "borrowed (never written or kept)"]];
  const checks = exps.map(([k, label]) => h("label", { style: { marginRight: "10px", fontSize: "12.5px" } }, h("input", { type: "checkbox", value: k }), " " + label));
  const reason = h("input", { type: "text", placeholder: "why this must hold (optional)", style: { width: "100%", marginTop: "6px", border: "1px solid var(--border)", borderRadius: "6px", padding: "4px 6px", background: "var(--code-bg)" } });
  csec.append(h("div", { class: "d-sub", text: "Pin today's behavior; “Changes” will flag any edit that breaks it." }), h("div", {}, checks), reason,
    h("button", { class: "btn small", style: { marginTop: "6px" }, onclick: async () => {
      const expect = checks.map((c) => c.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
      if (!expect.length) return toast("Choose at least one expectation");
      try { const c = await api("contracts", { finding: f.id, expect, reason: reason.value }); toast(`Pinned ${c.id}`); select(f.id, { keepView: true }); } catch (e) { fail(e); }
    } }, "Pin contract"));
  box.append(csec);

  // excerpt
  box.append(h("div", { class: "section" }, h("h4", { text: "Source" }),
    h("div", { class: "excerpt" }, h("pre", { text: d.excerpt.lines.join("\n") }))));
}

// ---------------------------------------------------------------- transactions
async function propose(fid, recipe) {
  try {
    const t = await api("propose", { finding: fid, recipe });
    await refreshState(); renderTabs();
    openTxn(t);
  } catch (e) { fail(e); }
}

function diffView(text) {
  const pre = h("pre", { class: "diff" });
  for (const line of (text || "").split("\n")) {
    const cls = line.startsWith("+++") || line.startsWith("---") ? "" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "";
    pre.append(h("span", { class: cls, text: line + "\n" }));
  }
  return pre;
}

function modal(title, body, actions) {
  const root = clear(document.getElementById("modal-root"));
  const close = () => clear(root);
  const back = h("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(); } },
    h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title },
      h("header", {}, h("h2", { text: title }), h("button", { class: "btn ghost", onclick: close, "aria-label": "Close" }, "✕")),
      h("div", { class: "body" }, body),
      h("footer", {}, actions(close))));
  root.append(back);
  document.addEventListener("keydown", function esc(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); } });
}

function openTxn(t) {
  const c = t.candidate;
  const body = [];
  body.push(h("div", {}, h("span", { class: `state ${t.state}`, text: t.state.toUpperCase() }),
    `  ${c.recipe} v${c.recipe_version} on '${t.finding.name}' in ${t.finding.function || t.finding.file}() — ${t.finding.file}:${t.finding.line}`));
  const v = t.validation;
  if (v && v.records) {
    const policy = (v.judgement && v.judgement.policy) || [];
    body.push(h("div", {}, h("h4", { text: "Validation" }),
      h("div", { class: "records" }, v.records.map((r) => h("div", {}, h("span", { class: r.outcome, text: r.outcome }), h("span", { text: r.kind }), h("span", { text: `${r.name}: ${r.detail}` })))),
      policy.length ? h("div", { class: "d-sub", text: `Acceptance policy requires: ${policy.join(", ")}` + (policy.some((k) => k.includes("test")) ? "" : " (tests are not required)") }) : null,
      v.judgement && v.judgement.reasons.length ? h("div", { class: "d-sub", text: "Judgement: " + v.judgement.reasons.join("; ") }) : null,
      v.strength === "compile-only" ? h("div", { class: "banner" }, h("b", { text: "Compile-only. " }),
        "The patch builds and its AST re-checks, but no test or differential run executed it. ",
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); openSettings(); } }, "Configure tests")) : null,
      h("div", { class: "d-sub", text: "Limits: tests and differential runs cover only exercised inputs; the mechanical re-check covers only analysed configurations. No universal proof is claimed." })));
  } else if (v && v.error) body.push(h("div", { class: "d-sub", style: { color: "var(--bad)" }, text: v.error }));
  if (t.patch) body.push(h("div", {}, h("h4", { text: `Patch (${c.edits.length} edit(s) in ${new Set(c.edits.map((e) => e.file)).size} file(s))` }), diffView(t.patch.diff)));
  body.push(h("div", {}, h("h4", { text: "Why behavior is preserved" }), h("div", { class: "d-sub", text: c.preservation_argument })));
  const ok = c.preconditions.filter((p) => p.status === "established").length;
  body.push(h("details", { class: "group", open: ok !== c.preconditions.length },
    h("summary", { text: `Preconditions (${ok} of ${c.preconditions.length} established)` }),
    h("ul", { class: "pre-list", style: { padding: 0 } }, c.preconditions.map((p) => h("li", {}, icon(p.status),
      h("details", { open: p.status !== "established" }, h("summary", { text: `${p.id}: ${p.description}` }),
        h("div", { class: "ev" }, p.evidence.map((e) => h("div", { text: e })))))))));
  modal(`Transaction ${t.id}`, body, (close) => {
    const acts = [];
    if (["proposed", "validated", "provisional", "rejected"].includes(t.state))
      acts.push(h("button", { class: "btn" + (t.state === "proposed" ? " primary" : ""), onclick: () => { close(); validateTxn(t.id); } }, "Validate"));
    if (t.state === "validated" || t.state === "provisional")
      acts.push(h("button", { class: "btn primary", onclick: () => { close(); applyTxn(t.id, "accept"); } }, "Accept & apply"));
    if (["proposed", "validated", "provisional", "rejected", "blocked"].includes(t.state))
      acts.push(h("button", { class: "btn", onclick: async () => { try { await api(`txn/${t.id}/skip`, {}); close(); toast("Skipped"); await refreshState(); renderView(); } catch (e) { fail(e); } } }, "Skip"));
    if (t.state === "accepted")
      acts.push(h("button", { class: "btn danger", onclick: () => { close(); applyTxn(t.id, "revert"); } }, "Revert"));
    acts.push(h("button", { class: "btn ghost", onclick: close }, "Close"));
    return acts;
  });
}

async function validateTxn(id) {
  try {
    const job = await api(`txn/${id}/validate`, {});
    followJob(job, async () => { await refreshState(); openTxn(await api("ledger/" + id)); if (S.view === "ledger") renderView(); });
  } catch (e) { fail(e); }
}

async function applyTxn(id, action) {
  try {
    const job = await api(`txn/${id}/${action}`, {});
    followJob(job, async () => { toast(`${id} ${action === "accept" ? "accepted" : "reverted"}; analysis refreshed`); S.source = null; await refreshState(); await loadData(); });
  } catch (e) { fail(e); }
}

async function explain(fid) {
  try {
    const job = await api("explain", { finding: fid });
    followJob(job, (res) => modal("Planner explanation (advisory)", [h("pre", { class: "diff", style: { whiteSpace: "pre-wrap" }, text: (res && res.text) || "" })], (close) => [h("button", { class: "btn", onclick: close }, "Close")]));
  } catch (e) { fail(e); }
}

// ---------------------------------------------------------------- ledger view
async function renderLedger(v) {
  let rows;
  try { rows = await api("ledger"); } catch (e) { return fail(e); }
  S.ledger = rows;
  const val = S.state && S.state.validation;
  if (val && val.level !== "behavioural") {
    v.append(h("div", { class: "banner" }, h("b", { text: val.level === "compile-only" ? "Validation is compile-only. " : "Tests are not required. " }),
      (val.notes || []).join(" ") + " ", h("a", { href: "#", onclick: (e) => { e.preventDefault(); openSettings(); } }, "Open settings")));
  }
  if (!rows.length) {
    v.append(h("div", { class: "pad d-sub", text: "No transactions yet. Select an eligible pointer and choose “Propose…”." }));
    return;
  }
  v.append(h("table", { class: "table" },
    h("thead", {}, h("tr", {}, ["Transaction", "State", "Validated by", "Recipe", "Pointer", "Where", "Created"].map((t) => h("th", { text: t })))),
    h("tbody", {}, rows.slice().reverse().map((t) => h("tr", { class: "click", onclick: async () => { try { openTxn(await api("ledger/" + t.id)); } catch (e) { fail(e); } } },
      h("td", { class: "mono", text: t.id }), h("td", {}, h("span", { class: `state ${t.state}`, text: t.state })),
      h("td", {}, t.strength ? h("span", { class: "tag" + (t.strength === "compile-only" ? " weak" : ""), text: t.strength === "compile-only" ? "compile only" : "tests" }) : ""),
      h("td", { text: t.recipe }), h("td", { class: "mono", text: t.finding.name }),
      h("td", { class: "mono", text: `${t.finding.file}:${t.finding.line} ${t.finding.function ? t.finding.function + "()" : ""}` }),
      h("td", { text: (t.created_at || "").replace("T", " ").replace("+00:00", "Z") }))))));
}

// ---------------------------------------------------------------- changes view
async function renderChanges(v) {
  const snaps = (S.state && S.state.snapshots) || [];
  const name = h("input", { type: "text", placeholder: "baseline name (optional)", style: { border: "1px solid var(--border)", borderRadius: "8px", padding: "5px 8px", background: "var(--code-bg)" } });
  const gitRev = h("input", { type: "text", placeholder: "or a git revision, e.g. HEAD~1", style: { border: "1px solid var(--border)", borderRadius: "8px", padding: "5px 8px", background: "var(--code-bg)" } });
  const since = h("select", { style: { border: "1px solid var(--border)", borderRadius: "8px", padding: "5px 8px", background: "var(--code-bg)" } },
    snaps.slice().reverse().map((s) => h("option", { value: s.name, text: `${s.name} — ${s.created_at.replace("T", " ").slice(0, 16)} (${s.findings} pointers)` })));
  const reval = h("input", { type: "checkbox", id: "reval" });
  v.append(h("div", { class: "pad" },
    h("div", { class: "cards" },
      h("div", { class: "card" }, h("h2", { text: "1 · Save a baseline" }),
        h("p", { class: "d-sub", text: "A snapshot freezes today's pointer facts, recipe verdicts and source tree. Take one before others change the code." }),
        h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } }, name,
          h("button", { class: "btn", onclick: async () => { try { const m = await api("snapshots", { name: name.value.trim() || null }); toast(`Saved ${m.name}`); await refreshState(); renderView(); } catch (e) { fail(e); } } }, "Save working tree")),
        h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap", marginTop: "8px" } }, gitRev,
          h("button", { class: "btn", onclick: async () => { if (!gitRev.value.trim()) return; try { const job = await api("snapshots", { git: gitRev.value.trim() }); followJob(job, async () => { await refreshState(); renderView(); }); } catch (e) { fail(e); } } }, "Analyse revision"))),
      h("div", { class: "card" }, h("h2", { text: "2 · Explain what changed" }),
        h("p", { class: "d-sub", text: "Compare the current analysis with a snapshot: every pointer whose behavior changed, why, and which edited lines caused it." }),
        snaps.length ? since : h("div", { class: "d-sub", text: "No snapshots yet." }),
        h("div", { style: { margin: "8px 0" } }, h("label", { for: "reval", style: { fontSize: "12.5px" } }, reval, " also rebuild and compare behavior (tests and differential runs)")),
        h("button", { class: "btn primary", disabled: !snaps.length, onclick: async () => {
          try { const job = await api("impact", { since: since.value, revalidate: reval.checked }); followJob(job, (rep) => { S.impact = rep; renderView(); }); } catch (e) { fail(e); }
        } }, "Compare"))),
    S.impact ? impactReport(S.impact) : null));
}

function impactReport(rep) {
  const box = h("div", { class: "report" });
  box.append(h("h3", {}, "Risk: ", h("span", { class: `risk ${rep.risk}`, text: rep.risk.toUpperCase() }),
    h("span", { class: "d-sub", style: { marginLeft: "10px" }, text: `since ${rep.base.name} · ${rep.summary.changed_files} changed file(s) · ${rep.summary.high} high · ${rep.summary.review} review · ${rep.summary.contracts_violated} contract(s) violated` })));
  if (rep.validation && rep.validation.level === "compile-only") box.append(h("div", { class: "banner" }, h("b", { text: "No tests configured. " }),
    "This report rests on pointer facts and contracts; revalidation can rebuild but has nothing to run."));
  if (rep.stale_inventory && rep.stale_inventory.length) box.append(h("div", { class: "d-sub", style: { color: "var(--warn)" }, text: `The analysis is older than ${rep.stale_inventory.length} file(s); compile again for an accurate comparison.` }));
  const rank = { high: 0, review: 1, info: 2 };
  const changes = rep.changes.slice().sort((a, b) => rank[a.severity] - rank[b.severity]);
  box.append(h("h3", { text: `Pointer behavior (${changes.length})` }));
  if (!changes.length) box.append(h("div", { class: "d-sub", text: "No pointer fact changed." }));
  for (const c of changes) {
    box.append(h("div", { class: "change" },
      h("div", {}, h("div", { class: `sev ${c.severity}`, text: c.severity }), h("div", { class: "who", text: c.aspect })),
      h("div", {},
        h("div", {}, h("a", { href: "#", onclick: (e) => { e.preventDefault(); if (c.finding && S.pointers.some((p) => p.id === c.finding)) select(c.finding); if (c.line) gotoLine(c.file, c.line); } }, `${c.name}`),
          ` in ${c.function ? c.function + "()" : c.file} — ${c.text}`, c.caused_by_change ? h("span", { class: "tag", style: { marginLeft: "6px" }, text: "edited line" }) : null),
        c.source ? h("code", { text: c.source }) : null)));
  }
  if (rep.contracts.length) {
    box.append(h("h3", { text: "Contracts" }));
    for (const c of rep.contracts) box.append(h("div", { class: "change" },
      h("div", {}, h("div", { class: `sev ${c.status === "violated" ? "high" : c.status === "held" ? "info" : "review"}`, text: c.status })),
      h("div", { text: `${c.contract}: ${c.text}` })));
  }
  if (rep.transactions.length) {
    box.append(h("h3", { text: "Accepted transactions touched later" }));
    for (const t of rep.transactions) box.append(h("div", { class: "change" }, h("div", { class: `sev ${t.severity}`, text: t.severity }), h("div", { text: t.text })));
  }
  const files = Object.entries(rep.changed_files);
  if (files.length) {
    box.append(h("h3", { text: "Edited lines" }));
    for (const [f, hunks] of files) for (const hk of hunks) {
      const txt = [`@@ ${f} −${hk.old[0]},${hk.old[1] - hk.old[0] + 1} +${hk.new[0]},${hk.new[1] - hk.new[0] + 1} @@`]
        .concat(hk.before.map((l) => "-" + l), hk.after.map((l) => "+" + l)).join("\n");
      box.append(diffView(txt));
    }
  }
  if (rep.revalidation) {
    box.append(h("h3", { text: "Behavior on the configured tests" }));
    for (const r of rep.revalidation) {
      box.append(h("div", { class: "change" }, h("div", { class: `sev ${r.outcome === "failed" ? "high" : "info"}`, text: r.outcome }), h("div", {}, `${r.name}: ${r.detail}`, r.diff ? diffView(r.diff) : null)));
    }
  }
  return box;
}

// ---------------------------------------------------------------- jobs
async function compile() {
  try { followJob(await api("compile", {}), async () => { S.source = null; await refreshState(); await loadData(); renderTabs(); }); }
  catch (e) { fail(e); }
}

async function runFlow() {
  try { followJob(await api("flow", {}), async () => { await refreshState(); await loadData(); }); }
  catch (e) { fail(e); }
}

function followJob(job, onDone) {
  const panel = document.getElementById("jobs");
  panel.classList.add("open");
  panel.classList.remove("collapsed");
  clear(panel);
  const status = h("span", { class: "spinner" });
  const title = h("b", { text: job.title });
  const state = h("span", { class: "d-sub", text: "running" });
  const log = h("pre", {});
  const toggle = h("button", { class: "btn small ghost", onclick: () => {
    panel.classList.toggle("collapsed");
    toggle.textContent = panel.classList.contains("collapsed") ? "Show log" : "Hide log";
  } }, "Hide log");
  panel.append(h("header", {}, status, title, state, h("span", { style: { flex: 1 } }), toggle,
    h("button", { class: "btn small ghost", "aria-label": "Dismiss", onclick: () => panel.classList.remove("open") }, "✕")), log);
  let since = 0;
  renderTop();
  const tick = async () => {
    let j;
    try { j = await api(`jobs/${job.id}?since=${since}`); } catch (e) { return fail(e); }
    if (j.log.length) { log.append(j.log.join("\n") + "\n"); log.scrollTop = log.scrollHeight; since = j.log_size; }
    if (j.state === "running") return setTimeout(tick, 600);
    status.className = "dot";
    status.style.color = j.state === "done" ? "var(--ok)" : "var(--bad)";
    const secs = j.finished && j.started ? ` in ${(j.finished - j.started).toFixed(1)} s` : "";
    state.textContent = j.state === "done" ? "done" + secs : "failed";
    if (j.state !== "done") state.after(h("span", { class: "err", title: j.error || "", text: j.error || "" }));
    if (j.state === "done") {
      toast(`${j.title}: done`);
      // keep the outcome visible but give the space back to the workspace
      setTimeout(() => { if (panel.contains(log)) { panel.classList.add("collapsed"); toggle.textContent = "Show log"; } }, 1800);
      if (onDone) await onDone(j.result);
    } else toast(`${j.title} failed: ${j.error}`, 8000);
    await refreshState();
  };
  tick();
}

// ---------------------------------------------------------------- snapshot mode
function snapData() { return SNAP.datasets[S.dataset]; }

function snapApi(path, body) {
  const d = snapData();
  if (body !== undefined) {
    // a pointer that has a recorded transaction opens it; everything else would change the project
    const txn = path === "propose" && d.proposals[body.finding];
    if (txn && d.responses["ledger/" + txn]) return Promise.resolve(structuredClone(d.responses["ledger/" + txn]));
    const e = new Error("This snapshot is read-only.");
    e.snapshot = path;
    return Promise.reject(e);
  }
  const key = decodeURIComponent(path);
  if (key in d.responses) return Promise.resolve(structuredClone(d.responses[key]));
  return Promise.reject(new Error(`Not recorded in this snapshot: ${key}`));
}

const SNAP_ACTIONS = {
  compile: ["Compile & analyse", "rebuilds the project through recording compiler shims, collects compiler evidence, checks the secondary frontend's fidelity, rebuilds the pointer inventory and runs points-to analysis.", "weaver refresh --capture"],
  flow: ["Points-to", "runs SVF and GCC's IPA points-to analysis over each linked program.", "weaver flow"],
  propose: ["Propose", "evaluates the recipe again, opens a transaction in the ledger and previews the patch.", "weaver propose P-…"],
  validate: ["Validate", "applies the patch in an isolated copy, rebuilds both trees with the production compiler, re-checks the patched AST and runs the configured tests on both.", "weaver validate T-…"],
  accept: ["Accept", "applies a validated patch to the working tree under the acceptance policy and re-analyses.", "weaver accept T-…"],
  revert: ["Revert", "undoes an accepted transaction.", "weaver revert T-…"],
  skip: ["Skip", "closes a transaction without applying it.", "weaver skip T-…"],
  settings: ["Save settings", "rewrites weaver.yaml with the validation commands, acceptance policy, concurrency declaration and flow backend.", "weaver tests --add …   (or edit weaver.yaml)"],
  contracts: ["Pin contract", "records what must stay true of this pointer; later edits that break it are flagged in Changes.", "weaver contract pin P-… --expect read-only"],
  explain: ["Explain", "sends the pointer's evidence to the configured LLM planner for an advisory explanation (it never edits code).", "weaver explain P-… --dry-run"],
  snapshots: ["Save a baseline", "freezes today's pointer facts, recipe verdicts and source tree.", "weaver snapshot save --name baseline"],
  impact: ["Compare", "explains how pointer behaviour changed since a baseline. The report on this page was recorded when the page was made.", "weaver impact --since baseline"],
};

function readOnly(path) {
  const key = path.startsWith("txn/") ? path.split("/")[2] : path.split("/")[0];
  const [label, what, cmd] = SNAP_ACTIONS[key] || [path, "changes the project.", "weaver serve"];
  modal(`${label} runs on your machine`, [
    h("p", {}, h("b", { text: label }), " " + what),
    h("p", { class: "d-sub", text: "This page is a recorded, read-only copy of the web interface, so it cannot run anything. In a local Weaver the same button works, or from the command line:" }),
    copyable(cmd),
    ...localSteps(),
  ], (close) => [h("button", { class: "btn", onclick: close }, "Close")]);
}

function copyable(text) {
  const pre = h("pre", { class: "diff copy", text });
  const btn = h("button", { class: "btn small", onclick: async () => {
    try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; }
    catch (e) {
      const r = document.createRange(); r.selectNodeContents(pre);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
      btn.textContent = "Selected: press Ctrl+C";
    }
  } }, "Copy");
  return h("div", { class: "copy-box" }, pre, btn);
}

function localSteps() {
  const clone = SNAP.repo ? `git clone${SNAP.branch ? " --branch " + SNAP.branch : ""} ${SNAP.repo} weaver\n` : "";
  return [
    h("h4", { text: "Run Weaver on your machine" }),
    h("p", { class: "d-sub", text: "Needs Python 3.10 or later and GCC or Clang. The optional [flow] extra adds SVF points-to analysis (AGPL, run as a separate process)." }),
    copyable(`${clone}cd weaver\npython -m pip install -e '.[flow]'\ncp -r tests/fixtures/demo ~/weaver-demo\nweaver serve --open`),
    h("p", { class: "d-sub", text: "In the browser choose “Set up a new project”: directory ~/weaver-demo, build command make -B CC={cc}, compiler gcc or clang. For your own code, give its directory and a full-rebuild command with {cc} where the compiler goes." }),
    SNAP.web ? h("p", { class: "d-sub" }, "Source: ", h("a", { href: SNAP.web, target: "_blank", rel: "noopener", text: SNAP.web })) : null,
  ];
}

function runLocally() {
  const d = snapData();
  const inv = S.state && S.state.inventory;
  modal("Run Weaver on your code", [
    h("p", {}, "You are browsing ", h("b", { text: d.label }), ", recorded with ", h("code", { text: "weaver export-ui" }),
      (inv ? `: ${inv.summary.findings} pointer(s) in the analysed program` : "") +
      (d.scope && d.scope.length ? `, those under ${d.scope.join(", ")} shown` : "") + ". " + (d.description || "")),
    ...localSteps(),
  ], (close) => [h("button", { class: "btn", onclick: close }, "Close")]);
}

function switchDataset(i) {
  Object.assign(S, { dataset: i, selected: null, detail: null, source: null, sourceFile: null, focusLine: null,
    impact: null, view: "map", graphMode: "auto", map: null, pointers: [], removed: [] });
  S.filters = { classes: new Set(), eligible: false, q: "", kinds: new Set() };
  try { localStorage.setItem("weaver-snapshot-dataset", snapData().id); } catch (e) { /* a convenience only */ }
  boot();
}

if (SNAP) {
  // a bare #id in the link picks the dataset; otherwise the one this viewer chose last
  let want = location.hash.slice(1);
  try { want = want || localStorage.getItem("weaver-snapshot-dataset") || ""; } catch (e) { /* no storage */ }
  const i = SNAP.datasets.findIndex((d) => d.id === want);
  if (i >= 0) S.dataset = i;
}

boot();
