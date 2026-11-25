"""Create empty tables with the columns a LookML project uses, then create the generated metric
views on them. This checks that the YAML is accepted by Databricks, not that numbers match.

    python scripts/deploy_check.py PROJECT_DIR --dialect bigquery --profile lookml --warehouse ID
"""

import argparse
import re
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

from live_check import run_sql  # noqa: E402
from lookml_metric_views import convert, load_project  # noqa: E402
from lookml_metric_views.cli import read_folder  # noqa: E402

TYPE = {"number": "DOUBLE", "yesno": "BOOLEAN", "string": "STRING", "zipcode": "STRING", "tier": "DOUBLE"}


def columns(view):
    cols = {}
    for d in view.get("dimensions", []) or []:
        for c in re.findall(r"\$\{TABLE\}\.`?(\w+)`?", d.get("sql", "") or "${TABLE}." + d["name"]):
            cols.setdefault(c, TYPE.get(str(d.get("type", "string")), "STRING"))
    for g in view.get("dimension_groups", []) or []:
        for c in re.findall(r"\$\{TABLE\}\.`?(\w+)`?", g.get("sql", "") or ""):
            cols.setdefault(c, "DATE" if g.get("datatype") == "date" else "TIMESTAMP")
    for m in view.get("measures", []) or []:
        for c in re.findall(r"\$\{TABLE\}\.`?(\w+)`?", m.get("sql", "") or ""):
            cols.setdefault(c, "DOUBLE")
    return cols


def main():
    p = argparse.ArgumentParser()
    p.add_argument("project")
    p.add_argument("--dialect", default="databricks")
    p.add_argument("--profile", required=True)
    p.add_argument("--warehouse", required=True)
    p.add_argument("--catalog", default="workspace")
    p.add_argument("--schema", required=True)
    a = p.parse_args()
    files = read_folder(a.project)
    project = load_project(files)
    run_sql(a.profile, a.warehouse, f"CREATE SCHEMA IF NOT EXISTS {a.catalog}.{a.schema}")
    for name, view in project.views.items():
        cols = columns(view)
        if not cols or view.get("derived_table"):
            continue
        ddl = ", ".join(f"`{c}` {t}" for c, t in cols.items())
        run_sql(a.profile, a.warehouse, f"CREATE OR REPLACE TABLE {a.catalog}.{a.schema}.{name} ({ddl})")
    out, results, _ = convert(files, dialect=a.dialect, catalog=a.catalog, schema=a.schema,
                              table_map={"*": f"{a.catalog}.{a.schema}.*"})
    ok, failed = [], []
    for stmt in out["create_metric_views.sql"].split("$$;"):
        if "CREATE OR REPLACE VIEW" not in stmt:
            continue
        sql = stmt[stmt.index("CREATE OR REPLACE VIEW"):] + "$$"
        name = sql.split()[4]
        try:
            run_sql(a.profile, a.warehouse, sql, catalog=a.catalog, schema=a.schema)
            ok.append(name)
        except Exception as exc:
            failed.append((name, str(exc)[:400]))
    print(f"created {len(ok)} of {len(ok) + len(failed)} metric views")
    for n, e in failed:
        print("FAILED", n, e)


if __name__ == "__main__":
    main()
