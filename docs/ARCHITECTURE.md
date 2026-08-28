# Architecture

## Core flow

`AgentService` owns one restart-safe five-minute cycle. It creates the unique observation bucket before making any order decision. A repeated bucket returns the existing Passport and cannot submit again.

1. Read account, market clock, broker orders, fill activities, and positions.
2. Verify account identity for live mode, ingest fills idempotently, and reconcile broker exposure to local attribution.
3. Apply stale-order cancellation/replacement and any mandatory close-only deadline action.
4. Persist equity, positions, and SPY/QQQ/IWM market evidence.
5. Compile liquid, defined-risk candidates; an empty set is normal.
6. Rank candidates in the risk-budget auction.
7. Ask one provider for the strict `ModelDecision` schema. Invalid output becomes abstention.
8. Resolve only a supplied candidate ID and run every deterministic risk check.
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
- `scheduler.py`: five-minute loop with database lease and graceful shutdown.

## Data and failure policy

Alpaca is authoritative for account, order, fill, position, and portfolio state. PostgreSQL is authoritative for decision attribution and the audit trail. Broker state without local attribution sets reconciliation unclean and blocks entries. Close requests invert only locally attributed structure legs, use current per-leg quotes, and cannot carry opening position intents. Exceptions are reduced to safe type-only incident codes; raw credential-bearing exceptions never enter Passports or logs.

The minimum entities from `SPEC.md` are represented explicitly: AgentRun, MarketSnapshot, Candidate, Auction, ModelDecision, RiskDecision, OptionStructure, BrokerOrder, Fill, EquitySnapshot, PositionSnapshot, and SystemState. Corrections are represented by new observations/system states rather than silent historical rewrites.

## Strategy defaults

Defaults are conservative and transparent, not statistically optimized: 90-second quotes, minimum 25 contracts of observed volume, minimum 200 open interest, 1.20× implied/realized move richness, a 45% structure-spread-to-credit ceiling, and $5 index wings ($3 for IWM). The compiler can emit zero candidates.

Initial risk is exactly the specification: 0.50% per index/directional structure, 0.35% earnings (reserved), 1.00% SPY/QQQ/IWM cluster, 2.00% total defined loss, 1.00% daily stop, 2.00% peak drawdown, three alpha structures, and one pending entry per underlying.
