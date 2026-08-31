# Money Machine

Money Machine is an auditable autonomous options agent for the Alpaca AI Trading Agents Hackathon, Options Alpha Agents track. It observes a paper account and liquid index options through Alpaca MCP Server V2, compiles only defined-risk structures, lets a structured model choose among candidate IDs or abstain, applies deterministic risk policy, reconciles broker state, and publishes a Decision Passport.

The application is built to fail closed. A missing quote, invalid model response, stale chain, account mismatch, live endpoint, unexplained position, kill switch, time cutoff, or risk-cap breach prevents a new entry. Development entry authority is not tied to the competition clock. Competition entries become eligible automatically at the official Monday scoring start after the exact paper account is verified on that live cycle; before the first managed competition order, the account must also still be flat, fill-free, and exactly $100,000.

> Educational paper-trading software only. Options involve substantial risk. Replay and counterfactual results are hypothetical and are never labeled as official competition P&L.

## What judges can see in 30 seconds

The dashboard leads with paper equity, execution state, current defined risk, the latest constrained model decision, the risk-budget auction, every hard policy check, and the resulting order or abstention. Opening the Decision Passport joins the Alpaca-sourced evidence, alternatives, selection, risk decision, execution, reconciliation state, and outcome under one audit hash.

Outside regular option hours, official Alpaca option P&L remains authoritative and unchanged. The
dashboard may additionally show a separately labeled provisional mark-to-market based on each held
option's closing delta and the underlying ETF's extended-hours move. The estimate never enters
official equity, risk, execution, or reporting calculations and explicitly excludes overnight IV,
theta, higher-order effects, and opening spreads.

## Architecture

```text
Alpaca MCP V2 ──> explicit adapter ──> deterministic candidate compiler
       │                                      │
       ├── account / clock / market data      ├── index iron condors
       ├── options chains / quotes            └── directional debit spreads
       └── orders / fills / positions / history         │
                                                        v
                                              risk-budget auction
                                                        │
                                              structured model choice
                                                        │
                                              deterministic risk policy
                                                        │
                                              idempotent limit execution
                                                        │
                             stale repricing + profit/loss/time/portfolio exits
                                                        │
                                              SQL audit ledger + Passport
```

The domain layer in `src/money_machine/domain` has no FastAPI, MCP, OpenAI, or SQLAlchemy dependency. Transport, model, clock, and persistence boundaries are explicit. Replay and live cycles call the same orchestration and risk code.

More detail: [Architecture](docs/ARCHITECTURE.md), [MCP decision](docs/adr/0001-alpaca-mcp-v2.md), and [execution authority](docs/adr/0002-execution-authority.md).

## Safe local setup

Requirements: Python 3.12. Docker is optional.

```bash
make install
make migrate
make verify
make replay
make serve
```

Open <http://127.0.0.1:8000>. Replay mode requires no API keys and seeds the canonical, explicitly non-official demonstration cycle.

Keep the two account configurations in separate ignored files. Use
`.env.development.local` with the development role and development credentials; reserve
`.env.competition.local` for the production role and the fresh competition credentials.
The immutable role-to-account mapping is documented in
[Competition accounts](docs/COMPETITION_ACCOUNTS.md).
Fill the three Alpaca credential/identity fields and restrict both files:

```bash
chmod 600 .env.development.local .env.competition.local
.venv/bin/money-machine --env-file .env.development.local mcp-read-check
```

Commands report credentials only as present or missing. They never print account IDs or secret values. `.env*.local` files are ignored by Git and Docker.

## Commands

```bash
# Apply Alembic migrations
.venv/bin/money-machine db upgrade

# Run the offline end-to-end cycle
.venv/bin/money-machine replay

# Start dashboard
.venv/bin/money-machine serve --host 127.0.0.1 --port 8000

# Safe Alpaca V2 read verification for the selected role
.venv/bin/money-machine --env-file .env.competition.local mcp-read-check

# Explicitly authorized development-only order acceptance: opens and closes one bounded spread
.venv/bin/money-machine --env-file .env.development.local development-round-trip --confirm-paper-order

# Read-only competition acceptance report (requires production/competition values)
.venv/bin/money-machine --env-file .env.competition.local acceptance

# Deterministic redacted scoring-window performance evidence
.venv/bin/money-machine --env-file .env.competition.local competition-performance-export

# Persistent entry kill switch; cancel/close authority is unaffected
.venv/bin/money-machine kill-switch on
.venv/bin/money-machine kill-switch status

# Single guarded scheduler cycle, useful for operations checks
.venv/bin/money-machine --env-file .env.competition.local scheduler --once

# Run the real local development instance (dashboard and scheduler use one live audit DB)
DATABASE_URL=sqlite:///./money_machine.development.db RUN_MODE=live \
  .venv/bin/money-machine --env-file .env.development.local serve --host 127.0.0.1 --port 8000
DATABASE_URL=sqlite:///./money_machine.development.db RUN_MODE=live \
  .venv/bin/money-machine --env-file .env.development.local scheduler
```

The replay endpoint and dashboard control are disabled whenever `RUN_MODE=live`.

There is intentionally no `EXECUTION_ENABLED` or separate go-live flag. New-entry authority is derived from `ACCOUNT_ROLE`, the fixed competition clock, exact paper-account verification, persistent kill switch, and clean reconciliation. Development is not bound to the competition clock, although options still require an open market to execute.

## Testing and quality

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
```

Development MCP integration tests skip automatically when `.env.development.local` is absent:

```bash
.venv/bin/pytest -m integration
```

The suite boundary-tests one second before, exactly at, and one second after all five competition deadlines. It also covers account isolation, invalid model fallback, option geometry, every portfolio stop, correlated exposure, duplicate suppression, kill-switch persistence, stale/incomplete data, bounded repricing, reconciliation failures, and replay-to-Passport generation.

Live health is evidence-based: database connectivity, the last successful Alpaca MCP cycle, scheduler heartbeat freshness, and reconciliation state are reported separately. A merely configured MCP command is not reported as connected.

## Docker and Render

Build locally:

```bash
docker build -t money-machine .
docker run --rm -p 8000:8000 -e RUN_MODE=replay money-machine
# Or start the replay dashboard plus PostgreSQL:
docker compose up --build
```

`render.yaml` defines one `money-machine` Render Project with a `production` Environment
containing a public dashboard, one scheduler worker, and PostgreSQL. Both services use the
same image; the scheduler also holds a database lease, so accidental duplicate workers cannot
create duplicate cycles or orders. Render prompts for Alpaca secrets through `sync: false`;
none are stored in the Blueprint. The initial deployment uses the deterministic live-data
selector; OpenAI can be enabled later by setting `MODEL_PROVIDER=openai` and adding
`OPENAI_API_KEY`. The production-capable Blueprint uses Starter
services and a Basic database, so review Render pricing before applying it.

## Safety posture

- Alpaca paper endpoint only; live URLs and lookalikes are rejected.
- Credentials select an account; `ALPACA_EXPECTED_ACCOUNT_ID` only verifies it.
- Production maps only to the competition role; development only to development.
- The model cannot set strikes, quantity, endpoint, account, order class, or price semantics.
- Structures use one underlying, one expiry, equal leg ratios, and bounded long wings.
- Multi-leg entries are day limit orders with deterministic client IDs.
- Stale entries are canceled and can be replaced only twice within a fixed concession budget.
- Open positions use executable-quote profit targets, stop losses, the model's maximum holding time, and portfolio loss/drawdown exits. New entries and all working opening orders stop at Thursday's 2:30 PM ET cutoff. Forced liquidation of every managed credit and debit structure starts by 3:15 PM, the internal flat target is 3:45 PM, and Alpaca's 4:00 PM EOD equity is the authoritative measurement. Close-only recovery persists until broker positions and relevant working orders confirm flat.
- The dashboard's primary countdown targets Thursday's authoritative 4:00 PM ET equity lock. Friday's 9:30 AM ET hackathon deadline is shown only as submission-window context, not additional trading time.
- Counterfactual and replay data are visually and structurally separate from official P&L.

See [SECURITY.md](SECURITY.md) for incident handling and disclosure.
