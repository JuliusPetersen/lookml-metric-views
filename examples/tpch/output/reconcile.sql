-- Compare these totals with the same measures in Looker before switching anything off.

-- lineitem: overall totals. In Looker, run the explore with the same measures and no filters.
SELECT
  MEASURE(`line_count`) AS `line_count`,
  MEASURE(`total_quantity`) AS `total_quantity`,
  MEASURE(`total_net_revenue`) AS `total_net_revenue`,
  MEASURE(`returned_revenue`) AS `returned_revenue`,
  MEASURE(`discounted_lines`) AS `discounted_lines`,
  MEASURE(`orders_order_count`) AS `orders_order_count`,
  MEASURE(`orders_total_order_value`) AS `orders_total_order_value`,
  MEASURE(`orders_max_order_value`) AS `orders_max_order_value`,
  MEASURE(`orders_urgent_orders`) AS `orders_urgent_orders`,
  MEASURE(`customer_customer_count`) AS `customer_customer_count`,
  MEASURE(`return_rate`) AS `return_rate`
FROM workspace.lookml_test.tpch__lineitem;

-- lineitem: by `l_shipdate_month`. Compare row by row with Looker.
SELECT
  `l_shipdate_month`,
  MEASURE(`line_count`) AS `line_count`,
  MEASURE(`total_quantity`) AS `total_quantity`,
  MEASURE(`total_net_revenue`) AS `total_net_revenue`,
  MEASURE(`returned_revenue`) AS `returned_revenue`,
  MEASURE(`discounted_lines`) AS `discounted_lines`,
  MEASURE(`orders_order_count`) AS `orders_order_count`,
  MEASURE(`orders_total_order_value`) AS `orders_total_order_value`,
  MEASURE(`orders_max_order_value`) AS `orders_max_order_value`,
  MEASURE(`orders_urgent_orders`) AS `orders_urgent_orders`,
  MEASURE(`customer_customer_count`) AS `customer_customer_count`,
  MEASURE(`return_rate`) AS `return_rate`
FROM workspace.lookml_test.tpch__lineitem
GROUP BY ALL
ORDER BY 1;

-- orders: overall totals. In Looker, run the explore with the same measures and no filters.
SELECT
  MEASURE(`order_count`) AS `order_count`,
  MEASURE(`total_order_value`) AS `total_order_value`,
  MEASURE(`max_order_value`) AS `max_order_value`,
  MEASURE(`urgent_orders`) AS `urgent_orders`,
  MEASURE(`customer_customer_count`) AS `customer_customer_count`
FROM workspace.lookml_test.tpch__orders;

-- orders: by `ordered_month`. Compare row by row with Looker.
SELECT
  `ordered_month`,
  MEASURE(`order_count`) AS `order_count`,
  MEASURE(`total_order_value`) AS `total_order_value`,
  MEASURE(`max_order_value`) AS `max_order_value`,
  MEASURE(`urgent_orders`) AS `urgent_orders`,
  MEASURE(`customer_customer_count`) AS `customer_customer_count`
FROM workspace.lookml_test.tpch__orders
GROUP BY ALL
ORDER BY 1;
