# Architecture

## Core flow

`AgentService` owns one restart-safe five-minute cycle. It creates the unique observation bucket before making any order decision. A repeated bucket returns the existing Passport and cannot submit again.

1. Read account, market clock, broker orders, fill activities, and positions.
2. Verify account identity for live mode, ingest fills idempotently, and reconcile broker exposure to local attribution.
3. Apply stale-order cancellation/replacement and any mandatory close-only deadline action.
4. Persist equity, positions, and SPY/QQQ/IWM market evidence.
5. Compile and persist the full liquid, defined-risk candidate report; an empty set is normal.
6. Exclude pending/already-managed underlyings from the model-facing auction while retaining
   them as audited counterfactuals, then rank the remaining candidates.
7. Ask one provider for the strict `ModelDecision` schema. Invalid output becomes abstention.
8. Resolve only an exact allowed candidate ID and run every deterministic risk check. One
   explicit exact-membership retry is allowed; a second mismatch abstains without fuzzy matching.
9. Calculate quantity from maximum loss and round down.
10. Submit one idempotent multi-leg day limit entry only when authorized.
11. Persist the model, risk, auction, order/fill lifecycle, operational state, and Decision Passport.

## Dependency direction

- `domain/`: immutable competition policy, schemas, option geometry, candidate compiler, risk checks.
- `ports.py`: market, brokerage, model, and clock interfaces.
- `adapters/`: official Alpaca MCP V2 and deterministic replay implementations.
- `persistence/`: append-oriented SQLAlchemy audit ledger.
- `service.py`: application orchestration; depends on ports and repository, not FastAPI.
- `web.py`: public read-only presentation plus canonical replay endpoint.
- `scheduler.py`: five-minute loop with database lease and graceful shutdown; one-minute recovery
  cadence from forced-liquidation start until broker-confirmed flat.

## Data and failure policy

Alpaca is authoritative for account, order, fill, position, and portfolio state. PostgreSQL is authoritative for decision attribution and the audit trail. Broker state without local attribution sets reconciliation unclean and blocks entries. Account equity is checkpointed immediately after identity and brokerage-state verification, before market-data collection, so a market-data failure blocks trading without losing performance evidence. Close requests invert only locally attributed structure legs, use current per-leg quotes, and cannot carry opening position intents. Exceptions are reduced to safe type-only incident codes; raw credential-bearing exceptions never enter Passports or logs.

The minimum entities from `SPEC.md` are represented explicitly: AgentRun, MarketSnapshot, Candidate, Auction, ModelDecision, RiskDecision, OptionStructure, BrokerOrder, Fill, EquitySnapshot, PositionSnapshot, and SystemState. Corrections are represented by new observations/system states rather than silent historical rewrites.

## Strategy defaults

Defaults are conservative and transparent, not statistically optimized: 90-second quotes, minimum 25 contracts of observed volume, minimum 200 open interest, 1.20× implied/realized move richness, a 45% structure-spread-to-credit ceiling, and $5 index wings ($3 for IWM). The compiler can emit zero candidates.

Risk is deterministic and based on current equity and defined maximum loss, never the
broker's buying-power multiple. Index/directional structures normally receive at most
1.00% of equity; a liquid index candidate can receive at most 3.00% only when validated
model confidence is at least 0.80 and every hard gate passes. Condors use richness of at
least 1.50 and reward/risk of at least 0.25 as their strategy-specific confirmation.
Directional debit spreads on SPY, QQQ, and IWM do not use richness for upsizing; they
require at least 0.50% deterministic trend strength, at least 2.00 reward/risk, and debit
no greater than one-third of spread width so expensive premium alone cannot trigger the
larger tier. Earnings stays
at 0.35%. The shared SPY/QQQ/IWM cap is 6.00%, total concurrent defined loss is capped at
8.00%, daily loss at 3.00%, and peak drawdown at 6.00%. At most three
alpha structures may normally be open, with no pending duplicate or addition to an existing
managed underlying. A narrow legacy-stack accommodation permits one ordinary-tier SPY or
IWM index structure only while four or five reconciled open structures are all QQQ and no
entry is pending. It cannot add QQQ exposure, cannot use the high-conviction tier, and
disables itself after the first distinct-underlying order or when the QQQ legacy count is
three or fewer. Quantity is always floored from the smallest remaining applicable budget,
and the effective tier, legacy-exception evidence, and budget are recorded in the Decision
Passport.
