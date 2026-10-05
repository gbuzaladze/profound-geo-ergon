"""Profound visibility extract for one or more categories.

Each category is written under `data/{slug}/` during a pull, then concatenated
into `data/` and the category folder is removed. Country is the `region`
column. When Azure SQL is configured, matching tables are replaced in schema
`{slug}`.
"""
