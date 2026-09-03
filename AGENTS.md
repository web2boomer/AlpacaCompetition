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

## Continuous production delivery

Production uses continuous deployment. Every validated commit intended for `main` must be
pushed and deployed promptly; do not leave completed production commits local or undeployed.

For every production commit:

- complete formatting, lint, strict typing, migration, and full test gates;
- push the exact validated commit without force-pushing;
- deploy the dashboard and scheduler at the exact same SHA;
- when a database migration is included, deploy the dashboard first and require its pre-deploy
  migration to succeed before deploying the scheduler;
- verify both deploys are live, then verify the first ordinary production cycle for the official
  account fingerprint, execution state, reconciliation, incidents, positions, working orders,
  defined-loss totals, and service health; and
- verify that deployment itself did not create, cancel, replace, or mutate broker orders or
  positions.

Escalate any blocker immediately rather than leaving production behind. Never manually alter
positions or orders as part of deployment verification.

## Standing final-day authorization

Alex has authorized the Thursday-only final-day recovery policy without repeated discretionary
confirmation gates. For the remainder of 2026-09-03, daily-loss and competition-drawdown states
remain audited but neither halt new entries nor force portfolio liquidation merely because those
loss thresholds remain breached. Reconciliation-safety exits and every position-specific exit
remain active. Correlated-index and total defined-loss caps are both 24% of current equity. At most
one additional independently validated atomic defined-risk structure may be added per already-used
SPY/QQQ/IWM underlying. Every Thursday directional debit spread uses a 2.10-times opening-debit
take-profit, a 0.65-times premium-value stop, and a 45-minute maximum hold.

This authorization never weakens the official-account and paper-only guards, atomic defined-risk
MLEG requirement, deterministic caps, reconciliation, liquidity, data, event, lifecycle,
idempotency, the one-additional-structure ceiling, 14:30 entry cutoff, 15:15 forced flatten, 15:45
flat target, 16:00 equity lock, or emergency controls. Agents must not invent an additional
approval pause when those deterministic gates pass, and must not add risk beyond their limits.
