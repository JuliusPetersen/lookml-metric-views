"""Command line: lookml-metric-views PATH_OR_GITHUB_URL [options]"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Dict

from . import convert
from .emit import summary


def read_folder(path: str) -> Dict[str, str]:
    files = {}
    for root, dirs, names in os.walk(path):
        # Skip hidden folders (.git, .venv, ...) below the given path, but not the path itself.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            if name.endswith((".lkml", ".lookml")):
                full = os.path.join(root, name)
                with open(full, encoding="utf-8", errors="replace") as fh:
                    files[os.path.relpath(full, path)] = fh.read()
    return files


def read_github(url: str) -> Dict[str, str]:
    """Fetch all .lkml files of a public GitHub repository (optionally /tree/<branch>)."""
    parts = url.rstrip("/").split("github.com/")[1].split("/")
    owner, repo = parts[0], (parts[1][:-4] if parts[1].endswith(".git") else parts[1])
    branch = "/".join(parts[3:]) if len(parts) > 3 and parts[2] == "tree" else None
    headers = {"User-Agent": "lookml-metric-views", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def get(u: str) -> bytes:
        with urllib.request.urlopen(urllib.request.Request(u, headers=headers), timeout=30) as resp:
            return resp.read()

    if branch is None:
        branch = json.loads(get(f"https://api.github.com/repos/{owner}/{repo}"))["default_branch"]
    from urllib.parse import quote as _q
    tree = json.loads(get(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{_q(branch, safe='')}?recursive=1"))
    paths = [e["path"] for e in tree.get("tree", [])
             if e["type"] == "blob" and e["path"].endswith((".lkml", ".lookml"))]
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import quote

    def fetch(path: str):
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(branch)}/{quote(path)}"
        return path, get(raw).decode("utf-8", errors="replace")

    with ThreadPoolExecutor(max_workers=12) as pool:
        return dict(pool.map(fetch, paths))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="lookml-metric-views",
                                description="Convert a LookML project into Databricks metric views.")
    p.add_argument("project", help="Folder with .lkml files, or a public GitHub repository URL")
    p.add_argument("--out", default="metric_views_out", help="Output folder (default: metric_views_out)")
    p.add_argument("--dialect", default="databricks",
                   help="SQL dialect of the Looker connection, e.g. databricks, bigquery, snowflake")
    p.add_argument("--catalog", default="main")
    p.add_argument("--schema", default="looker")
    p.add_argument("--table-map", action="append", default=[],
                   help="Map a Looker table to Unity Catalog: 'project.dataset.table=main.sales.table' "
                        "or a prefix 'project.dataset.*=main.sales.*'")
    p.add_argument("--timezone-conversion", action="store_true",
                   help="Your Looker connection converts timestamps to the user's timezone; flag time fields for review")
    p.add_argument("--summary", action="store_true", help="Print the summary as JSON")
    args = p.parse_args(argv)

    files = read_github(args.project) if args.project.startswith("https://github.com/") else read_folder(args.project)
    if not files:
        print("No .lkml files found.", file=sys.stderr)
        return 1
    table_map = dict(m.split("=", 1) for m in args.table_map)
    out, results, project = convert(files, args.dialect, args.catalog, args.schema, table_map, source=args.project,
                                   timezone_conversion=args.timezone_conversion)
    os.makedirs(args.out, exist_ok=True)
    for rel, text in out.items():
        full = os.path.join(args.out, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
    s = summary(results)
    if args.summary:
        print(json.dumps(s, indent=2))
    else:
        c = s["counts"]
        print(f"{s['explores']} explores -> {s['metric_views']} metric views in {args.out}/")
        print(f"exact {c['exact']}, adapted {c['adapted']}, review {c['review']}, manual {c['manual']}, skipped {c['skipped']}")
        print(f"Read {args.out}/report.md before deploying.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
