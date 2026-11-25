view: lineitem {
  sql_table_name: samples.tpch.lineitem ;;

  dimension: line_key {
    primary_key: yes
    hidden: yes
    sql: CONCAT(${TABLE}.l_orderkey, '-', ${TABLE}.l_linenumber) ;;
  }
  dimension: l_orderkey { type: number hidden: yes sql: ${TABLE}.l_orderkey ;; }
  dimension: return_flag { type: string sql: ${TABLE}.l_returnflag ;; }
  dimension: ship_mode { type: string sql: ${TABLE}.l_shipmode ;; }
  dimension: quantity { type: number sql: ${TABLE}.l_quantity ;; }
  dimension: quantity_tier { type: tier tiers: [10, 25, 40] style: integer sql: ${quantity} ;; }
  dimension: net_revenue { type: number sql: ${TABLE}.l_extendedprice * (1 - ${TABLE}.l_discount) ;; }
  dimension: is_discounted { type: yesno sql: ${TABLE}.l_discount > 0 ;; }

  dimension_group: l_shipdate {
    label: "Ship"
    type: time
    timeframes: [raw, date, month, year]
    datatype: date
    sql: ${TABLE}.l_shipdate ;;
  }

  measure: line_count { type: count }
  measure: total_quantity { type: sum sql: ${quantity} ;; }
  measure: total_net_revenue { type: sum sql: ${net_revenue} ;; value_format_name: usd }
  measure: returned_revenue {
    type: sum
    sql: ${net_revenue} ;;
    filters: [return_flag: "R"]
  }
  measure: discounted_lines { type: count filters: [is_discounted: "yes"] }
  measure: return_rate {
    type: number
    sql: 1.0 * ${returned_revenue} / NULLIF(${total_net_revenue}, 0) ;;
  }
}

view: orders {
  sql_table_name: samples.tpch.orders ;;

  dimension: o_orderkey { primary_key: yes type: number sql: ${TABLE}.o_orderkey ;; }
  dimension: o_custkey { type: number hidden: yes sql: ${TABLE}.o_custkey ;; }
  dimension: status { type: string sql: ${TABLE}.o_orderstatus ;; }
  dimension: priority { type: string sql: ${TABLE}.o_orderpriority ;; }
  dimension: total_price { type: number sql: ${TABLE}.o_totalprice ;; }

  dimension_group: ordered {
    type: time
    timeframes: [date, month, year]
    datatype: date
    sql: ${TABLE}.o_orderdate ;;
  }

  measure: order_count { type: count }
  measure: total_order_value {
    description: "Sum of order totals. In the lineitem explore Looker sums each order once."
    type: sum
    sql: ${total_price} ;;
  }
  measure: max_order_value { type: max sql: ${total_price} ;; }
  measure: urgent_orders { type: count filters: [priority: "1-URGENT"] }
}

view: customer {
  sql_table_name: samples.tpch.customer ;;

  dimension: c_custkey { primary_key: yes type: number sql: ${TABLE}.c_custkey ;; }
  dimension: c_nationkey { type: number hidden: yes sql: ${TABLE}.c_nationkey ;; }
  dimension: segment { type: string sql: ${TABLE}.c_mktsegment ;; }

  measure: customer_count { type: count }
}

view: nation {
  sql_table_name: samples.tpch.nation ;;

  dimension: n_nationkey { primary_key: yes type: number hidden: yes sql: ${TABLE}.n_nationkey ;; }
  dimension: name { type: string sql: ${TABLE}.n_name ;; }
}
