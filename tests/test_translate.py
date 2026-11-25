import re

import yaml

from lookml_metric_views import convert

MODEL = """
connection: "dbx"
include: "*.view.lkml"

explore: order_items {
  sql_always_where: ${order_items.status} <> 'test' ;;
  always_filter: { filters: [order_items.status: "complete"] }
  join: orders {
    sql_on: ${order_items.order_id} = ${orders.id} ;;
    relationship: many_to_one
  }
  join: users {
    sql_on: ${orders.user_id} = ${users.id} ;;
    relationship: many_to_one
  }
  join: events {
    sql_on: ${users.id} = ${events.user_id} ;;
    relationship: one_to_many
  }
  join: tags {
    sql_on: ${order_items.id} = ${tags.item_id} ;;
    relationship: many_to_many
  }
}

explore: slim {
  from: order_items
  fields: [ALL_FIELDS*, -slim.status]
}

explore: inner_join_example {
  view_name: orders
  join: users {
    type: inner
    sql_on: ${orders.user_id} = ${users.id} ;;
    relationship: many_to_one
  }
}
"""

ORDER_ITEMS = """
view: order_items {
  sql_table_name: main.shop.order_items ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: order_id { type: number hidden: yes sql: ${TABLE}.order_id ;; }
  dimension: status { type: string sql: ${TABLE}.status ;; }
  dimension: sale_price { type: number sql: ${TABLE}.sale_price ;; }
  dimension: is_big { type: yesno sql: ${sale_price} > 100 ;; }
  dimension: price_tier { type: tier tiers: [0, 10, 50] style: integer sql: ${sale_price} ;; }
  dimension: brand_pick {
    type: string
    sql: {% if brand._in_query %} ${TABLE}.brand {% else %} 'all' {% endif %} ;;
  }
  dimension_group: created {
    type: time
    timeframes: [raw, date, week, month, year, day_of_week]
    sql: ${TABLE}.created_at ;;
  }
  measure: count { type: count }
  measure: total_sale_price { type: sum sql: ${sale_price} ;; }
  measure: complete_sales {
    type: sum
    sql: ${sale_price} ;;
    filters: [status: "complete, shipped"]
  }
  measure: non_test_count { type: count filters: [status: "-test"] }
  measure: big_count { type: count filters: [is_big: "yes"] }
  measure: avg_price_per_item { type: number sql: 1.0 * ${total_sale_price} / NULLIF(${count}, 0) ;; }
  measure: item_list { type: list list_field: status }
}
"""

ORDERS = """
view: orders {
  sql_table_name: main.shop.orders ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: user_id { type: number sql: ${TABLE}.user_id ;; }
  dimension: shipping { type: number sql: ${TABLE}.shipping ;; }
  measure: count { type: count }
  measure: total_shipping { type: sum sql: ${shipping} ;; }
  measure: max_shipping { type: max sql: ${shipping} ;; }
}
"""

USERS = """
view: users {
  sql_table_name: main.shop.users ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: country { type: string sql: ${TABLE}.country ;; }
  measure: count { type: count }
}
"""

EVENTS = """
view: events {
  derived_table: {
    sql: SELECT user_id, event_type, ts FROM main.shop.raw_events WHERE ts > '2024-01-01' ;;
    datagroup_trigger: daily
  }
  dimension: user_id { type: number sql: ${TABLE}.user_id ;; }
  dimension: event_type { type: string sql: ${TABLE}.event_type ;; }
  measure: count { type: count_distinct sql: ${TABLE}.event_type ;; }
}
"""

TAGS = """
view: tags {
  sql_table_name: main.shop.tags ;;
  dimension: item_id { type: number sql: ${TABLE}.item_id ;; }
  dimension: tag { type: string sql: ${TABLE}.tag ;; }
}
"""

FILES = {
    "shop.model.lkml": MODEL,
    "order_items.view.lkml": ORDER_ITEMS,
    "orders.view.lkml": ORDERS,
    "users.view.lkml": USERS,
    "events.view.lkml": EVENTS,
    "tags.view.lkml": TAGS,
}


def run(files=FILES, **kw):
    out, results, project = convert(files, **kw)
    by_explore = {r.explore: r for r in results}
    return out, by_explore


def item(result, looker):
    return next(i for i in result.items if i.looker == looker)


def test_writes_valid_yaml_for_each_explore():
    out, results = run()
    for name in ("shop__order_items", "shop__slim", "shop__inner_join_example"):
        data = yaml.safe_load(out[f"metric_views/{name}.yaml"])
        assert data["version"] == "1.1"
        assert data["source"]
        names = [f["name"] for f in data.get("fields", [])] + [m["name"] for m in data.get("measures", [])]
        assert len(names) == len(set(names)), "metric view names must be unique"
        for m in data.get("measures", []):
            assert re.search(r"\b(COUNT|SUM|AVG|MIN|MAX|MEDIAN|PERCENTILE)\(|MEASURE\(", m["expr"]), m


def test_nested_join_and_paths():
    out, r = run()
    mv = r["order_items"].metric_view
    orders = next(j for j in mv["joins"] if j["name"] == "orders")
    assert orders["on"] == "source.order_id = orders.id"
    users = orders["joins"][0]
    assert users["name"] == "users" and users["on"] == "orders.user_id = users.id"
    fields = {f["name"]: f["expr"] for f in mv["fields"]}
    assert fields["users_country"] == "orders.users.country"


def test_one_to_many_join_is_nested_and_its_dimensions_are_not_fields():
    out, r = run()
    users = next(j for j in r["order_items"].metric_view["joins"] if j["name"] == "orders")["joins"][0]
    events = users["joins"][0]
    assert events["cardinality"] == "one_to_many"
    assert item(r["order_items"], "events.event_type").status == "manual"
    assert item(r["order_items"], "events.count").status == "exact"


def test_many_to_many_is_manual_and_dropped():
    out, r = run()
    assert item(r["order_items"], "tags").status == "manual"
    assert all(j["name"] != "tags" for j in r["order_items"].metric_view["joins"])


def test_fanout_on_joined_sum_needs_review_but_max_and_count_do_not():
    out, r = run()
    res = r["order_items"]
    assert item(res, "orders.total_shipping").status == "review"
    assert "symmetric aggregates" in item(res, "orders.total_shipping").reason
    assert item(res, "orders.max_shipping").status == "exact"
    measures = {m["name"]: m["expr"] for m in res.metric_view["measures"]}
    assert measures["orders_count"] == "COUNT(DISTINCT orders.id)"


def test_measure_filters_and_number_measures():
    out, r = run()
    measures = {m["name"]: m["expr"] for m in r["order_items"].metric_view["measures"]}
    assert measures["complete_sales"] == "SUM(source.sale_price) FILTER (WHERE source.status IN ('complete', 'shipped'))"
    assert measures["non_test_count"] == "COUNT(1) FILTER (WHERE source.status NOT IN ('test'))"
    assert measures["big_count"] == "COUNT(1) FILTER (WHERE (source.sale_price > 100))"
    assert measures["avg_price_per_item"] == "1.0 * MEASURE(`total_sale_price`) / NULLIF(MEASURE(`count`), 0)"


def test_liquid_list_and_hidden():
    out, r = run()
    res = r["order_items"]
    assert item(res, "order_items.brand_pick").status == "manual"
    assert "Liquid" in item(res, "order_items.brand_pick").reason
    assert item(res, "order_items.item_list").status == "manual"
    assert item(res, "order_items.order_id").status == "skipped"


def test_explore_filters():
    out, r = run()
    res = r["order_items"]
    assert res.metric_view["filter"] == "source.status <> 'test'"
    assert item(res, "always_filter").status == "manual"


def test_timeframes_and_tiers():
    out, r = run()
    fields = {f["name"]: f["expr"] for f in r["order_items"].metric_view["fields"]}
    assert fields["created_week"] == "TO_DATE(DATE_TRUNC('WEEK', source.created_at))"
    assert fields["created_month"] == "TO_DATE(DATE_TRUNC('MONTH', source.created_at))"
    assert fields["created_day_of_week"] == "DATE_FORMAT(source.created_at, 'EEEE')"
    assert fields["price_tier"].startswith("CASE WHEN source.sale_price < 0 THEN 'Below 0'")
    assert item(r["order_items"], "order_items.created_month").status == "adapted"


def test_derived_table_becomes_sql_source():
    out, r = run()
    users = next(j for j in r["order_items"].metric_view["joins"] if j["name"] == "orders")["joins"][0]
    events = users["joins"][0]
    assert events["source"].startswith("SELECT user_id, event_type, ts FROM main.shop.raw_events")
    assert "materialized view" in item(r["order_items"], "events").reason


def test_explore_fields_exclusion_and_from():
    out, r = run()
    res = r["slim"]
    assert res.metric_view["source"] == "main.shop.order_items"
    assert not any(f["name"] == "status" for f in res.metric_view["fields"])


def test_inner_join_flagged():
    out, r = run()
    assert item(r["inner_join_example"], "users").status == "review"


def test_bigquery_snippets_are_transpiled_but_templates_are_not():
    files = {
        "m.model.lkml": "explore: t {}",
        "t.view.lkml": """
view: t {
  sql_table_name: `proj.ds.t` ;;
  dimension: ratio { type: number sql: SAFE_DIVIDE(${TABLE}.a, ${TABLE}.b) ;; }
  dimension_group: ts { type: time timeframes: [week] sql: TIMESTAMP_SECONDS(${TABLE}.epoch) ;; }
  measure: total { type: sum sql: ${ratio} ;; }
}""",
    }
    out, r = run(files, dialect="bigquery", table_map={"proj.ds.*": "main.ds.*"})
    mv = r["t"].metric_view
    assert mv["source"] == "main.ds.t"
    fields = {f["name"]: f["expr"] for f in mv["fields"]}
    assert "SAFE_DIVIDE" not in fields["ratio"]
    assert "source.a" in fields["ratio"] and "source.b" in fields["ratio"]
    assert fields["ts_week"].startswith("TO_DATE(DATE_TRUNC('WEEK'")


def test_extends_and_refinements():
    files = {
        "m.model.lkml": "explore: child {}",
        "v.view.lkml": """
view: base { extension: required dimension: a { sql: ${TABLE}.a ;; } measure: n { type: count } }
view: child { extends: [base] sql_table_name: main.x.child ;; dimension: b { sql: ${TABLE}.b ;; } }
view: +child { dimension: c { sql: ${TABLE}.c ;; } }
""",
    }
    out, r = run(files)
    names = {f["name"] for f in r["child"].metric_view["fields"]}
    assert {"a", "b", "c"} <= names


def test_bad_file_does_not_stop_the_run():
    files = dict(FILES)
    files["broken.view.lkml"] = "view: broken { dimension: x { sql: ;; "
    out, r = run(files)
    assert "order_items" in r
    assert "broken.view.lkml" in out["report.md"]


def test_read_folder_works_inside_hidden_parent(tmp_path):
    from lookml_metric_views.cli import read_folder
    project = tmp_path / ".cache" / "proj"
    (project / ".git").mkdir(parents=True)
    (project / "a.view.lkml").write_text("view: a {}")
    (project / ".git" / "b.view.lkml").write_text("view: b {}")
    files = read_folder(str(project))
    assert list(files) == ["a.view.lkml"]


def test_correlated_subquery_on_base_table_is_manual_but_on_join_is_review():
    files = {
        "m.model.lkml": """
explore: orders {}
explore: items { join: orders { sql_on: ${items.order_id} = ${orders.id} ;; relationship: many_to_one } }
""",
        "v.view.lkml": """
view: orders {
  sql_table_name: main.s.orders ;;
  dimension: id { primary_key: yes sql: ${TABLE}.id ;; }
  dimension: seq { type: number sql: (SELECT COUNT(*) FROM main.s.orders o WHERE o.id < ${TABLE}.id) ;; }
}
view: items {
  sql_table_name: main.s.items ;;
  dimension: order_id { sql: ${TABLE}.order_id ;; }
}
""",
    }
    out, r = run(files)
    assert item(r["orders"], "orders.seq").status == "manual"
    assert item(r["items"], "orders.seq").status == "review"


def test_derived_tables_building_on_each_other_are_inlined():
    files = {
        "m.model.lkml": "explore: summary {}",
        "v.view.lkml": """
view: base_pdt { derived_table: { sql: SELECT user_id, amount FROM main.s.payments ;; } }
view: summary {
  derived_table: { sql: SELECT user_id, SUM(amount) AS total FROM ${base_pdt.SQL_TABLE_NAME} GROUP BY 1 ;; }
  dimension: user_id { sql: ${TABLE}.user_id ;; }
  measure: total { type: sum sql: ${TABLE}.total ;; }
}
""",
    }
    out, r = run(files)
    src = r["summary"].metric_view["source"]
    assert "FROM (SELECT user_id, amount FROM main.s.payments)" in src
    assert "base_pdt" in item(r["summary"], "summary").reason


def test_blocked_explore_counts_its_fields_as_manual():
    files = {
        "m.model.lkml": "explore: x {}",
        "v.view.lkml": "view: x { derived_table: { sql: SELECT {% parameter p %} AS a ;; } dimension: a {} measure: n { type: count } }",
    }
    out, r = run(files)
    assert item(r["x"], "x.a").status == "manual"
    assert item(r["x"], "x.n").status == "manual"


def test_dashboard_files_are_ignored():
    files = dict(FILES)
    files["dashboards/overview.dashboard.lookml"] = "- dashboard: overview\n  title: Overview\n"
    out, r = run(files)
    assert "overview.dashboard" not in out["report.md"]


def test_manifest_constants_are_substituted_and_reported():
    files = {
        "manifest.lkml": 'project_name: "p"\nconstant: schema { value: "ads_export" }',
        "m.model.lkml": "explore: x {}",
        "v.view.lkml": "view: x { sql_table_name: @{schema}.events ;; dimension: a {} }",
    }
    out, r = run(files)
    assert r["x"].metric_view["source"] == "ads_export.events"
    assert "schema" in out["report.md"]


def test_undefined_constant_and_unparsable_derived_sql_are_manual_not_a_crash():
    files = {
        "m.model.lkml": "explore: x {}\nexplore: y {}",
        "v.view.lkml": ("view: x { derived_table: { sql: SELECT a FROM @{missing}.t ;; } dimension: a {} }\n"
                        "view: y { derived_table: { sql: SELECT * FROM (-- comment) LIMIT 1 ;; } dimension: a {} }"),
    }
    _, results, _ = convert(files, dialect="bigquery")
    by_name = {res.explore: res for res in results}
    assert "constant" in item(by_name["x"], "x").reason
    assert item(by_name["y"], "y").status == "manual"
    assert all("Unexpected error" not in it.reason for res in results for it in res.items)


def test_fine_and_bucketed_timeframes():
    files = {
        "m.model.lkml": "explore: x {}",
        "v.view.lkml": ("view: x { sql_table_name: main.s.x ;; dimension_group: t { type: time "
                        "timeframes: [microsecond, minute15, time_of_day, fiscal_year] sql: ${TABLE}.ts ;; } }"),
    }
    out, r = run(files)
    assert item(r["x"], "x.t_microsecond").status == "exact"
    assert item(r["x"], "x.t_minute15").status == "adapted"
    assert "900" in out["metric_views/m__x.yaml"]
    assert "fiscal_month_offset" in item(r["x"], "x.t_fiscal_year").reason


def test_number_measure_can_use_a_measure_of_a_joined_view():
    files = {
        "m.model.lkml": "explore: items { join: orders { sql_on: ${items.order_id} = ${orders.id} ;; relationship: many_to_one } }",
        "v.view.lkml": ("view: items { sql_table_name: main.s.items ;; dimension: order_id {} measure: n { type: count }\n"
                        "  measure: per_order { type: number sql: ${n} / NULLIF(${orders.order_count}, 0) ;; } }\n"
                        "view: orders { sql_table_name: main.s.orders ;; dimension: id { primary_key: yes } "
                        "measure: order_count { type: count } }"),
    }
    out, r = run(files)
    assert item(r["items"], "items.per_order").status != "manual"
    assert "MEASURE(`orders_order_count`)" in out["metric_views/m__items.yaml"]


def test_generated_names_never_collide():
    files = {
        "m.model.lkml": "explore: x { join: y { sql_on: ${x.id} = ${y.id} ;; relationship: many_to_one } }",
        "v.view.lkml": ("view: x { sql_table_name: main.s.x ;; dimension: id {} dimension: y_id_2 {} }\n"
                        "view: y { sql_table_name: main.s.y ;; dimension: id {} }"),
    }
    out, r = run(files)
    names = [f["name"] for f in yaml.safe_load(out["metric_views/m__x.yaml"])["fields"]]
    assert len(names) == len(set(names))
