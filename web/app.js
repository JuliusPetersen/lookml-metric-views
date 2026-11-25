"use strict";

const STATUSES = ["exact", "adapted", "review", "manual", "skipped"];
const COLORS = { exact: "var(--exact)", adapted: "var(--adapted)", review: "var(--review)", manual: "var(--manual)", skipped: "var(--skipped)" };
const LABELS = { exact: "Exact", adapted: "Adapted", review: "Review", manual: "Manual", skipped: "Skipped" };
const MEANING = {
  exact: "same meaning",
  adapted: "same meaning, shown differently",
  review: "numbers can differ from Looker",
  manual: "needs a person",
  skipped: "hidden in Looker",
};

const $ = (id) => document.getElementById(id);
let pyodide = null;
let current = null; // { files: {path: text}, report, explores, source }
let activeExplore = 0;
let activeFilter = "all";

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

function setStatus(text, isError = false) {
  const s = $("status");
  s.textContent = text;
  s.classList.toggle("error", isError);
}

// ---- Python runtime -----------------------------------------------------------------------
async function loadPython(dialect) {
  if (!pyodide) {
    setStatus("Loading Python in your browser (first run takes a few seconds)…");
    if (!window.loadPyodide) {
      await new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = "https://cdn.jsdelivr.net/pyodide/v0.27.2/full/pyodide.js";
        s.onload = resolve;
        s.onerror = () => reject(new Error("Could not load Pyodide from cdn.jsdelivr.net."));
        document.head.append(s);
      });
    }
    pyodide = await window.loadPyodide();
    await pyodide.loadPackage(["pyyaml", "micropip"]);
    const micropip = pyodide.pyimport("micropip");
    await micropip.install("lkml==1.3.7");
    const manifest = await (await fetch("py/manifest.json")).json();
    pyodide.FS.mkdirTree("/app/lookml_metric_views");
    for (const name of manifest.files) {
      const code = await (await fetch(`py/lookml_metric_views/${name}`)).text();
      pyodide.FS.writeFile(`/app/lookml_metric_views/${name}`, code);
    }
    pyodide.runPython("import sys; sys.path.insert(0, '/app')");
  }
  if (dialect !== "databricks" && !pyodide._sqlglotLoaded) {
    setStatus(`Loading the SQL translator for ${dialect}…`);
    const micropip = pyodide.pyimport("micropip");
    await micropip.install("sqlglot==30.35.0");
    pyodide._sqlglotLoaded = true;
  }
  return pyodide;
}

async function runConversion(files, options) {
  const py = await loadPython(options.dialect);
  setStatus(`Converting ${Object.keys(files).length} files…`);
  py.globals.set("files_json", JSON.stringify(files));
  py.globals.set("opts_json", JSON.stringify(options));
  const out = py.runPython(`
import json
from lookml_metric_views import convert
_opts = json.loads(opts_json)
_out, _results, _project = convert(json.loads(files_json), dialect=_opts["dialect"],
    catalog=_opts["catalog"], schema=_opts["schema"], table_map=_opts.get("table_map") or None,
    source=_opts.get("source", ""))
json.dumps(_out)
`);
  return JSON.parse(out);
}

// ---- GitHub ---------------------------------------------------------------------------------
async function fetchGithub(url) {
  const m = url.trim().match(/^https:\/\/github\.com\/([^/]+)\/([^/#?]+)(?:\/tree\/([^#?]+))?/);
  if (!m) throw new Error("Enter a GitHub repository URL like https://github.com/owner/repo.");
  const owner = m[1];
  const repo = m[2].replace(/\.git$/, "");
  let branch = m[3];
  const api = (path) => fetch(`https://api.github.com/repos/${owner}/${repo}${path}`, { headers: { Accept: "application/vnd.github+json" } });
  if (!branch) {
    const r = await api("");
    if (r.status === 404) throw new Error("That repository was not found or is private.");
    if (r.status === 403) throw new Error("GitHub's rate limit for anonymous use was reached. Try again in an hour or use local files.");
    branch = (await r.json()).default_branch;
  }
  const t = await api(`/git/trees/${encodeURIComponent(branch)}?recursive=1`);
  if (!t.ok) throw new Error(`GitHub returned ${t.status} while listing files.`);
  const tree = await t.json();
  const paths = tree.tree.filter((e) => e.type === "blob" && /\.(lkml|lookml)$/.test(e.path)).map((e) => e.path);
  if (!paths.length) throw new Error("No .lkml files in that repository.");
  const files = {};
  let done = 0;
  await Promise.all(paths.map(async (p) => {
    const r = await fetch(`https://raw.githubusercontent.com/${owner}/${repo}/${branch}/${p.split("/").map(encodeURIComponent).join("/")}`);
    files[p] = await r.text();
    done += 1;
    setStatus(`Downloading LookML from GitHub (${done} of ${paths.length})…`);
  }));
  return files;
}

// ---- Rendering ------------------------------------------------------------------------------
function show(outFiles, source) {
  const report = JSON.parse(outFiles["report.json"]);
  current = { files: outFiles, report, source };
  // Open the explore with the most items to check: that is where the report is most useful.
  const flagged = (e) => e.items.filter((it) => it.status === "review" || it.status === "manual").length;
  activeExplore = report.explores.reduce((best, e, i, all) => (flagged(e) > flagged(all[best]) ? i : best), 0);
  $("result").hidden = false;
  $("result-title").textContent = source ? `Result for ${source.replace("https://github.com/", "")}` : "Result";
  renderSummary(report.summary);
  renderExploreList();
  renderLedger();
}

function renderSummary(summary) {
  const counts = summary.counts;
  const total = STATUSES.reduce((a, s) => a + counts[s], 0) || 1;
  const strip = $("strip");
  strip.replaceChildren();
  STATUSES.forEach((s, i) => {
    if (!counts[s]) return;
    const seg = el("span");
    seg.style.width = `${(100 * counts[s]) / total}%`;
    seg.style.background = COLORS[s];
    seg.style.animationDelay = `${i * 60}ms`;
    strip.append(seg);
  });
  const legend = $("legend");
  legend.replaceChildren();
  for (const s of STATUSES) {
    const dot = el("span", { class: "dot" });
    dot.style.background = COLORS[s];
    legend.append(el("li", {}, dot, el("span", { text: `${LABELS[s]} ` }), el("b", { text: String(counts[s]) }), el("span", { text: ` (${MEANING[s]})` })));
  }
  const counted = counts.exact + counts.adapted + counts.review + counts.manual;
  const auto = counts.exact + counts.adapted;
  $("auto").textContent = counted
    ? `${summary.explores} explores, ${summary.metric_views} metric views. ${auto} of ${counted} fields, measures and joins (${Math.round((100 * auto) / counted)}%) translate without a person checking them.`
    : "Nothing to translate.";
}

function renderExploreList() {
  const list = $("explore-list");
  list.replaceChildren();
  current.report.explores.forEach((e, i) => {
    const flagged = e.items.filter((it) => it.status === "review" || it.status === "manual").length;
    const btn = el("button", { type: "button", "aria-current": String(i === activeExplore) },
      el("span", { text: e.explore }), el("small", { text: flagged ? `${flagged} to check` : "clean" }));
    btn.addEventListener("click", () => { activeExplore = i; renderExploreList(); renderLedger(); });
    list.append(el("li", {}, btn));
  });
}

function renderLedger() {
  const e = current.report.explores[activeExplore];
  if (!e) return;
  $("explore-title").textContent = e.model ? `${e.explore} (model ${e.model})` : e.explore;
  const body = $("ledger");
  body.replaceChildren();
  const items = e.items.filter((it) => activeFilter === "all" || it.status === activeFilter);
  if (!items.length) {
    body.append(el("tr", {}, el("td", { colspan: "4", text: activeFilter === "all" ? "No items." : `Nothing marked ${activeFilter} in this explore.` })));
  }
  for (const it of items) {
    const tr = el("tr", { class: it.status },
      el("td", { class: "st" }, el("span", { class: `tag ${it.status}`, text: it.status })),
      el("td", {}, el("code", { text: it.looker })),
      it.name ? el("td", { class: "mv" }, el("code", { text: it.name })) : el("td", { class: "mv" }),
      el("td", { class: "why", text: it.reason || (it.kind === "source" ? "Source table" : "") }));
    body.append(tr);
  }
  const yamlText = e.metric_view ? current.files[`metric_views/${e.metric_view}.yaml`] : "# No metric view was written for this explore. See the report above.";
  $("yaml").textContent = yamlText;
}

document.querySelectorAll(".filters button").forEach((b) => {
  b.addEventListener("click", () => {
    activeFilter = b.dataset.filter;
    document.querySelectorAll(".filters button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    renderLedger();
  });
});

$("copy-yaml").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("yaml").textContent);
    $("copy-yaml").textContent = "Copied";
    setTimeout(() => { $("copy-yaml").textContent = "Copy YAML"; }, 1600);
  } catch {
    setStatus("Copy failed. Select the YAML and copy it by hand.", true);
  }
});

$("download").addEventListener("click", async () => {
  if (!current || !window.JSZip) return;
  const zip = new JSZip();
  for (const [path, text] of Object.entries(current.files)) zip.file(path, text);
  const blob = await zip.generateAsync({ type: "blob" });
  const a = el("a", { href: URL.createObjectURL(blob), download: "metric_views.zip" });
  document.body.append(a);
  a.click();
  a.remove();
});

function options(source) {
  const target = $("target").value.trim() || "main.looker";
  const [catalog, schema] = target.split(".");
  const dialect = $("dialect").value;
  // Map bare Looker table names (views without sql_table_name) and the connection's own schema
  // into the chosen catalog.schema, so the YAML is deployable as is.
  return { dialect, catalog, schema, table_map: { "*": `${catalog}.${schema}.*` }, source };
}

async function convertAndShow(getFiles, source) {
  const btn = $("convert");
  btn.disabled = true;
  try {
    const files = await getFiles();
    const out = await runConversion(files, options(source));
    show(out, source);
    setStatus(`Converted ${Object.keys(files).length} files.`);
    $("result").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  } catch (err) {
    setStatus(err.message || String(err), true);
  } finally {
    btn.disabled = false;
  }
}

$("run").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const url = $("repo").value;
  convertAndShow(() => fetchGithub(url), url);
});

$("files").addEventListener("change", (ev) => {
  const list = Array.from(ev.target.files || []);
  if (!list.length) return;
  convertAndShow(async () => {
    const files = {};
    for (const f of list) files[f.webkitRelativePath || f.name] = await f.text();
    return files;
  }, `${list.length} local files`);
});

// ---- Precomputed example and survey ---------------------------------------------------------
async function loadExample() {
  try {
    const out = await (await fetch("data/example_tpch.json")).json();
    show(out, "the TPC-H example");
    setStatus("Showing a precomputed run on the TPC-H example from this repository, over the samples.tpch tables every Databricks workspace has. Press Convert to run Looker's public thelook project live.");
  } catch {
    /* the page still works without the example */
  }
}

async function loadSurvey() {
  try {
    const data = await (await fetch("data/survey.json")).json();
    const ok = data.projects.filter((p) => !p.error && p.explores > 0);
    const t = data.totals;
    const counted = ["exact", "adapted", "review", "manual"].reduce((a, s) => a + (t[s] || 0), 0);
    const explores = ok.reduce((a, p) => a + p.explores, 0);
    const views = ok.reduce((a, p) => a + p.metric_views, 0);
    const shares = ok.map((p) => {
      const c = p.counts;
      const n = ["exact", "adapted", "review", "manual"].reduce((a, s) => a + (c[s] || 0), 0);
      return n ? ((c.exact || 0) + (c.adapted || 0)) / n : null;
    }).filter((x) => x !== null).sort((a, b) => a - b);
    const mid = shares.length ? (shares[Math.floor((shares.length - 1) / 2)] + shares[Math.ceil((shares.length - 1) / 2)]) / 2 : 0;
    const auto = counted ? ((t.exact || 0) + (t.adapted || 0)) / counted : 0;
    $("survey-intro").textContent = `I ran the converter on ${ok.length} public Looker projects from Google's looker-open-source organisation on ${new Date(data.date).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}. ${explores} explores became ${views} metric views. Of ${counted.toLocaleString("en")} fields, measures and joins, ${Math.round(100 * auto)}% translate without a person. In the median project it is ${Math.round(100 * mid)}%.`;
    const chart = $("survey-chart");
    chart.replaceChildren();
    for (const s of ["exact", "adapted", "review", "manual"]) {
      const share = counted ? (100 * (t[s] || 0)) / counted : 0;
      const fill = el("div", { class: "bar-fill" });
      fill.style.width = `${share}%`;
      fill.style.background = COLORS[s];
      chart.append(el("div", { class: "bar-row" }, el("span", { text: LABELS[s] }), el("div", { class: "bar-track", role: "img", "aria-label": `${LABELS[s]}: ${share.toFixed(1)} percent` }, fill), el("b", { text: `${share.toFixed(0)}%` })));
    }
    const reasons = $("survey-reasons");
    reasons.replaceChildren();
    for (const r of data.top_reasons.slice(0, 8)) {
      reasons.append(el("li", {}, el("span", { class: `tag ${r.status}`, text: r.status }), el("span", { text: ` ${r.reason} (${r.count.toLocaleString("en")})` })));
    }
    const liquid = data.liquid_manual || 0;
    $("survey-finding").textContent = t.manual ? `Liquid templating is the largest blocker: ${Math.round((100 * liquid) / t.manual)}% of the items that need a person use it, and a metric view has no templating. Most of these projects are BigQuery blocks, so UNNEST joins weigh more here than for a team already on Databricks.` : "";
    $("survey-fine").textContent = "Only counts are stored, not the projects' code. The list shows the most common reasons a person is still needed, with how often each appears.";
  } catch {
    $("survey-intro").textContent = "The survey data could not be loaded.";
  }
}

async function loadLive() {
  try {
    const r = await fetch("data/live_check.json");
    if (!r.ok) return;
    const data = await r.json();
    const body = $("live-body");
    body.append(el("p", { text: data.summary }));
    const table = el("table", { class: "live-table" });
    table.append(el("thead", {}, el("tr", {}, el("th", { text: "Measure" }), el("th", { text: "Status" }), el("th", { text: "Metric view" }), el("th", { text: "Looker logic in SQL" }), el("th", { text: "Match" }))));
    const num = (v) => {
      const n = Number(v);
      return Number.isFinite(n) ? n.toLocaleString("en", { maximumFractionDigits: Math.abs(n) < 10 ? 4 : 2 }) : String(v);
    };
    const tb = el("tbody");
    let group = "";
    for (const row of data.rows) {
      const [mv, measure] = row.measure.split(".");
      if (mv !== group) {
        group = mv;
        tb.append(el("tr", { class: "group" }, el("th", { colspan: "5", scope: "colgroup" }, el("code", { text: mv }))));
      }
      tb.append(el("tr", { class: row.match ? "" : "differs" },
        el("td", {}, el("code", { text: measure })),
        el("td", {}, el("span", { class: `tag ${row.status}`, text: row.status })),
        el("td", { class: "num", text: num(row.metric_view) }),
        el("td", { class: "num", text: num(row.looker_sql) }),
        el("td", { text: row.match ? "yes" : "no" })));
    }
    table.append(tb);
    body.append(el("div", { class: "table-scroll" }, table));
    if (data.note) body.append(el("p", { class: "fine", text: data.note }));
    $("live").hidden = false;
  } catch {
    /* optional section */
  }
}

loadExample();
loadSurvey();
loadLive();
