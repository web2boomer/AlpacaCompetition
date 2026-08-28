# ADR 0002: Execution authority is derived, never toggled

Status: accepted

Money Machine does not define `EXECUTION_ENABLED`. Settings reject the variable if present.

Development paper entry authority requires the development environment/role pair, paper endpoint and transport, exact account identity, open market, clean reconciliation, inactive persistent kill switch, valid candidate/model/risk decisions, and idempotency checks.

Competition entry authority additionally requires the immutable competition window, exact production/competition mapping, recorded $100,000 baseline, every acceptance gate, development round-trip evidence, and `COMPETITION_GO_LIVE_AUTHORIZED` set in reviewed version-controlled code after explicit owner authorization. It is `False` in this build. Cancel and close authority is not removed by the entry kill switch or entry cutoff.
