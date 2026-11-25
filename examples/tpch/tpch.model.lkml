# Example LookML project over the TPC-H sample data that every Databricks workspace has
# (samples.tpch). Written for this repository to test the converter end to end; it mirrors
# common patterns in real Looker projects: nested many_to_one joins, a measure on a joined
# view that fans out, filtered measures, ratios, tiers, yes/no fields and time groups.

connection: "databricks"
include: "*.view.lkml"

explore: lineitem {
  label: "Order lines"
  description: "One row per order line, with order, customer and nation."
  sql_always_where: ${lineitem.l_shipdate_raw} < DATE'1998-09-01' ;;

  join: orders {
    sql_on: ${lineitem.l_orderkey} = ${orders.o_orderkey} ;;
    relationship: many_to_one
  }
  join: customer {
    sql_on: ${orders.o_custkey} = ${customer.c_custkey} ;;
    relationship: many_to_one
  }
  join: nation {
    sql_on: ${customer.c_nationkey} = ${nation.n_nationkey} ;;
    relationship: many_to_one
  }
}

explore: orders {
  join: customer {
    sql_on: ${orders.o_custkey} = ${customer.c_custkey} ;;
    relationship: many_to_one
  }
  join: nation {
    sql_on: ${customer.c_nationkey} = ${nation.n_nationkey} ;;
    relationship: many_to_one
  }
}
