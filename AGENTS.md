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
confirmation gates: an 11% daily-loss boundary against the fixed session baseline, and one
qualifying high-conviction SPY/QQQ/IWM directional debit spread sized up to 12% of current equity
within the 12% correlated-index cluster cap. Thursday high-conviction directional exposure uses
a 2.10-times opening-debit take-profit, a 0.65-times premium-value stop, and a 45-minute maximum
hold. The one-shot 12% sizing tier additionally requires no concurrent correlated-index exposure.

This authorization never weakens the official-account and paper-only guards, atomic defined-risk
MLEG requirement, deterministic caps, reconciliation, liquidity, data, event, lifecycle,
idempotency, no-pyramiding, cutoff, forced-flatten, or emergency controls. Agents must not invent
an additional approval pause when those deterministic gates pass, and must not add risk beyond
their limits.
