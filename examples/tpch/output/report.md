# LookML to metric views: translation report

Source: examples/tpch

2 explores, 2 metric views written.

| Status | Fields, measures and joins | Meaning |
|---|---|---|
| Exact | 45 | Same meaning, mechanical translation |
| Adapted | 5 | Same meaning, value shown differently |
| Review | 1 | Translated, but numbers can differ from Looker |
| Manual | 0 | Not translated, needs a person |
| Skipped | 8 | Hidden in Looker, not exposed |

Not checked by this tool: Looker connection settings. If your connection converts timestamps to the user's timezone, time fields can shift (run again with --timezone-conversion to flag them).

50 of 51 items (98%) translate without a person checking them.

## What needs a person most often

- Review (1): Looker computes this over distinct rows of the joined view (symmetric aggregates)

## Explore `lineitem` (model `tpch`)

Metric view: `tpch__lineitem`

| Looker | Kind | Status | Metric view name | Note |
|---|---|---|---|---|
| `lineitem` | source | Exact |  |  |
| `orders` | join | Exact | `orders` |  |
| `customer` | join | Exact | `customer` |  |
| `nation` | join | Exact | `nation` |  |
| `sql_always_where` | filter | Exact |  | Becomes the metric view filter, applied to every query. |
| `lineitem.line_key` | field | Skipped |  | Hidden in Looker; still used inside other definitions. |
| `lineitem.l_orderkey` | field | Skipped |  | Hidden in Looker; still used inside other definitions. |
| `lineitem.return_flag` | field | Exact | `return_flag` |  |
| `lineitem.ship_mode` | field | Exact | `ship_mode` |  |
| `lineitem.quantity` | field | Exact | `quantity` |  |
| `lineitem.quantity_tier` | field | Adapted | `quantity_tier` | Tiers become a CASE expression; Looker's sort order of tiers is not kept. |
| `lineitem.net_revenue` | field | Exact | `net_revenue` |  |
| `lineitem.is_discounted` | field | Adapted | `is_discounted` | Boolean field; Looker displays it as Yes/No. |
| `lineitem.l_shipdate_raw` | field | Exact | `l_shipdate_raw` |  |
| `lineitem.l_shipdate_date` | field | Exact | `l_shipdate_date` |  |
| `lineitem.l_shipdate_month` | field | Adapted | `l_shipdate_month` | Looker shows 'YYYY-MM'; here it is the first day of the month. |
| `lineitem.l_shipdate_year` | field | Exact | `l_shipdate_year` |  |
| `orders.o_orderkey` | field | Exact | `orders_o_orderkey` |  |
| `orders.o_custkey` | field | Skipped |  | Hidden in Looker; still used inside other definitions. |
| `orders.status` | field | Exact | `orders_status` |  |
| `orders.priority` | field | Exact | `orders_priority` |  |
| `orders.total_price` | field | Exact | `orders_total_price` |  |
| `orders.ordered_date` | field | Exact | `orders_ordered_date` |  |
| `orders.ordered_month` | field | Adapted | `orders_ordered_month` | Looker shows 'YYYY-MM'; here it is the first day of the month. |
| `orders.ordered_year` | field | Exact | `orders_ordered_year` |  |
| `customer.c_custkey` | field | Exact | `customer_c_custkey` |  |
| `customer.c_nationkey` | field | Skipped |  | Hidden in Looker; still used inside other definitions. |
| `customer.segment` | field | Exact | `customer_segment` |  |
| `nation.n_nationkey` | field | Skipped |  | Hidden in Looker; still used inside other definitions. |
| `nation.name` | field | Exact | `nation_name` |  |
| `lineitem.line_count` | measure | Exact | `line_count` |  |
| `lineitem.total_quantity` | measure | Exact | `total_quantity` |  |
| `lineitem.total_net_revenue` | measure | Exact | `total_net_revenue` |  |
| `lineitem.returned_revenue` | measure | Exact | `returned_revenue` |  |
| `lineitem.discounted_lines` | measure | Exact | `discounted_lines` |  |
| `orders.order_count` | measure | Exact | `orders_order_count` |  |
| `orders.total_order_value` | measure | Review | `orders_total_order_value` | Looker computes this over distinct rows of the joined view (symmetric aggregates); here it counts once per row of the explore's base view. Define it in a metric view based on this view, or use a one_to_many design. |
| `orders.max_order_value` | measure | Exact | `orders_max_order_value` |  |
| `orders.urgent_orders` | measure | Exact | `orders_urgent_orders` |  |
| `customer.customer_count` | measure | Exact | `customer_customer_count` |  |
| `lineitem.return_rate` | measure | Exact | `return_rate` |  |

## Explore `orders` (model `tpch`)

Metric view: `tpch__orders`

| Looker | Kind | Status | Metric view name | Note |
|---|---|---|---|---|
| `orders` | source | Exact |  |  |
| `customer` | join | Exact | `customer` |  |
| `nation` | join | Exact | `nation` |  |
| `orders.o_orderkey` | field | Exact | `o_orderkey` |  |
| `orders.o_custkey` | field | Skipped |  | Hidden in Looker; still used inside other definitions. |
| `orders.status` | field | Exact | `status` |  |
| `orders.priority` | field | Exact | `priority` |  |
| `orders.total_price` | field | Exact | `total_price` |  |
| `orders.ordered_date` | field | Exact | `ordered_date` |  |
| `orders.ordered_month` | field | Adapted | `ordered_month` | Looker shows 'YYYY-MM'; here it is the first day of the month. |
| `orders.ordered_year` | field | Exact | `ordered_year` |  |
| `customer.c_custkey` | field | Exact | `customer_c_custkey` |  |
| `customer.c_nationkey` | field | Skipped |  | Hidden in Looker; still used inside other definitions. |
| `customer.segment` | field | Exact | `customer_segment` |  |
| `nation.n_nationkey` | field | Skipped |  | Hidden in Looker; still used inside other definitions. |
| `nation.name` | field | Exact | `nation_name` |  |
| `orders.order_count` | measure | Exact | `order_count` |  |
| `orders.total_order_value` | measure | Exact | `total_order_value` |  |
| `orders.max_order_value` | measure | Exact | `max_order_value` |  |
| `orders.urgent_orders` | measure | Exact | `urgent_orders` |  |
| `customer.customer_count` | measure | Exact | `customer_customer_count` |  |
