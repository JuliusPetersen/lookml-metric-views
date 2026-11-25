"""Create the TPC-H example metric views in a Databricks workspace and compare every measure
with SQL that reproduces what Looker would return (including symmetric aggregates).

Needs the Databricks CLI, logged in:  databricks auth login --host <workspace> --profile <name>

    python scripts/live_check.py --profile lookml --warehouse <id> --catalog workspace --schema lookml_test
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

FILTER = "l_shipdate < DATE'1998-09-01'"
L = f"(SELECT * FROM samples.tpch.lineitem WHERE {FILTER})"
ORDERS_IN_L = f"(SELECT o.* FROM samples.tpch.orders o WHERE o.o_orderkey IN (SELECT l_orderkey FROM {L}))"
CUSTOMERS_IN_L = (f"(SELECT c.* FROM samples.tpch.customer c WHERE c.c_custkey IN "
                  f"(SELECT o_custkey FROM {ORDERS_IN_L}))")

# What Looker returns for each measure, written as plain SQL. In the lineitem explore Looker
# applies symmetric aggregates, so measures of joined views count each order or customer once.
LOOKER_TRUTH = {
    "tpch__lineitem": {
        "line_count": f"SELECT COUNT(*) FROM {L}",
        "total_quantity": f"SELECT SUM(l_quantity) FROM {L}",
        "total_net_revenue": f"SELECT SUM(l_extendedprice * (1 - l_discount)) FROM {L}",
        "returned_revenue": f"SELECT SUM(l_extendedprice * (1 - l_discount)) FROM {L} WHERE l_returnflag = 'R'",
        "discounted_lines": f"SELECT COUNT(*) FROM {L} WHERE l_discount > 0",
        "return_rate": (f"SELECT 1.0 * SUM(CASE WHEN l_returnflag = 'R' THEN l_extendedprice * (1 - l_discount) END)"
                        f" / NULLIF(SUM(l_extendedprice * (1 - l_discount)), 0) FROM {L}"),
        "orders_order_count": f"SELECT COUNT(*) FROM {ORDERS_IN_L}",
        "orders_total_order_value": f"SELECT SUM(o_totalprice) FROM {ORDERS_IN_L}",
        "orders_max_order_value": f"SELECT MAX(o_totalprice) FROM {ORDERS_IN_L}",
        "orders_urgent_orders": f"SELECT COUNT(*) FROM {ORDERS_IN_L} WHERE o_orderpriority = '1-URGENT'",
        "customer_customer_count": f"SELECT COUNT(*) FROM {CUSTOMERS_IN_L}",
    },
    "tpch__orders": {
        "order_count": "SELECT COUNT(*) FROM samples.tpch.orders",
        "total_order_value": "SELECT SUM(o_totalprice) FROM samples.tpch.orders",
        "max_order_value": "SELECT MAX(o_totalprice) FROM samples.tpch.orders",
        "urgent_orders": "SELECT COUNT(*) FROM samples.tpch.orders WHERE o_orderpriority = '1-URGENT'",
        "customer_customer_count": ("SELECT COUNT(DISTINCT c.c_custkey) FROM samples.tpch.orders o "
                                    "LEFT JOIN samples.tpch.customer c ON o.o_custkey = c.c_custkey"),
    },
}


def run_sql(profile: str, warehouse: str, sql: str, catalog: str = None, schema: str = None) -> list:
    body = {"warehouse_id": warehouse, "statement": sql, "wait_timeout": "50s", "on_wait_timeout": "CONTINUE"}
    if catalog:
        body["catalog"] = catalog
    if schema:
        body["schema"] = schema
    out = subprocess.run(["databricks", "api", "post", "/api/2.0/sql/statements", "--profile", profile,
                          "--json", json.dumps(body)], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr or out.stdout)
    res = json.loads(out.stdout)
    while res["status"]["state"] in ("PENDING", "RUNNING"):
        time.sleep(2)
        poll = subprocess.run(["databricks", "api", "get", f"/api/2.0/sql/statements/{res['statement_id']}",
                               "--profile", profile], capture_output=True, text=True)
        if poll.returncode != 0:
            raise RuntimeError(poll.stderr or poll.stdout)
        res = json.loads(poll.stdout)
    if res["status"]["state"] != "SUCCEEDED":
        raise RuntimeError(res["status"].get("error", {}).get("message", res["status"]))
    return res.get("result", {}).get("data_array", []) or []


def close(a, b) -> bool:
    if a is None or b is None:
        return a == b
    try:
        fa, fb = float(a), float(b)
    except ValueError:
        return a == b
    return abs(fa - fb) <= 1e-6 * max(1.0, abs(fb))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--warehouse", required=True)
    p.add_argument("--catalog", default="workspace")
    p.add_argument("--schema", default="lookml_test")
    p.add_argument("--create-sql", default="examples/tpch/output/create_metric_views.sql")
    p.add_argument("--report", default="examples/tpch/output/report.json")
    p.add_argument("--out", default="web/data/live_check.json")
    args = p.parse_args()

    run_sql(args.profile, args.warehouse, f"CREATE SCHEMA IF NOT EXISTS {args.catalog}.{args.schema}")
    created = []
    for stmt in open(args.create_sql).read().split("$$;"):
        if "CREATE OR REPLACE VIEW" not in stmt:
            continue
        sql = stmt[stmt.index("CREATE OR REPLACE VIEW"):] + "$$"
        name = sql.split()[4]
        run_sql(args.profile, args.warehouse, sql)
        created.append(name)
        print("created", name, file=sys.stderr)

    status_of = {}
    report = json.load(open(args.report))
    for e in report["explores"]:
        for it in e["items"]:
            if it["kind"] == "measure" and it["name"]:
                status_of[(e["metric_view"], it["name"])] = it["status"]

    rows = []
    for mv, truths in LOOKER_TRUTH.items():
        target = f"{args.catalog}.{args.schema}.{mv}"
        names = list(truths)
        got = run_sql(args.profile, args.warehouse,
                      "SELECT " + ", ".join(f"MEASURE(`{n}`)" for n in names) + f" FROM {target}")[0]
        for n, value in zip(names, got):
            truth = run_sql(args.profile, args.warehouse, truths[n])[0][0]
            rows.append({"metric_view": value, "looker_sql": truth, "match": close(value, truth),
                         "measure": f"{mv}.{n}", "status": status_of.get((mv, n), "?")})
            print(mv, n, value, truth, file=sys.stderr)

    matches = sum(r["match"] for r in rows)
    flagged_mismatch = [r for r in rows if not r["match"] and r["status"] == "review"]
    unflagged_mismatch = [r for r in rows if not r["match"] and r["status"] != "review"]
    summary = (f"On {time.strftime('%d %b %Y')} I created the {len(created)} generated metric views in a Databricks "
               f"Free Edition workspace over the TPC-H sample data and compared {len(rows)} measures with SQL that "
               f"reproduces Looker's results. {matches} match exactly. ")
    if flagged_mismatch and not unflagged_mismatch:
        ratio = float(flagged_mismatch[0]["metric_view"]) / float(flagged_mismatch[0]["looker_sql"])
        which = "measure that differs is the one" if len(flagged_mismatch) == 1 else f"{len(flagged_mismatch)} that differ are the ones"
        summary += (f"The {which} the report marks Review, as expected ({ratio:.1f} times Looker's value): "
                    "Looker counts each order once (symmetric aggregates), the metric view once per order line.")
    elif unflagged_mismatch:
        summary += f"{len(unflagged_mismatch)} differ without a warning; that is a bug to fix."
    data = {"date": time.strftime("%Y-%m-%d"), "created": created, "rows": rows, "summary": summary,
            "note": "Example LookML and both sets of SQL are in the repository (examples/tpch, scripts/live_check.py)."}
    with open(args.out, "w") as fh:
        json.dump(data, fh, indent=2)
    print(summary)
    return 0 if not unflagged_mismatch else 1


if __name__ == "__main__":
    sys.exit(main())
