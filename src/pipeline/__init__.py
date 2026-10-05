"""Profound visibility extract for one or more countries.

Each market in `project.toml` writes CSVs under `data/{slug}/` and, when Azure
SQL is configured, replaces matching tables in schema `{slug}`.
"""
