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
1.50% of equity; a liquid index candidate can receive at most 4.00% only when validated
model confidence is at least 0.80 and every hard gate passes. Condors use richness of at
least 1.50 and reward/risk of at least 0.25 as their strategy-specific confirmation.
Directional debit spreads on SPY, QQQ, and IWM do not use richness for upsizing; they
require at least 0.50% deterministic trend strength, at least 2.00 reward/risk, and debit
no greater than one-third of spread width so expensive premium alone cannot trigger the
larger tier. Earnings stays
at 0.35%. The shared SPY/QQQ/IWM cap is 8.00%, total concurrent defined loss is capped at
10.00%, daily loss at 4.00%, and peak drawdown at 8.00%. A candidate is excluded when its
underlying already has a managed structure or pending entry; raw parent count is not a second
portfolio veto. Quantity is always floored from the smallest remaining applicable budget, and
the effective tier, per-underlying evidence, correlated headroom, and total headroom are recorded
in the Decision Passport.

Index condors must clear 0.20 reward/risk using adverse executable bid/ask economics before the
model auction; the 0.25 high-conviction threshold remains stricter and unchanged. Rejected
structures remain in the complete CandidateBuildReport for audit and counterfactual analysis.
Entries also require at least 30 minutes before the earliest of the model hold, the 3:50 PM ET
daily hard exit, and Thursday forced flatten. Directional spreads cap the model hold at 60 minutes
and persist a deadline no later than 15 minutes before the next macro event; configured event
cooldowns and explicit upstream event vetoes remain fail closed. Soft maximum-hold/profit exits use quote-aware
backoff, while urgent safety exits retain aggressive bounded concessions.

During the final September 2–3 competition recovery window, the production selector is
directional-only for new entries. Condors remain in the complete candidate report for audit and
counterfactual analysis but are deterministically excluded before model selection. Directional
spreads require two consecutive completed five-minute observations with the same direction and at
least 0.40% absolute return from previous close; missing, reversed, weak, or stale history abstains.
Production entry authority is additionally restricted to 09:45–15:20 ET, while Thursday retains
the immutable 14:30 ET competition cutoff. These selector controls do
not weaken reconciliation or lifecycle ownership of previously established structures.

The daily-loss control is persistent and session-scoped. A raw 4.00% breach immediately
freezes entries, but a clean defined-risk book is force-closed only after a second account
observation and complete fresh executable leg quotes validate a plausible loss. A loss beyond
the persisted defined-loss envelope (plus a documented tolerance) remains quarantined for
mark-quality review. A credible breach latches through the session; reconciliation and
structural safety incidents keep immediate close authority.
