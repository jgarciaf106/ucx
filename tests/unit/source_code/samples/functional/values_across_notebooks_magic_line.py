# Databricks notebook source

%run ./values_across_notebooks_child.py

# COMMAND ----------

# ucx[table-migrate:+1:0:+1:19] The default format changed in Databricks Runtime 8.0, from Parquet to Delta
spark.table(f"{a}")
