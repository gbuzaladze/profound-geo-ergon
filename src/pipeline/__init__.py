"""Profound visibility extract for one or more categories.

Each category is written under `data/{slug}/` during a pull, then concatenated
into `data/` and the category folder is removed. Country is the market
column, and region is the screenshot grouping beside it. When Azure SQL is
configured, both categories replace their own countries in schema `dbo`.
"""
