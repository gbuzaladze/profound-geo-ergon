# Azure Deployment Plan

Status: Ready for Validation

## Goal

Deploy the existing Python pipeline as a scheduled Azure Durable Function while
preserving the current CLI and CSV workflow.

## Confirmed behavior

- Run daily at 6:00 AM America/Toronto.
- Keep the full historical scores refresh.
- Pull citations incrementally using Azure SQL as the cloud watermark.
- Replace the complete citations table in SQL after a successful incremental pull.
- Do not generate CSV files from Azure Functions.
- Preserve existing CLI commands and CSV output behavior.

## Planned architecture

- Python 3.12 Azure Functions v4 on Flex Consumption.
- Durable Functions with Durable Task Scheduler.
- User-assigned managed identity for Azure SQL and Azure resources.
- Key Vault for the Profound API key.
- Application Insights and Log Analytics for monitoring.
- Existing Azure SQL database; no replacement database.

## Work plan

1. Complete: separate extraction/transformation from CSV and SQL output adapters.
2. Complete: add SQL-backed citation watermark and safe full-table replacement.
3. Complete: add timer, orchestrator, and sequential country activities.
4. Complete: add Azure configuration and Bicep infrastructure.
5. Complete: add tests and local Python verification.
6. Pending: validate Bicep and run Azure preflight before deployment.

## Infrastructure recipe

- Recipe: Azure Developer CLI with Bicep.
- Compute: Linux Flex Consumption Function App, Python 3.12.
- Orchestration: Durable Functions backed by Durable Task Scheduler.
- Identity: user-assigned managed identity.
- Secrets: Key Vault reference for `PROFOUND_API_KEY`.
- Data: existing Azure SQL database with managed-identity authentication.
- Monitoring: workspace-based Application Insights and Log Analytics.
- Function host storage: identity-authenticated StorageV2 account.

## Validation steps

1. Run Python unit tests and compilation.
2. Verify the Functions decorator model indexes all three functions.
3. Compile `infra/main.bicep`.
4. Run `azd provision --preview`.
5. Verify static RBAC assignments for host storage, Key Vault, Application
   Insights, Durable Task Scheduler, and deployment-package upload.
6. Confirm existing Azure SQL networking and create the managed-identity
   database user before enabling the schedule.

## Azure context required before deployment

- Subscription and tenant
- Deployment region
- Existing SQL server and database resource IDs
- Microsoft Entra administrator for Azure SQL
- Target resource-group naming

## Validation proof

- Passed: `uv run python -m unittest discover -s tests -v` (9 tests).
- Passed: `uv run python -m compileall function_app.py src tests`.
- Passed: `uv build` produced the source distribution and wheel.
- Passed: JSON parsing for `host.json`, local settings, and Bicep parameters.
- Passed: Function metadata indexes `scheduled_pipeline`,
  `pipeline_orchestrator`, and `run_country_activity`.
- Passed: IDE diagnostics report no errors in changed Python files.
- Pending: Bicep compilation and Azure preflight; Azure CLI, Azure Developer
  CLI, and Functions Core Tools are not installed on this workstation.
