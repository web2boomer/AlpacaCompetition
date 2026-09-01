# Business Performance Protocol v1

Mission Control pulls compact business-performance reports from every configured project
endpoint. The protocol is for observability: it lets a human see operational and commercial
performance together. It is not a general ledger, invoice store, tax record, or replacement for
the source accounting system.

V1 is pull-based. Mission Control owns cadence and retries. Each product keeps its accounting
definitions and credentials local, exposes one authenticated report endpoint, and never shares
database, broker, payment-provider, or internal-API credentials with Mission Control.
Transaction-by-transaction webhooks are intentionally out of scope. Product endpoints are
implemented from this repository against this specification.

## Delivery contract

Mission Control `GET`s each enabled project's `business_reporting.endpoint` on that project's
`expected_interval_minutes` cadence.

- Authenticate with `Authorization: Bearer <project-token>`. Mission Control stores each
  distinct token in `BUSINESS_PROJECT_TOKENS_JSON` and presents it when polling. The product
  validates that token before returning data. `EVENT_SECRET` and the legacy
  `X-Mission-Control-Token` header are heartbeat-only and must not unlock the report endpoint.
- Return `Content-Type: application/json` over HTTPS.
- Set `schema_version` to `1.0`.
- Give every logical report a stable `event_id`. A poll that repeats the same ID is recorded
  once; later identical pulls are treated as already delivered.
- The payload's `project` must equal the YAML slug Mission Control is polling.
- Use timezone-aware ISO 8601 timestamps. Periods are half-open: `period_start` is inclusive and
  `period_end` is exclusive.
- Encode decimal values as JSON strings, not binary floating-point numbers.
- Return `estimated` reports during a period and a new `final` report after close. Mission
  Control displays the latest report and does not mutate historical reports.
- HTTP 204 means the product has no reportable data yet. Mission Control leaves the pulse
  `NOT CONNECTED` and does not treat that as a failure.
- HTTP 503 may include `Retry-After` in seconds. Mission Control waits that long before the next
  poll.
- Mission Control marks the feed stale after `expected_interval_minutes` plus `grace_minutes`,
  based on server receipt time.

`POST /v1/business/reports` remains as a compatibility ingest path during migration. New
adapters implement the GET endpoint instead of pushing.

## Product endpoint

```
GET /internal/mission_control/report
Authorization: Bearer <project-token>
Accept: application/json
```

The response body is this protocol's report object — the same payload previously POSTed. The
authoritative machine-readable contract is also served by Mission Control at
`GET /v1/business/schema` and stored in
[`protocol/business-report-v1.schema.json`](../protocol/business-report-v1.schema.json).
Python services can copy
[`integrations/python/mission_control_business.py`](../integrations/python/mission_control_business.py)
for payload helpers. Ruby services can use the
[`mission_control_reporter`](../integrations/ruby/mission_control_reporter) gem's `Report#to_json`
as the GET body.


## Metric semantics

Every measurement has a `unit` and a `kind`:

| Kind | Meaning | Examples |
|---|---|---|
| `flow` | Accumulated during the report period | revenue, profit, orders, realized gains |
| `balance` | Point-in-time value at `period_end` | cash, receivables, portfolio value |
| `gauge` | Current rate or non-ledger level | MRR, active customers, churn rate |

Standard names and required semantics:

| Names | Unit | Kind |
|---|---|---|
| `revenue`, `gross_profit`, `net_profit`, `operating_expenses`, `cost_of_goods_sold`, `taxes`, `net_cash_flow`, `realized_gains`, `losses` | currency | flow |
| `unrealized_gains`, `cash_balance`, `accounts_receivable`, `portfolio_value`, `assets_under_management` | currency | balance |
| `mrr`, `arr` | currency | gauge |
| `new_customers`, `churned_customers`, `orders` | count | flow |
| `customers`, `active_customers` | count | gauge |
| `churn_rate`, `conversion_rate`, `return_percent` | percent | gauge |

Project-specific metrics must start with `x_`, for example `x_predictions_settled`. This reserves
the unprefixed namespace for future shared protocol fields. Keep custom names stable once shipped.

Do not use the standard `operating_expenses` metric for vendor or run-rate operating costs. That
name is a P&L flow. Recurring and usage costs use the `x_` metrics and `metadata.costs_v1`
overlay in [Operating costs (costs_v1)](#operating-costs-costs_v1).

## Example

```json
{
  "schema_version": "1.0",
  "event_id": "money-monster-business-2026-08-28-final",
  "project": "money-monster",
  "environment": "production",
  "occurred_at": "2026-08-29T00:05:00Z",
  "period_start": "2026-08-28T00:00:00Z",
  "period_end": "2026-08-29T00:00:00Z",
  "reporting_basis": "operational",
  "report_status": "final",
  "currency": "USD",
  "metrics": [
    {"name": "realized_gains", "value": "1842.37", "unit": "currency", "kind": "flow"},
    {"name": "portfolio_value", "value": "92144.80", "unit": "currency", "kind": "balance"},
    {"name": "return_percent", "value": "2.04", "unit": "percent", "kind": "gauge"}
  ],
  "metadata": {"source": "daily-close"}
}
```

The authoritative machine-readable contract is also served by Mission Control at
`GET /v1/business/schema` and stored in
[`protocol/business-report-v1.schema.json`](../protocol/business-report-v1.schema.json).
Python services can copy
[`integrations/python/mission_control_business.py`](../integrations/python/mission_control_business.py)
for payload helpers. Ruby services can use the
[`mission_control_reporter`](../integrations/ruby/mission_control_reporter) gem's `Report#to_json`
as the GET response body.

## Operating costs (costs_v1)

This overlay is optional and backward compatible. Keep `schema_version` at `"1.0"`. Omit the cost
metrics and `metadata.costs_v1` when the project has no operating costs to report. Mission Control
accepts reports that omit them and does not reject a report for malformed cost line items; bad
items are ignored.

The originating design note is
[`docs/specs/mission-control-recurring-costs-payload.md`](./specs/mission-control-recurring-costs-payload.md).
This section is the producer contract.

### Cost metrics

When any cost is reported, include `x_operating_cost_total`. The split metrics are recommended.
All three are decimal strings with `unit=currency` and `kind=flow`:

| Name | Meaning |
|---|---|
| `x_operating_cost_total` | Total operating cost for the report period. Required when any cost is reported. |
| `x_recurring_cost_total` | Recurring component only. |
| `x_usage_cost_total` | Usage-based component only. |

### Line items

Set `metadata.costs_contract_version` to `"v1"` and send `metadata.costs_v1` as an array of line
items. Keep `cost_key`s stable once shipped. Line items must fit in the existing 16 KiB metadata
cap.

| Field | Meaning |
|---|---|
| `cost_key` | Stable machine key per line item. |
| `display_name` | Human label for the dashboard. |
| `category` | `data_vendor`, `llm_usage`, `infra`, `observability`, or `other`. |
| `type` | `recurring` or `usage`. |
| `status` | `active`, `cancelled`, `trial`, or `paused`. |
| `amount_usd_monthly` | Normalized monthly USD amount as a decimal string. |
| `currency` | ISO 4217 code; `USD` in v1. |
| `cadence` | `monthly`, `annual_prorated`, `daily_prorated`, or `variable`. |
| `proration_rule` | Short parseable rule (`exact`, `annual/12`, and similar). |
| `effective_start` / `effective_end` | Service-local dates (`YYYY-MM-DD`). `effective_end` is null while open. |
| `source_confidence` | `confirmed`, `estimated`, or `list_price`. |
| `source_type` | `receipt`, `invoice`, `database`, `estimate`, or `manual`. |
| `evidence_ref` | Pointer string only. Never include secrets or account numbers. |
| `notes` | Optional short explanation. |

### Aggregation

Mission Control includes a line item in period totals when:

- `status` is `active`
- `effective_start` is on or before `period_end`
- `effective_end` is null or on or after `period_start`

`cancelled` items contribute `0` after their `effective_end`. Prefer the producer-provided
`x_operating_cost_total`. If that metric is missing, sum in-period `costs_v1` amounts. Recurring
and usage totals stay separate; the combined total is their sum when the producer total is absent.

### Estimated vs final

- **Usage:** period actuals — month-to-date for `estimated`, the full closed month for `final`.
- **Recurring:** monthly run-rate for items active in the period.

V1 has no confirmed-only dashboard toggle. Send mixed `source_confidence` values when that is
truthful; Mission Control shows a `confirmed` or `mixed` badge. Shared-infra `allocation_percent`
is deferred to a later contract version.

### Secrets and size

Do not send account numbers, full invoice PDFs, payment tokens, or other secrets. `evidence_ref`
is a pointer (for example `gmail:thread:…` or `answers.cost:2026-08`). Treat cancelled vendors
explicitly with `status=cancelled` and a bounded effective range rather than omitting them.

### Net after costs

Mission Control derives net after costs as the first present of `x_fund_gain`, `net_profit`,
or `revenue`, minus `x_operating_cost_total`. The dashboard hides that figure unless both sides
are present.

### Example

```json
{
  "report_status": "estimated",
  "metrics": [
    { "name": "x_fund_gain", "value": "1234.56", "unit": "currency", "kind": "flow" },
    { "name": "x_operating_cost_total", "value": "520.09", "unit": "currency", "kind": "flow" },
    { "name": "x_recurring_cost_total", "value": "377.32", "unit": "currency", "kind": "flow" },
    { "name": "x_usage_cost_total", "value": "143.77", "unit": "currency", "kind": "flow" }
  ],
  "metadata": {
    "source": "persisted-local-data",
    "costs_contract_version": "v1",
    "costs_v1": [
      {
        "cost_key": "openai_usage",
        "display_name": "OpenAI",
        "category": "llm_usage",
        "type": "usage",
        "status": "active",
        "amount_usd_monthly": "143.77",
        "currency": "USD",
        "cadence": "variable",
        "proration_rule": "exact",
        "effective_start": "2026-08-01",
        "effective_end": null,
        "source_confidence": "confirmed",
        "source_type": "database",
        "evidence_ref": "answers.cost:2026-08"
      }
    ]
  }
}
```

### Project mappings

Configured business-reporting projects (`money-monster`, `prophecy`, `treaty`,
`alpaca-competition`) follow this contract. Omit the overlay until the project has authoritative
local sources for operating costs.

MoneyMonster's initial `cost_key`s:

| `cost_key` | Notes |
|---|---|
| `openai_usage` | `type=usage`, from `answers.cost` monthly sum |
| `anthropic_usage` | `type=usage`, from `answers.cost` monthly sum |
| `render_infra_mm` | Recurring or usage by policy; currently an estimated MoneyMonster slice |
| `betterstack_logs` | Recurring |
| `earningscall_starter` | Recurring |
| `quiver_trader` | Recurring |
| `marketdata_trader` | Recurring |
| `fmp_starter` | Recurring; `list_price` confidence until a receipt feed exists |
| `quartr_live_transcripts` | `status=cancelled`; monthly amount `0` after cancel |

## Integration checklist

For a full step-by-step guide to wiring a new product repository (environment variables, adapter
structure, dry-run/rollout), see
[`reporting-project-onboarding.md`](./reporting-project-onboarding.md).

1. Add the project's `business_reporting.endpoint` to Mission Control's `config/projects.yaml`.
2. Store that project's distinct token in Mission Control's `BUSINESS_PROJECT_TOKENS_JSON` and in
   the product's `MISSION_CONTROL_TOKEN`. The product uses the token only to authenticate incoming
   Mission Control polls.
3. Produce values from the project's authoritative database or accounting API. Do not scrape a
   dashboard or derive financial values from formatted UI text.
4. Serve the current report at `GET /internal/mission_control/report` using a deterministic
   `event_id` such as `<project>-business-<period-end>-<status>`.
5. Log only the report ID. Never log the bearer token or the complete financial payload.
6. Return HTTP 204 when there is nothing to report yet, and HTTP 503 with `Retry-After` during
   brief unavailability. Mission Control owns retries.
7. Confirm the project's amber `AWAITING FIRST REPORT` row changes to green `CONNECTED`. The row
   separately shows whether the latest report is `ESTIMATED` or `FINAL`; missed cadence becomes a
   red `CONNECTION LOST` state and does not change project or system software health.
