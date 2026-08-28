# Implementation checklist

## P0

- [x] Python 3.12/FastAPI/Pydantic/SQLAlchemy/Alembic scaffold
- [x] Safe environment/account-role guard and paper endpoint rejection
- [x] Explicit official Alpaca MCP Server V2 adapter
- [x] SPY/QQQ/IWM defined-risk iron-condor compiler
- [x] Strict model candidate/abstain contract and replay/OpenAI providers
- [x] Deterministic sizing and initial portfolio limits
- [x] Required audit entities and Decision Passport
- [x] Judge-first public dashboard
- [x] Canonical offline replay
- [ ] Development-account round trip (blocked: development env file absent)

## P1

- [x] Five-minute scheduler, database lease, and cycle idempotency
- [x] Broker-order/position reconciliation and orphan halt
- [x] Idempotent Alpaca fill-activity ingestion and order-state advancement
- [x] Persistent kill switch preserving close authority
- [x] Equity/peak/drawdown/daily/defined/correlated risk state
- [x] SPY/QQQ directional debit-spread compiler
- [x] Wired stale-order cancellation and bounded repricing lifecycle
- [x] Wired short-volatility/final-deadline close-only lifecycle
- [x] Evidence-based MCP/DB/reconciliation health, heartbeat state, structured logging, correlation IDs
- [x] Read-only production acceptance command
- [x] Docker and Render Blueprint
- [ ] Live development MCP identity/read verification (blocked: development env file absent)
- [x] Selected paper-account MCP V2 identity/read smoke check (zero orders submitted)
- [ ] Production acceptance (blocked: supplied competition file is not mapped to the competition role; development round trip and go-live authorization also absent)
- [ ] Competition execution (intentionally blocked pending explicit go-live)

## P2 (not allowed to delay safety)

- [x] Visually isolated counterfactual alternatives
- [x] Risk-budget auction visualization and replay page
- [ ] Earnings and hedge playbooks
- [ ] Submission screenshots, video, and deck
