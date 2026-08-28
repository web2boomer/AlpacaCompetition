# Security and safety

Do not report vulnerabilities with credentials or account identifiers in a public issue. Rotate any credential suspected of exposure and stop the scheduler with the persistent kill switch while preserving cancel/close operations.

Local secrets belong only in `.env.development.local` or `.env.competition.local`, must have mode `0600`, and are ignored by Git and Docker. Logs, exceptions, model context, API responses, screenshots, and test fixtures must never include secret values. Status commands report only present/missing.

The application rejects live Alpaca URLs, non-paper transport, account mismatches, invalid environment/role pairs, and the forbidden `EXECUTION_ENABLED` variable. The public dashboard contains only a one-way account fingerprint in live mode.
