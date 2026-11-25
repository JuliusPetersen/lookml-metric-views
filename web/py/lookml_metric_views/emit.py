"""Write metric view YAML, deployment SQL, reconciliation queries and the translation report."""

from __future__ import annotations

import json
from collections import Counter
from typing import Dict, List, Optional

import yaml

from .translate import ADAPTED, EXACT, MANUAL, REVIEW, SKIPPED, STATUSES, Result


class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_presenter)


def metric_view_yaml(result: Result) -> str:
    assert result.metric_view is not None
    text = yaml.dump(result.metric_view, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=10_000)
    return text


def create_sql(result: Result, catalog: str, schema: str) -> str:
    body = metric_view_yaml(result).rstrip("\n")
    return (f"CREATE OR REPLACE VIEW {catalog}.{schema}.{result.metric_view_name}\n"
            f"WITH METRICS\nLANGUAGE YAML\nAS $$\n{body}\n$$;\n")


def reconcile_sql(result: Result, catalog: str, schema: str) -> str:
    """Totals per measure, overall and by one field, to compare with the same Looker explore."""
    if not result.measure_names:
        return ""
    target = f"{catalog}.{schema}.{result.metric_view_name}"
    measures = ",\n  ".join(f"MEASURE(`{m}`) AS `{m}`" for m in result.measure_names)
    sql = f"-- {result.explore}: overall totals. In Looker, run the explore with the same measures and no filters.\n"
    sql += f"SELECT\n  {measures}\nFROM {target};\n"
    group = next((f for f in result.field_names if f.endswith("_year") or f.endswith("_month")), None) \
        or (result.field_names[0] if result.field_names else None)
    if group:
        sql += f"\n-- {result.explore}: by `{group}`. Compare row by row with Looker.\n"
        sql += f"SELECT\n  `{group}`,\n  {measures}\nFROM {target}\nGROUP BY ALL\nORDER BY 1;\n"
    return sql


def summary(results: List[Result]) -> Dict:
    counts = Counter()
    by_kind: Dict[str, Counter] = {}
    reasons = Counter()
    for r in results:
        for item in r.items:
            if item.kind not in ("field", "measure", "join"):
                continue
            counts[item.status] += 1
            by_kind.setdefault(item.kind, Counter())[item.status] += 1
            if item.status in (MANUAL, REVIEW):
                reasons[(item.status, _reason_key(item.reason))] += 1
    return {
        "explores": len(results),
        "metric_views": sum(1 for r in results if r.metric_view),
        "counts": {s: counts.get(s, 0) for s in STATUSES},
        "by_kind": {k: {s: c.get(s, 0) for s in STATUSES} for k, c in by_kind.items()},
        "top_reasons": [{"status": s, "reason": k, "count": n} for (s, k), n in reasons.most_common(12)],
    }


def _reason_key(reason: str) -> str:
    """Group similar reasons: keep the first sentence and drop field names in quotes."""
    import re
    blocked = "Explore's base view can't be translated. "
    if reason.startswith(blocked):
        return "Base view not translated: " + _reason_key(reason[len(blocked):])
    first = reason.split(". ")[0].split("; ")[0]
    first = re.sub(r"(?<![A-Za-z])'[^']*'(?![A-Za-z])", "'…'", first)
    return first.strip().rstrip(".")


def report_json(results: List[Result], project_warnings) -> str:
    data = {
        "summary": summary(results),
        "warnings": [{"file": p, "message": m} for p, m in project_warnings],
        "explores": [
            {
                "model": r.model,
                "explore": r.explore,
                "metric_view": r.metric_view_name if r.metric_view else None,
                "items": [item.__dict__ for item in r.items],
            }
            for r in results
        ],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


LABEL = {EXACT: "Exact", ADAPTED: "Adapted", REVIEW: "Review", MANUAL: "Manual", SKIPPED: "Skipped"}


def report_markdown(results: List[Result], project_warnings, source: str = "") -> str:
    s = summary(results)
    c = s["counts"]
    total = sum(c[k] for k in (EXACT, ADAPTED, REVIEW, MANUAL))
    lines = ["# LookML to metric views: translation report", ""]
    if source:
        lines += [f"Source: {source}", ""]
    lines += [
        f"{s['explores']} explores, {s['metric_views']} metric views written.",
        "",
        "| Status | Fields, measures and joins | Meaning |",
        "|---|---|---|",
        f"| Exact | {c[EXACT]} | Same meaning, mechanical translation |",
        f"| Adapted | {c[ADAPTED]} | Same meaning, value shown differently |",
        f"| Review | {c[REVIEW]} | Translated, but numbers can differ from Looker |",
        f"| Manual | {c[MANUAL]} | Not translated, needs a person |",
        f"| Skipped | {c[SKIPPED]} | Hidden in Looker, not exposed |",
        "",
    ]
    lines += ["Not checked by this tool: Looker connection settings. If your connection converts timestamps to "
              "the user's timezone, time fields can shift (run again with --timezone-conversion to flag them).", ""]
    if total:
        auto = c[EXACT] + c[ADAPTED]
        lines += [f"{auto} of {total} items ({round(100 * auto / total)}%) translate without a person checking them.", ""]
    if s["top_reasons"]:
        lines += ["## What needs a person most often", ""]
        for r in s["top_reasons"]:
            lines.append(f"- {LABEL[r['status']]} ({r['count']}): {r['reason']}")
        lines.append("")
    if project_warnings:
        lines += ["## Project warnings", ""]
        lines += [f"- {p or 'project'}: {m}" for p, m in project_warnings]
        lines.append("")
    for r in results:
        lines += [f"## Explore `{r.explore}`" + (f" (model `{r.model}`)" if r.model else ""), ""]
        lines.append(f"Metric view: `{r.metric_view_name}`" if r.metric_view else "No metric view written.")
        lines += ["", "| Looker | Kind | Status | Metric view name | Note |", "|---|---|---|---|---|"]
        for item in r.items:
            note = item.reason.replace("|", "\\|")
            lines.append(f"| `{item.looker}` | {item.kind} | {LABEL.get(item.status, item.status)} | "
                         f"{'`' + item.name + '`' if item.name else ''} | {note} |")
        lines.append("")
    return "\n".join(lines)


def write_outputs(results: List[Result], project_warnings, out_files: Dict[str, str],
                  catalog: str = "main", schema: str = "looker", source: str = "") -> Dict[str, str]:
    """Fill out_files with {relative path: text}. Pure function, so it also works in the browser."""
    creates, reconciles = [], []
    for r in results:
        if not r.metric_view:
            continue
        out_files[f"metric_views/{r.metric_view_name}.yaml"] = (
            f"# Metric view for Looker explore '{r.explore}'. Deploy with create_metric_views.sql.\n"
            + metric_view_yaml(r))
        creates.append(create_sql(r, catalog, schema))
        rec = reconcile_sql(r, catalog, schema)
        if rec:
            reconciles.append(rec)
    out_files["create_metric_views.sql"] = (
        f"-- Creates one metric view per Looker explore in {catalog}.{schema}.\n"
        "-- Read report.md first: items marked Review can give different numbers than Looker.\n\n"
        + "\n".join(creates))
    out_files["reconcile.sql"] = (
        "-- Compare these totals with the same measures in Looker before switching anything off.\n\n"
        + "\n".join(reconciles))
    out_files["report.md"] = report_markdown(results, project_warnings, source)
    out_files["report.json"] = report_json(results, project_warnings)
    return out_files
