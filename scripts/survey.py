"""Run the converter over public Looker projects on GitHub and collect aggregate numbers.

Only counts and reason categories are stored, never the LookML itself, so the results can be
published without redistributing anyone's code.

    GITHUB_TOKEN=... python scripts/survey.py --out web/data/survey.json repo1 repo2 ...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, "src")

from lookml_metric_views import convert  # noqa: E402
from lookml_metric_views.cli import read_github  # noqa: E402
from lookml_metric_views.emit import _reason_key  # noqa: E402

LIQUID_PATTERNS = {
    "templated_filter_condition": r"\{%-?\s*condition\b",
    "date_start_or_end": r"\{%-?\s*date_(start|end)\b",
    "parameter": r"\{%-?\s*parameter\b|_parameter_value",
}

DIALECT_HINTS = {"snowflake": "snowflake", "redshift": "redshift", "postgres": "postgres", "athena": "presto"}


def guess_dialect(repo: str, files) -> str:
    for hint, dialect in DIALECT_HINTS.items():
        if hint in repo.lower():
            return dialect
    text = " ".join(files.values())[:200_000].lower()
    if "bigquery" in text or "`" in text and ".table" in text:
        return "bigquery"
    return "bigquery"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("repos", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--cache", default=".cache/repos", help="Keep downloaded files here between runs")
    args = p.parse_args()
    os.makedirs(args.cache, exist_ok=True)

    totals = Counter()
    by_kind = {}
    reasons = Counter()
    liquid_manual = 0
    patterns = Counter()  # projects that use each Liquid pattern anywhere in their LookML
    projects = []
    for repo in args.repos:
        url = repo if repo.startswith("https://") else f"https://github.com/{repo}"
        cached = os.path.join(args.cache, repo.replace("/", "__") + ".json")
        try:
            if os.path.exists(cached):
                with open(cached) as fh:
                    files = json.load(fh)
            else:
                files = read_github(url)
                with open(cached, "w") as fh:
                    json.dump(files, fh)
        except Exception as exc:
            projects.append({"repo": repo, "error": f"fetch failed: {exc}"})
            continue
        if not files:
            projects.append({"repo": repo, "error": "no .lkml files"})
            continue
        text = "\n".join(files.values())
        for key, rx in LIQUID_PATTERNS.items():
            if re.search(rx, text):
                patterns[key] += 1
        dialect = guess_dialect(repo, files)
        started = time.time()
        try:
            _, results, project = convert(files, dialect=dialect)
        except Exception as exc:
            projects.append({"repo": repo, "error": f"converter crashed: {exc!r}"})
            continue
        counts = Counter()
        for r in results:
            for item in r.items:
                if item.kind in ("field", "measure", "join"):
                    counts[item.status] += 1
                    totals[item.status] += 1
                    by_kind.setdefault(item.kind, Counter())[item.status] += 1
                    if item.status in ("manual", "review"):
                        reasons[(item.status, _reason_key(item.reason))] += 1
                    if item.status == "manual" and "Liquid" in item.reason:
                        liquid_manual += 1
                if item.kind == "explore" and item.status == "manual" and "Unexpected error" in item.reason:
                    counts["crash"] += 1
        projects.append({
            "repo": repo,
            "dialect": dialect,
            "files": project.files,
            "views": len(project.views),
            "explores": len(results),
            "metric_views": sum(1 for r in results if r.metric_view),
            "counts": dict(counts),
            "seconds": round(time.time() - started, 2),
        })
        print(repo, dict(counts), file=sys.stderr)

    data = {
        "date": time.strftime("%Y-%m-%d"),
        "projects": projects,
        "totals": dict(totals),
        "by_kind": {k: dict(v) for k, v in by_kind.items()},
        "liquid_manual": liquid_manual,
        "liquid_pattern_projects": dict(patterns),
        "top_reasons": [{"status": s, "reason": r, "count": n} for (s, r), n in reasons.most_common(15)],
    }
    with open(args.out, "w") as fh:
        json.dump(data, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
