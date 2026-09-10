# Mission Control business reporting

Mission Control pulls one consolidated Business Performance Protocol v1 report for
`alpaca-competition` from `GET /internal/mission_control/report`. Reports are built entirely from
completed rows in the local `equity_snapshots` audit table for the verified production competition
account. Reporting never calls Alpaca and never exposes Alpaca or database credentials to Mission
Control. There is no product-side scheduler push.

Before scoring begins, the endpoint returns an `estimated` pre-competition telemetry report so
Mission Control can verify the reporting uplink and display current paper equity/P&L. Its metadata
includes `official_scoring_window: false` and `scoring_window_state: pre_scoring`; the P&L label is
`Pre-competition paper P&L`. This reporting does not grant trading authority and does not classify
weekend observations as official competition performance.

HTTP 204 means no completed official equity period is available yet.

## Paper P&L exception

This week-long competition has one deliberate accounting exception: Alpaca paper-account P&L is
reported as real competition P&L so gains and current equity remain visible on the Mission Control
dashboard. The exception is also included in every report's metadata as
`paper_pnl_reported_as_real: true`. It applies only to this competition integration; it does not
change the application's paper-only safety posture or make the values suitable for a general
ledger, tax, or cash accounting purpose.

## Metric mapping

| Metric | Source / query | Semantics | Period and unit | Accounting assumption |
|---|---|---|---|---|
| `net_profit` | Latest completed official `equity_snapshots.equity` at or before the report boundary, minus `$100,000.00` | Flow | Competition-to-date; USD | Paper P&L is treated as real competition P&L under the exception above. Equity captures realized and unrealized P&L. |
| `portfolio_value` | `equity_snapshots.portfolio_value` from the same row | Balance | Point-in-time at the report boundary; USD | Alpaca's persisted paper portfolio value is authoritative. |
| `cash_balance` | `equity_snapshots.cash` from the same row | Balance | Point-in-time at the report boundary; USD | Alpaca's persisted paper cash balance is authoritative. |
| `return_percent` | `(equity - 100000) / 100000 * 100` | Gauge | Competition-to-date; percent | The fixed verified competition baseline is `$100,000.00`; output is rounded to four decimal places. |

## Operating-cost readiness

The producer does not emit `x_operating_cost_total`, `costs_contract_version`, or `costs_v1` while
any active material vendor amount is unknown. Mission Control currently treats a numeric producer
total as complete and does not interpret coverage metadata, so reporting the known `$0.00` Alpaca
paper-trading price would incorrectly turn unresolved OpenAI usage and market-data costs into zero.

For auditability, `metadata.operating_cost_observability` records `status=incomplete`,
`contract_emitted=false`, the Render exclusion, known facts, and structured gaps under a
non-contract key that Mission Control ignores for cost calculations. The known fact is Alpaca's
officially documented `$0.00` paper-trading access; it does not establish the separate market-data
subscription. Unknown entries have no amount field.

The current audited gaps are:

- `openai_api_usage`: the application does not persist billable usage, and its project key lacks
  the access required by the OpenAI organization Costs API. An invoice, an authorized Costs API
  feed, or locally persisted billed cost is required before the cost contract can be enabled.
- `alpaca_market_data_subscription`: the paper API host and credentials do not establish whether
  the account uses the free Basic data plan or the paid Algo Trader Plus plan. Account-plan or
  invoice evidence is required.

Render compute, worker, and Postgres costs are intentionally absent because Mission Control
attributes the project's Render infrastructure directly. Repository hosting, DNS, and centralized
observability have no authoritative project allocation in this repository and are not included.
When every active material vendor amount has an authoritative local source, replace the readiness
metadata with the versioned `costs_v1` overlay and a complete `x_operating_cost_total` in one
reviewed change.

Pre-scoring reports start after the first completed hourly boundary following the hackathon start
and use only persisted, completed live observations for the verified competition account. Official
competition reports begin after the first completed scoring interval following
`2026-08-31T13:30:00Z`. Every report is `estimated` and has a stable ID tied to its hourly boundary.
After Thursday's EOD measurement, a `final` report is produced only from the exact authoritative
Thursday EOD equity checkpoint at `2026-09-03T20:00:00Z`; if that snapshot does not exist, final is
deliberately omitted. Production paper observations before Monday remain excluded from official
competition reports even though they are visible as explicitly non-official telemetry.

The adapter omits broker-reported `realized_pl` and `unrealized_pl` as separate metrics because
those fields are not guaranteed to be populated by the Alpaca account response. It does not send a
zero or guess in their place.

## Configuration and rollout

The web service reads:

- `MISSION_CONTROL_PROJECT=alpaca-competition`
- `MISSION_CONTROL_TOKEN` (the distinct token for this project only)
- `MISSION_CONTROL_REPORTING_INTERVAL_MINUTES=60`
- `MISSION_CONTROL_ENVIRONMENT=production`

The product does not need `MISSION_CONTROL_URL` for pull. Keep it only if you still use
`money-machine mission-control-report` as a compatibility submitter. There is intentionally no
reporting enable flag. A configured project slug other than `alpaca-competition` is rejected
before a payload is returned. Logs include only the report ID, duplicate flag, error class, and
HTTP status.

Before Mission Control starts polling, reconcile the exact persisted payload without sending:

```bash
money-machine mission-control-report-dry-run
```

Confirm Mission Control shows the project as `CURRENT`, with the expected period, status, metrics,
and equity-derived P&L.

The wire contract is copied at [`business-performance-protocol.md`](./business-performance-protocol.md).
