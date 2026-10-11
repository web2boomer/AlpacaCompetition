# Alpaca Competition pull request AutoMerge

The coordinator and independent review workflows are installed but remain inactive until `COORDINATOR_BOOTSTRAP_ACTIVE=true`. They operate only on same-repository pull requests targeting `main`. An agent submits a ready PR with `automerge:agent`; the coordinator checks the declared `## Merge dependencies`, current head and base, required CI, independent review, holds, and branch protection before queueing one PR. `automerge:ready` is coordinator-owned.

## Activation gates

1. Confirm the official competition and production release policy still permits unattended merge. A merge to `main` must not race the dashboard and scheduler deployment or bypass exact-SHA, migration, or first-cycle verification. If that cannot be guaranteed, keep the bootstrap off.
2. Confirm GitHub native auto-merge is available and `main` protection requires the existing `verify` CI check, `PR coordinator fixtures`, `Codex review gate`, and `Automerge coordinator gate`, with conversation resolution and no credential bypass. Verify the check names on an actual PR run.
3. Configure least-privilege `AUTOMERGE_TOKEN` and `OPENAI_API_KEY` secrets only after the private patch review transfer and API use are authorized. Never place credentials in PRs or logs.
4. Set `COORDINATOR_REQUIRED_CI` to `["verify","PR coordinator fixtures"]`, `COORDINATOR_PROTECTION_VERIFIED=true`, and `COORDINATOR_AUTO_MERGE=true` after verification. Set `COORDINATOR_BOOTSTRAP_ACTIVE=true` last.
5. Run a harmless same-repository test PR with `## Merge dependencies` followed by `None`. Confirm failed and pending gates hold it and successful gates admit only one PR.

Until these gates are complete, report Alpaca Competition as **installed, inactive**. Installation does not authorize trading, deployment, or relaxation of competition controls.
