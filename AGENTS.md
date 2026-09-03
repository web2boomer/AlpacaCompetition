# Project mission

The shared objective of every agent working in this repository is to win the Alpaca AI Trading Agents Hackathon.

Evaluate product, engineering, trading, risk, deployment, and presentation decisions by how much they improve the probability of winning under the official judging criteria. Account equity and P&L matter, so agents must explicitly identify when a conservative choice materially limits competitive upside and propose an evidence-backed alternative.

Winning does not mean maximizing raw broker buying power or accepting uncontrolled ruin risk. Preserve the official rules, fixed account guard, defined-risk structures, deterministic sizing caps, reconciliation, auditability, and emergency exits. Within those hard constraints, prefer decisive changes with positive expected competition value over inactivity or conservatism by default.

For any material strategy or sizing recommendation, report:

- expected contribution to competition performance;
- worst-case defined loss and correlated exposure;
- evidence supporting the edge;
- the condition that blocks, reduces, or exits the risk; and
- how the result will be verified from the official competition account.

The fixed development and production account mapping is recorded in `docs/COMPETITION_ACCOUNTS.md` and must not be changed during the scoring period.

# Final-day standing authorization

As of 2026-09-03, Alex has granted standing authorization through the official competition equity lock for agents to implement, push, and deploy validated, in-scope Money Machine production changes without repeatedly requesting discretionary approval. Time-to-production is a competition constraint: do not pause a ready change merely to reconfirm an already authorized policy.

The earlier 11% daily-loss boundary and 12% correlated index-cluster limit are superseded for the remainder of 2026-09-03 by Alex's explicit final-day risk-on authorization. The authorized final-day policy disables daily-loss and competition-drawdown entry halts, raises both correlated index-cluster and total defined-loss limits to 24% of current equity, and permits one additional independently validated atomic defined-risk structure per already-used index underlying. This is a one-time competition policy and must not survive the 16:00 ET equity lock.

Every final-day directional debit spread retains a 2.10x opening-debit take-profit, 0.65x debit stop, and 45-minute maximum hold. New entries still end at 14:30 ET, forced flatten still begins at 15:15 ET, the internal flat target remains 15:45 ET, and the official equity lock remains 16:00 ET.

This standing authorization does not permit agents to remove the official-account fingerprint, paper-only transport, atomic defined-risk MLEG execution, the final-day 24% deterministic aggregate cap, reconciliation, data freshness, liquidity, event, lifecycle, order idempotency, new-entry cutoff, forced-flatten, audit, or emergency-exit controls. It also does not override any approval that the execution platform itself requires or authorize unrelated destructive, credential, billing, or external-account actions.

# Final-hour recovery mandate

As of 15:00 ET on 2026-09-03, Alex has authorized a final-hour recovery policy whose sole competition objective is to reach and lock at least $104,000 of official account equity. This section supersedes the earlier final-day per-structure sizing, structure-count, new-entry cutoff, and timing values only until the 16:00 ET equity lock. It must not be reused after the competition.

The strategy may allocate the full remaining 24% aggregate defined-loss budget to one independently validated, high-conviction, same-day SPY, QQQ, or IWM directional debit spread. Existing correlated defined loss counts against that budget. The selected structure must have fresh two-cycle directional confirmation, current liquid quotes, deterministic positive reward-to-risk, sufficient executable depth for the full quantity, and an exact invalidation and exit path. Do not manufacture a trade when those conditions are absent, alternate positions randomly, or submit overlapping orders while a parent is pending or unreconciled.

For this final-hour structure, the per-structure cap equals the remaining headroom under the 24% total and correlated-index caps. New entries may continue through 15:20 ET. Forced flatten begins at 15:35 ET, the required internal flat target is 15:50 ET, and the official equity lock remains 16:00 ET. The final-hour structure uses a 2.10x opening-debit take-profit, a 0.65x opening-debit stop, and a maximum hold ending no later than 15:35 ET. When verified official equity reaches $104,000, immediately close all exposure, block further entries, and preserve the result.

This mandate does not remove the official-account fingerprint, paper-only transport, atomic defined-risk MLEG execution, the 24% deterministic aggregate cap, reconciliation, data freshness, liquidity, event, lifecycle, order idempotency, audit, target lock, or emergency-exit controls. Deployment itself must not mutate broker state; only an ordinary scheduler cycle may trade.

# Expiring final-window amendment

As of 15:25 ET on 2026-09-03, Alex has authorized the ordinary scheduler to evaluate the full SPY, QQQ, and IWM directional auction on every five-minute cycle and submit at most one independently eligible atomic debit-spread entry per cycle. No fixed final-window entry count applies. Every entry must remain within authoritative remaining 24% correlated-index and total defined-loss headroom; no pending or unreconciled entry may overlap another.

New-entry authority includes the exact 15:45 ET boundary, forced flatten begins at 15:50 ET, the required broker-confirmed flat target is 15:57 ET, and the official 16:00 ET equity lock is unchanged. Each entry is sized only from live remaining headroom and its maximum hold is clamped to 15:50 ET. A stopped same-underlying setup may requalify only with fresh independent two-cycle confirmation; an identical stale signal remains blocked. This amendment expires at the equity lock and does not authorize manual broker orders or invalid candidates.

# Continuous production delivery

Production is continuously deployed during the competition. Every validated commit intended for `main` must be pushed and deployed promptly; agents must not accumulate completed, undeployed production commits.

For every production commit:

- Run the complete required quality gates before pushing, including formatting/lint, strict typing, migrations where applicable, and the full test suite.
- Deploy the dashboard and scheduler at the exact same Git SHA and verify both Render deploys reach `live`.
- If the commit contains a database migration, deploy the dashboard first, require its pre-deploy migration to complete successfully, and only then deploy the scheduler. Do not race a scheduler against unapplied schema.
- Verify the first ordinary production cycle after deployment: official competition account fingerprint, execution state, reconciliation, incidents, positions, working orders, defined-loss totals, and scheduler/dashboard health.
- A deployment must not itself create, cancel, replace, or otherwise mutate a broker order or position. Trading changes must occur only through an ordinary authorized scheduler cycle.
- Report the deployed SHA, service deployment identifiers, test evidence, first-passport verification, and any blocker. If a commit cannot be deployed, escalate immediately rather than silently leaving production behind.
