# Money Machine Competition Specification

Status: Draft for kickoff implementation
Competition: Alpaca AI Trading Agents Hackathon
Track: Options Alpha Agents
Hackathon window: August 28, 2026 at 9:30 AM ET through September 4, 2026 at 9:30 AM ET
Official trading starts: August 31, 2026 at 9:30 AM ET
Authoritative performance measurement: September 3, 2026 at 4:00 PM ET

## 1. Product thesis

Money Machine is an autonomous options trading agent that decides when to sell volatility, express a directional view with defined risk, hedge, or abstain. It uses Alpaca's MCP server for account state, market data, option-chain analysis, and paper-order execution. Every model decision passes through deterministic liquidity, exposure, loss, timing, and account-safety controls before an order can be submitted.

The winning story is not "an LLM that trades." It is an auditable volatility governor:

1. Observe the market and the paper account through Alpaca.
2. Classify the current volatility and directional regime.
3. Select one approved options playbook or no trade.
4. Compile the intent into a defined-risk structure.
5. Enforce deterministic portfolio and execution limits.
6. Submit and reconcile a multi-leg paper order.
7. Explain the complete decision, risk checks, fill quality, and P&L in a public dashboard.

## 2. Competition objective

Primary objective: finish the competition with positive, verifiable P&L while demonstrating a technically credible autonomous agent and a polished, understandable product.

Target outcomes:

- Total return target: +0.5% to +1.5% on the $100,000 competition account.
- Maximum peak-to-trough drawdown stop: 8.0% during the final competition sprint.
- No unauthorized, live-money, naked-option, or wrong-account orders.
- Every order traceable to one agent decision and one risk decision.
- A judge can understand the agent's last decision within 30 seconds.
- The deployed application, public repository, video, deck, and written submission tell the same story.

The return target is an operating objective, not a promise. Preserving eligibility and avoiding a catastrophic loss take precedence over forcing trades.

### 2.1 Winning rubric strategy

Money Machine is designed against Lablab's four judging dimensions, not only the paper-account result:

- **Presentation:** the opening dashboard communicates account result, current risk, latest action, and latest abstention within 30 seconds. The video leads with a working decision-to-fill demo rather than architecture slides.
- **Business value:** the initial user is an active options trader or small trading team that wants automation but cannot accept an opaque agent. The commercial extension is a subscription decision-audit workspace plus a broker/platform risk-control API. Any market-size figure used in the pitch must have a cited source; none will be invented for the prototype.
- **Application of technology:** the model performs meaningful regime and candidate comparison, Alpaca MCP supplies real account and options context, and the deployed system completes the entire loop. The AI is neither a chat wrapper nor allowed to manufacture order parameters.
- **Originality:** the product makes restraint, alternatives, and counterfactuals visible. It demonstrates how an AI agent competes for a finite risk budget and proves why a trade or abstention deserved that budget.

The first end-to-end working loop is the highest-priority artifact. Settings, authentication, broad strategy coverage, and non-demo administration screens cannot delay it.

### 2.2 Distinctive mechanism: risk-budget auction and Decision Passport

Each eligible playbook submits zero or more precompiled, defined-risk candidates to a risk-budget auction. The model compares candidates across regime fit, event context, diversification, and thesis quality. Deterministic policy then awards or refuses portfolio risk. A higher model confidence never overrides a hard gate or increases a position beyond its calculated cap.

Every cycle produces a **Decision Passport** containing:

- The Alpaca-sourced market and account evidence available at decision time.
- All candidates that reached the auction, including maximum loss and liquidity facts.
- The selected candidate or abstention, with structured model reasoning.
- Deterministic risk checks, awarded risk budget, and rejection reason codes.
- The order, fill, position lifecycle, and realized outcome when a trade occurs.
- Counterfactual tracking of rejected candidates using their decision-time marks, clearly labeled as hypothetical and never mixed with official P&L.

This is the primary product and demo differentiator. The dashboard should make a profitable trade interesting, but it should make a well-justified no-trade decision equally legible.

## 3. Competition requirements

Money Machine must:

- Operate only in Alpaca paper trading.
- Use a brand-new competition account starting with exactly $100,000.
- Use Alpaca's Trading API and Alpaca MCP or CLI.
- Be autonomous rather than a prompt-driven trade ticket.
- Incorporate options in every trading playbook.
- Use a clear, testable strategy.
- Show how opportunities are detected, decisions are made, positions are managed, and performance is measured.
- Include the competition Alpaca account ID in the final submission.
- Include a one-page explanation of AI logic, risk gates, and Alpaca infrastructure.
- Provide a public GitHub repository, hosted application, demo video, and pitch deck.
- Optionally provide up to five qualifying build-in-public social links.

Secrets and Alpaca API credentials must never be committed or displayed in the public application.

## 4. Scope

### 4.1 In scope

- SPY, QQQ, and IWM as the primary liquid underlying universe.
- A small, gated universe of liquid single-name earnings candidates.
- Defined-risk multi-leg option structures only.
- Autonomous observation, decision, order, monitoring, exit, and reconciliation loops.
- Paper-account equity and P&L tracking directly from Alpaca.
- Replay mode using captured or historical snapshots for the demo.
- Public read-only dashboard with redacted identifiers and no secrets.

### 4.2 Out of scope

- Live-money trading.
- Naked calls or puts.
- Equity-only, crypto-only, or ETF-share fallback strategies.
- High-frequency or latency-sensitive trading.
- Martingale sizing, doubling down, or recovery trades.
- Unbounded LLM-generated strikes, quantities, or order parameters.
- A general-purpose retail trading platform.
- Training a new foundation model during the competition.

### 4.3 Delivery priority

The scope is intentionally implemented in layers:

- **P0 — working within 24 hours:** account guard, Alpaca read path, one liquid-index candidate factory, structured model choice including abstention, deterministic position sizing, one development-account multi-leg round trip, persisted Decision Passport, and a deployed read-only result page.
- **P1 — competition-safe:** restart-safe scheduler, continuous reconciliation, equity tracking, close logic, operational kill switch, directional debit-spread playbook, alerts, and production acceptance gate.
- **P2 — judging leverage:** risk-budget auction UI, counterfactual tracking, selective earnings playbook, hedge playbook, replay mode, and presentation polish.

P2 work cannot delay P0 or P1. If time compresses, Money Machine ships with one deeply verified alpha playbook and excellent evidence rather than several shallow ones.

## 5. Environments and account isolation

Money Machine has one configuration per runtime environment and never switches Alpaca accounts at runtime.

### 5.1 Development

- `APP_ENV=development`
- `ACCOUNT_ROLE=development`
- Uses only the development paper-account credential pair.
- Paper execution is allowed at any time.
- The startup account check must match `ALPACA_EXPECTED_ACCOUNT_ID`.

### 5.2 Production

- `APP_ENV=production`
- `ACCOUNT_ROLE=competition`
- Uses only the fresh competition paper-account credential pair.
- The startup account check must match `ALPACA_EXPECTED_ACCOUNT_ID`.
- The returned account must be a paper account.
- Entry authority is derived from the competition clock, not from an environment toggle.

API credentials select the Alpaca account. `ALPACA_EXPECTED_ACCOUNT_ID` is a fail-closed assertion and never selects or changes an account.

### 5.3 Startup invariants

The application must refuse to start its trading loop when any invariant fails:

- Alpaca authentication succeeds.
- Returned account ID equals the expected ID.
- The endpoint and credentials are for paper trading.
- `production` maps only to `competition`.
- `development` maps only to `development`.
- Production baseline was recorded from a new $100,000 account.
- Database schema is current.
- No unreconciled broker positions exist without local records.

## 6. Competition clock and execution state

Competition timing is immutable product policy and belongs in version-controlled constants, not environment files.

Internal timestamps must use UTC:

```text
HACKATHON_STARTS_AT      2026-08-28 13:30:00Z  # Friday 9:30 AM EDT (build window)
SCORING_STARTS_AT        2026-08-31 13:30:00Z  # Monday 9:30 AM EDT (trading window)
NEW_ENTRY_CUTOFF         2026-09-03 18:30:00Z  # Thursday 2:30 PM EDT
FORCED_FLATTEN_STARTS_AT 2026-09-03 19:15:00Z  # Thursday 3:15 PM EDT
FLAT_TARGET_AT           2026-09-03 19:45:00Z  # Thursday 3:45 PM EDT
EOD_EQUITY_SNAPSHOT_AT   2026-09-03 20:00:00Z  # Thursday 4:00 PM EDT
ENDS_AT                  2026-09-04 13:30:00Z  # Friday 9:30 AM EDT
BASELINE_EQUITY          100000.00
```

The production execution state is derived as follows:

```text
Before SCORING_STARTS_AT         OBSERVE_ONLY
SCORING_STARTS_AT to cutoff      FULL_EXECUTION
NEW_ENTRY_CUTOFF to ENDS_AT      CLOSE_ONLY
After ENDS_AT with positions     CLOSE_ONLY_UNTIL_FLAT
After ENDS_AT and flat           DISABLED
```

At `NEW_ENTRY_CUTOFF`, every pending opening order is canceled immediately regardless of age and
can never be replaced. `FORCED_FLATTEN_STARTS_AT` is the trigger for repeated close attempts across
both credit and debit structures; it is not a promise that the account is already flat.
`FLAT_TARGET_AT` is the internal target at which residual positions or working orders become a
prominent incident. `EOD_EQUITY_SNAPSHOT_AT` is Alpaca's authoritative Thursday measurement,
including Thursday exercise and assignment effects. Risk-reducing cancel and close operations
remain available whenever authoritative broker positions or relevant working orders show exposure,
including after the formal Friday end.

Official P&L is measured from Monday's open using the fresh competition account and final account equity is taken after Thursday's close. Every position is therefore scheduled flat before Thursday's closing bell; the Friday hackathon deadline is not treated as extra trading time. September 4 also contains the 8:30 AM EDT Employment Situation release, so no exposure is carried into it.

For this one-off event, the reviewed BLS, BEA, and Federal Reserve release times are also
version-controlled. Condors retain the conservative six-hour overlap veto. Directional debit
spreads instead have a hard 45-minute maximum hold and a 15-minute pre-release buffer: entry is
allowed only when at least 30 minutes remain and the persisted lifecycle deadline does not cross
that buffer. Every strategy observes the configured post-release cooldown, and an explicit
upstream event-risk flag always vetoes entry.

Boundary tests are required for one second before, exactly at, and one second after every transition.

## 7. Autonomous agent loop

The regular market-hours loop runs every five minutes and increases to approximately once per minute
from forced-liquidation start until Alpaca positions and relevant working orders confirm flat.

For the September 2 recovery window, new production entries are considered only from 09:45 ET
inclusive to 15:20 ET exclusive. The 2026-09-03 final-hour mandate supersedes Thursday's earlier
14:30 cutoff and admits the exact 15:45 scheduler boundary before close-only begins.
New-entry auction input is limited to SPY/QQQ/IWM call and put
debit spreads. Index condors continue to be compiled and persisted for Decision Passport and
counterfactual evidence, but carry the explicit `competition_directional_only_policy` exclusion and
cannot reach the model auction. This temporary policy does not alter lifecycle authority for an
already-managed structure.

A directional candidate must retain the same sign and at least 0.40% absolute return from previous
close across the current and immediately preceding completed five-minute observation. The cycle
buckets must be 5–10 minutes apart. Missing, weak, reversed, malformed, or stale prior evidence
fails closed, and both observation timestamps and trend values are sealed in the Passport. The
existing high-conviction requirements remain unchanged; final-sprint sizing is 1.5% standard and
4% qualifying high conviction.

Each loop:

1. Verifies account identity and competition state.
2. Reconciles Alpaca orders, fills, and positions with the local ledger.
3. Records an account-equity and open-risk snapshot.
4. Retrieves market clock, prices, option chains, quotes, Greeks, and relevant news through Alpaca MCP.
5. Builds deterministic features and eligible candidate structures.
6. Requests one structured regime decision from the model.
7. Compiles the decision into a permitted structure or abstention.
8. Runs portfolio and execution risk gates.
9. Submits an idempotent multi-leg limit order when approved.
10. Monitors stale orders and open positions.
11. Publishes the decision and state change to the dashboard.

The loop must be restart-safe. Re-running the same observation window must not create a duplicate order.

## 8. AI decision contract

The model is a constrained decision component, not the risk manager or broker.

It receives:

- Market regime features.
- Underlying returns and trend features.
- Realized and implied movement estimates.
- Option-chain liquidity and spread summaries.
- Scheduled macro-event context.
- Relevant news summaries.
- Current portfolio exposure and recent decisions.
- Only candidate structures that already pass basic data-quality checks.

It returns a schema-validated object:

```json
{
  "regime": "calm|directional_up|directional_down|event_risk|dislocated",
  "action": "index_condor|call_debit_spread|put_debit_spread|earnings_condor|hedge|abstain",
  "candidate_id": "string|null",
  "confidence": 0.0,
  "thesis": "short explanation",
  "evidence": ["fact 1", "fact 2"],
  "invalidation": ["condition 1"],
  "maximum_holding_minutes": 0
}
```

Rules:

- Invalid or incomplete model output becomes `abstain`.
- Candidates on pending or already-managed underlyings remain in the persisted report and
  counterfactual evidence but are excluded from the model-facing auction. Their IDs and
  deterministic exclusion reasons are recorded in the Decision Passport. If the model returns
  an ID outside the allowed set, it receives one exact-membership retry listing the allowed IDs;
  a second mismatch fails closed without fuzzy matching.
- Unsupported actions become `abstain`.
- The model cannot set quantity, final strikes, account, broker endpoint, or order type.
- The model cannot override time, drawdown, loss, liquidity, correlation, or event gates.
- One model call per normal agent cycle is the target. Avoid multi-agent prompt chains in the critical path.

## 9. Trading playbooks

All structures use a single underlying, one expiration, defined maximum loss, and multi-leg limit execution.

### 9.1 Liquid index volatility condor

Universe: SPY, QQQ, IWM.

Intent: harvest the index/ETF variance risk premium when implied movement is rich relative to a deterministic realized-movement forecast and the regime is calm.

Required gates:

- Sufficient option-chain depth and freshness.
- Every leg has a fresh bid/ask, available quote depth when supplied, and trustworthy daily
  volume. Open interest is used when the source supplies it but is not fabricated or required
  from Alpaca's option snapshot schema.
- Net structure spread is below the configured fraction of expected credit.
- Executable adverse-side credit implies reward/risk of at least 0.20 before the candidate can
  enter the auction. Rejected structures remain in the full CandidateBuildReport and
  counterfactual evidence.
- Implied move is inside a sane range.
- Richness ratio exceeds the minimum threshold.
- No disallowed macro event within the intended holding period.
- Aggregate SPY/QQQ/IWM cluster risk remains within its shared cap.

### 9.2 Directional debit spread

Universe: SPY, QQQ, and IWM.

Intent: express a high-confidence directional regime without selling neutral volatility into a trend.

Structure: call debit spread for bullish regimes, put debit spread for bearish regimes. The
compiler requires an exact $5 protective short-wing distance for every directional underlying,
so strike geometry and deterministic maximum-loss accounting cannot diverge.

Required gates:

- Model confidence exceeds the directional threshold.
- Deterministic trend and momentum features agree with direction.
- Debit, maximum loss, break-even, and reward-to-risk are acceptable.
- No contradictory event-risk veto.
- Exit deadline fits inside the competition window.

### 9.3 Selective earnings volatility condor

Intent: sell an overpriced earnings move only when a liquid single-name chain survives strict screening.

The runtime implied move comes from the Alpaca ATM straddle. External data may enrich or validate the estimate but cannot be a hard runtime dependency.

Required gates:

- Confirmed earnings time and trading session.
- Mid- or large-cap underlying with listed options.
- Expected-move richness exceeds the threshold.
- All four legs pass quote, spread, volume, and open-interest checks.
- Position can be entered and exited within the competition window.
- No pre-window position seeding.

This is a satellite playbook, not the sole engine. An empty earnings opportunity set is normal and must not force a lower-quality trade.

### 9.4 Hedge or abstain

The agent may purchase a small put debit spread when portfolio exposure breaches the hedge trigger but remains below the hard stop. Otherwise it abstains.

`abstain` is a first-class successful decision and must include the failed gates or risk rationale.

## 10. Deterministic risk policy

Competition limits:

- Maximum loss per index or directional structure: 3.00% of current equity.
- High-conviction liquid index tier: up to 6.00% of current equity only when validated
  model confidence is at least 0.80 and every existing data, liquidity, reconciliation,
  event, directional, and portfolio gate passes. Premium-selling index condors additionally
  require richness of at least 1.50 and reward/risk of at least 0.25. Directional debit
  spreads instead require deterministic absolute trend strength of at least 0.50%,
  reward/risk of at least 2.00, and debit no greater than one-third of spread width;
  richness never qualifies a debit spread for larger sizing because it can reflect expensive
  option premium. Earnings candidates are never eligible for this tier.
- Maximum loss per earnings structure: 0.35% of current equity.
- Maximum combined SPY/QQQ/IWM cluster loss: 12.00% of current equity normally; 24.00% for the
  explicitly authorized 2026-09-03 final-day session.
- Maximum total concurrent defined loss: 15.00% of current equity normally; 24.00% for the
  explicitly authorized 2026-09-03 final-day session.
- Daily realized plus unrealized loss stop: 6.00% of start-of-day equity, with an explicitly
  bounded Thursday-only final-day boundary of 11.00%.
- A raw daily-loss breach freezes new entries immediately. Liquidation of a clean, fully
  reconciled defined-risk book requires a second broker equity observation plus complete,
  fresh, internally consistent executable leg quotes. A loss materially beyond the persisted
  defined-loss envelope is quarantined as a mark-quality incident rather than treated as a
  credible liquidation signal. A validated breach latches the entry halt through the UTC
  session; reconciliation and other structural safety incidents retain immediate close authority.
- Competition peak-to-trough drawdown stop: 12.00%.
- Maximum one managed or pending alpha structure per underlying normally. On 2026-09-03 only, at
  most one additional independently validated index structure may be added per already-used
  underlying. Raw parent-order count is not an independent veto; effective correlated-cluster and
  total defined-loss caps remain final portfolio limits.
- Maximum one pending entry per underlying.
- No resizing an existing managed structure. Pyramiding remains forbidden except for the explicit
  2026-09-03 one-additional-index-structure rule above.
- No naked legs.
- No market orders for multi-leg entries or exits under normal operation.
- No quantity increase after an adverse move.
- On the Thursday final day, every index directional debit spread takes profit when fresh
  executable closing quotes reach 2.10 times opening debit, including positions opened before
  this rule was deployed. Other competition directional debit spreads retain the 1.35 multiple,
  other debit strategies retain 1.50, and the directional 0.65 premium-value stop is unchanged.
- The final-day directional playbook rotates deterministically after 45 minutes. Decision
  Passports persist the flat/no-entry clock and each open structure's elapsed time and best
  executable progress. A setup that remains flat and unproductive for the full interval is
  excluded on the next auction in favor of another underlying or independently confirmed
  direction. After a debit stop, the identical underlying/direction remains excluded until a
  persisted neutral or opposite signal is followed by the normal two-cycle reconfirmation.
- Thursday's daily-loss baseline is the first clean regular-session account mark and never resets
  after a trade, exit, or return to flat. Each Passport reports the running loss even below the
  latch. At the $99,243.24 Thursday start, the authorized 11% boundary is $10,916.76 and is
  reported at about $88,326.48 after confirmation without overriding the final-day policy below.
- On 2026-09-03 only, the daily-loss and competition-drawdown checks remain audited but neither
  veto a new entry nor force portfolio liquidation merely because those loss thresholds remain
  breached. Reconciliation-safety exits, per-structure 0.65 premium-value stops, 2.10 targets,
  45-minute maximum holds, and the forced-flatten schedule remain active. This override expires
  at the official equity lock and does not alter other sessions.
- The final-day profit lock is $104,000 of verified official-account equity. The first clean mark at
  or above the target latches through the 16:00 ET equity lock using the persisted clean official
  peak: no new entry is permitted, pending entries cancel without replacement through normal
  lifecycle, and established structures close atomically through the normal exit path. The trigger,
  current equity, latched peak, source, and expiry are recorded in every Decision Passport. It is
  date-bounded to 2026-09-03 and cannot activate from an unreconciled mark.
- From 15:00 through the exact 15:45 ET boundary on 2026-09-03 only, independently validated
  high-conviction SPY/QQQ/IWM directional debit spread may use the remaining headroom under both
  24% defined-loss caps. Existing defined loss consumes that headroom. Quantity is floored by
  per-contract maximum loss and capped by displayed executable depth on every opening leg. The
  tier requires the normal exact-ID, two-cycle direction, confidence, reward/risk, data, liquidity,
  event, reconciliation, pending-order, account, and atomic-MLEG gates. There is no fixed final-
  window entry count; each ordinary cycle may submit at most one eligible atomic entry, sized only
  from live remaining headroom. Each hold is clamped to 15:50 ET, with a minimum five-minute
  tradable window at the exact 15:45 boundary. Forced flatten begins at 15:50 and the flat target
  is 15:57.
- On Thursday only, one fresh high-conviction SPY/QQQ/IWM directional debit spread may use up to
  12% of current equity, never exceeding the existing 12% index-cluster cap and only while the
  cluster is flat. It requires every normal hard gate plus at least 0.10 percentage point of
  five-minute absolute-trend acceleration, or a persisted reset followed by a reconfirmed
  reversal. The submitted order persists a 2.10-times debit target, the unchanged 0.65 stop and
  45-minute maximum hold. Any prior submission that day disables this one-shot sizing tier; the
  2.10-times final-day high-conviction exit target remains applicable independently of sizing.
- The final-day target scenario assumes two independently qualifying high-conviction debit
  spreads near $5,954.59 each. Two 35% exits are about +$4,168 gross before slippage; one winner
  and one symmetric 35% stopped loser are approximately flat. Two stops are about -$4,168 before
  slippage. If both positions lose their entire debit before exits fill, the 12% index-cluster
  ceiling is about $11,909 at the $99,243.24 starting equity; the absolute 15% portfolio ceiling
  is about $14,886. The Thursday-only 11% daily stop blocks subsequent entries and invokes risk
  handling, but cannot guarantee an 11% terminal loss after 12% of defined risk is already open. Wednesday's
  observed premium gains of about 10.5% and 7.8% are weak evidence: Thursday 0DTE gamma makes a
  35% gain possible, not probable or guaranteed.
- No new entries during close-only state.

Risk decisions return machine-readable reason codes. A rejected intent remains visible on the dashboard.

The risk layer calculates quantity as the floor of the minimum of the effective
per-structure budget, remaining correlated-cluster budget, and remaining total budget,
divided by per-contract worst-case loss. If one contract exceeds any remaining cap, the
trade is rejected rather than weakened by changing the defined-risk structure. The chosen
tier, its thresholds, effective percentage, and effective dollar budget are recorded in
the Decision Passport risk checks.

An operational kill switch must exist in persistent application state. It is independent of environment configuration and immediately prevents new entries while preserving cancel and close authority.

## 11. Execution and reconciliation

### 11.1 Order requirements

- Alpaca paper account only.
- Multi-leg order where supported.
- Limit orders derived from current leg quotes.
- Unique deterministic client order ID.
- Explicit strategy, candidate, decision, and environment identifiers.
- Idempotency check before every submission.

### 11.2 Order lifecycle

```text
PROPOSED -> RISK_APPROVED -> SUBMITTED -> PARTIALLY_FILLED/FILLED
         -> RISK_REJECTED
SUBMITTED -> CANCELED/REJECTED/EXPIRED
FILLED -> OPEN -> CLOSING -> CLOSED
```

Stale orders are canceled and may be repriced only within a small deterministic concession budget.
Opening orders are canceled immediately at the entry cutoff. Closing orders reprice only their
remaining quantity. Soft exits such as maximum-hold and profit-target closes use quote-aware
backoff, a longer stale window, and bounded concessions; an unchanged market does not trigger a
five-minute cancel/replace loop. Urgent stop, daily-loss, hard-deadline, and reconciliation exits
retain the existing aggressive bounded path. Ordinary close-repricing exhaustion cancels that
order but does not strand the structure: authoritative remaining exposure becomes eligible for a
new idempotent emergency-close series with a unique deterministic client ID and increasingly
marketable multi-leg limits.

Every approved entry records an effective holding deadline equal to the earliest of the model's
requested hold, the 3:50 PM ET daily hard exit, and Thursday's forced-flatten boundary.
Directional spreads additionally clamp the model hold to 45 minutes and to 15 minutes before the
next scheduled event. At least 30 minutes must remain before the effective deadline or the entry
fails closed.

### 11.3 Reconciliation

Alpaca is authoritative for account state, orders, fills, positions, and portfolio equity. The local database is the authoritative audit and attribution ledger.

On startup and every cycle:

- Fetch open orders and positions.
- Associate them by broker order ID and client order ID.
- Halt new entries on any unexplained mismatch.
- Recover known orders into the local ledger.
- Surface orphaned broker exposure as a critical dashboard incident.

## 12. Performance measurement

There is no public leaderboard. Alpaca's verified competition-account equity at Thursday EOD is
authoritative for the official result. The application maintains read-only self-tracking evidence,
bounded to Monday's scoring start through the Thursday EOD checkpoint and scoped to the configured
account fingerprint. Pre-scoring production rows and post-EOD observations are excluded.

At production initialization, record:

- Account ID fingerprint or redacted suffix.
- Baseline timestamp.
- Baseline equity of $100,000.
- Empty order and position state.

Record on every scheduler cycle (approximately once per minute during liquidation recovery):

- Equity, cash, buying power, and portfolio value.
- Realized and unrealized P&L.
- Peak equity and current drawdown.
- Open defined loss and correlated-cluster loss.
- Positions, orders, and fills.

Trade attribution records:

- Strategy and playbook.
- Decision-time structure midpoint.
- Submitted limit and actual fill.
- Slippage and time to fill.
- Realized P&L and return on maximum risk.
- Maximum favorable and adverse excursion when available.

Headline metrics:

- Competition P&L in dollars and percent.
- Maximum drawdown.
- P&L by playbook.
- Fill and cancellation rates.
- Average slippage.
- Approved, rejected, and abstained decision counts.

Sharpe and Sortino may be displayed as supporting statistics but must not be presented as robust over a five-session sample.

## 13. Data model

Minimum entities:

- `AgentRun`: one observation and decision cycle.
- `MarketSnapshot`: source data and computed features.
- `Candidate`: deterministic eligible structure proposal.
- `Auction`: eligible candidates, comparative ranking, selected candidate, and awarded risk budget.
- `ModelDecision`: validated model output and raw-response hash.
- `RiskDecision`: approval, limits, calculations, and reason codes.
- `OptionStructure`: legs, strikes, expiration, credit/debit, and maximum loss.
- `BrokerOrder`: client ID, broker ID, status, limit, and timestamps.
- `Fill`: execution quantity and price.
- `PositionSnapshot`: broker position state.
- `EquitySnapshot`: account equity and P&L.
- `SystemState`: execution state, kill switch, reconciliation state, and incidents.

Records are append-oriented. Corrections add reconciliation events rather than silently overwriting the audit trail.

## 14. Dashboard and judge experience

The public dashboard must show:

- Current execution state: observe, execute, close-only, halted, or complete.
- Competition equity curve and total P&L.
- Maximum drawdown and current open risk.
- Current structures and pending orders.
- Latest agent decision with evidence, confidence, and invalidation.
- The latest risk-budget auction and the alternatives considered.
- Every deterministic risk check and its outcome.
- Rejected and abstained decisions.
- A Decision Passport joining evidence, alternatives, decision, risk, execution, and outcome.
- Hypothetical counterfactual results in a visually separate, explicitly labeled view.
- Fill quality and strategy attribution.
- A replay button for one canonical completed decision.

The dashboard must not expose API keys, secrets, unredacted internal prompts, or sensitive account details beyond what the competition requires.

### 14.1 Thirty-second demo path

1. Show current account equity and execution state.
2. Open the latest agent run.
3. Show Alpaca-sourced market and option-chain facts.
4. Show the structured model decision.
5. Show the candidate auction and why one structure won—or why cash won.
6. Show deterministic risk approval or rejection.
7. Show the Alpaca order/fill or the abstention reason.
8. Show the resulting P&L, counterfactual alternatives, and Decision Passport timeline.

## 15. Reliability, observability, and safety

Required controls:

- Structured logs with decision, order, and request correlation IDs.
- Health endpoint covering database and MCP connectivity.
- Scheduler heartbeat and last-success timestamp.
- Alert on account mismatch, stale data, unreconciled position, rejected close, or drawdown halt.
- Retries only for idempotent reads or broker-safe operations.
- Exponential backoff for rate limits.
- No secrets in logs, exceptions, screenshots, or model context.
- All production writes and orders tagged with `competition` role.

Paper trading has optimistic fill assumptions. The product must emphasize quoted liquidity, spread, slippage, and order discipline rather than claim paper results guarantee live performance.

## 16. Proposed implementation stack

Preferred implementation for first-class MCP integration and competition speed:

- Python 3.12.
- FastAPI application and API.
- Pydantic schemas for model and risk contracts.
- SQLAlchemy and Alembic.
- PostgreSQL in production; PostgreSQL or SQLite for local tests.
- Official Alpaca MCP server v2 through the MCP Python client.
- OpenAI structured model output, with a provider boundary for Featherless if used.
- Server-rendered templates or HTMX plus Tailwind and Chart.js for the dashboard.
- Pytest for unit, integration, replay, and boundary tests.
- Docker deployment on Render.

The critical domain layer must not depend on FastAPI, the model provider, or Alpaca transport details. Market data, model decisions, risk decisions, and broker execution use explicit interfaces so replay tests can replace all external services.

If the kickoff MCP spike cannot submit and reconcile a paper multi-leg structure through the selected MCP path, the fallback must be documented in an ADR. Direct Alpaca Trading API execution is acceptable only if MCP remains a meaningful part of the autonomous state and market-data loop and the competition requirement is still satisfied.

## 17. Testing and acceptance criteria

### 17.1 Unit tests

- Competition-clock boundaries.
- Account-role and environment mapping.
- Account mismatch and live-endpoint rejection.
- Model-schema validation and abstention fallback.
- Strike selection and structure maximum loss.
- Quantity calculation and every risk cap.
- Correlated-cluster risk.
- Drawdown and daily-stop calculations.
- Idempotent client order IDs.

### 17.2 Integration tests

- Development-account MCP authentication.
- Account and portfolio history retrieval.
- Option-chain and quote retrieval.
- One small multi-leg paper entry.
- Fill and position reconciliation.
- Cancel and close lifecycle.
- Restart with an open position.
- MCP timeout and rate-limit recovery.

### 17.3 Production acceptance

Before the first competition-account order:

- The production account ID assertion passes.
- Baseline is recorded at $100,000.
- Competition state is `FULL_EXECUTION`.
- Development round-trip integration test passes.
- Production read-only account, clock, portfolio, chain, and quote calls pass.
- All risk and boundary tests pass.
- Dashboard and kill switch are available.
- The intended order is within every portfolio limit.

Any failed acceptance item blocks the first order.

## 18. Submission deliverables

- Submission title: Money Machine.
- Short description, 50-255 characters.
- Long description, 600-2,000 characters and at least 100 words.
- Categories: Finance, Investment, Web Application, and ProjectFromScratch if allowed.
- Track: Options Alpha Agents.
- Technologies: Alpaca and only the model/infrastructure technologies actually used.
- Public GitHub repository with MIT license.
- Hosted public dashboard and demo URL.
- Demo video.
- Pitch deck.
- Competition Alpaca account ID.
- One-page AI logic, risk gates, and Alpaca infrastructure write-up.
- Up to five social post links.
- Final performance snapshot and exported audit evidence.

All submission copy is maintained in `SUBMISSION.md` before it is pasted into Lablab.

## 19. Build plan

### Kickoff day

- Finalize this specification and architecture decision.
- Add license, README, environment example, and secret checks.
- Scaffold application, database, tests, and Docker environment.
- Prove Alpaca development-account identity and read-only MCP calls.
- Prove one development multi-leg round trip before competition-account execution.

### Weekend

- Implement agent, feature, playbook, risk, order, and reconciliation layers.
- Implement replay fixtures and end-to-end demo path.
- Build and deploy the dashboard.
- Exercise failure cases and restart recovery.

### Monday

- Run production read-only validation.
- Permit small competition execution only after all acceptance gates pass.
- Monitor fill quality and reconciliation.

### Tuesday

- Make one evidence-based calibration pass.
- Freeze strategy, risk limits, and architecture by market close.
- Draft submission copy, one-page write-up, and deck.

### Wednesday

- Complete reliability work and public dashboard polish.
- Record primary demo video and backup footage.

### Thursday

- Prepare final evidence before the one permitted submission.
- Stop new entries at 2:30 PM ET and cancel every pending opening order.
- Begin forced liquidation no later than 3:15 PM ET; target broker-confirmed flat by 3:45 PM ET.
- Capture Alpaca's authoritative EOD equity at 4:00 PM ET, including exercise/assignment effects.
- Enter the account ID only in the private submission field and submit once on Thursday.

### Friday

- Preserve close-only recovery for any residual broker exposure.
- Treat 9:30 AM ET as the formal hackathon/scoring end, not additional trading time.
- Do not make a second submission.

## 20. Strategy and scope-change governance

Before Tuesday's strategy freeze, a change requires:

- A stated failure or opportunity.
- Evidence from replay, development fills, or competition observations.
- An explicit risk impact.
- Tests and an audit-log change when behavior changes.

After strategy freeze, only eligibility, security, reliability, reconciliation, or presentation fixes are allowed. No new playbook, universe expansion, or leverage increase is permitted.

## 21. Definition of done

Money Machine is complete when:

- The autonomous paper agent is deployed and operating against the correct fresh account.
- Every decision and order passes the defined AI and deterministic contracts.
- Account state and local records reconcile continuously.
- Competition P&L and drawdown are visible and reproducible from Alpaca data.
- The application can replay a canonical decision without live-market dependence.
- The repository is public, MIT-licensed, documented, tested, and contains no secrets.
- The application URL, video, deck, write-up, account ID, and submission copy are ready before the deadline.
- The final submission is completed with sufficient time for recovery from platform failure.
