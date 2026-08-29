# ADR 0002: Execution authority is derived, never toggled

Status: accepted

Money Machine does not define `EXECUTION_ENABLED`. Settings reject the variable if present.

Development paper entry authority requires the development environment/role pair, paper endpoint and transport, exact account identity, open market, clean reconciliation, inactive persistent kill switch, valid candidate/model/risk decisions, and idempotency checks.

Competition entry authority additionally requires the immutable scoring window and exact production/competition mapping. Every live cycle verifies the configured paper account before it can reach risk approval. Until the first locally managed competition order exists, the live path also requires an untouched $100,000 baseline with no orders, positions, or fills. There is no environment toggle, manual code latch, or persisted acceptance latch. The read-only acceptance command remains a preflight diagnostic, while `ACCOUNT_ROLE` and the clock determine entry authority. Cancel and close authority is not removed by the entry kill switch or entry cutoff.
