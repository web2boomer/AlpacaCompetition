# Competition account mapping

The account roles are immutable for this competition deployment:

| Environment | Purpose | Redacted account fingerprint |
| --- | --- | --- |
| Local development | Development and acceptance testing only | `ea097b90f9d4` |
| Render production | Official competition execution only | `2e10efeeb330` |

Exact account identifiers, API keys and secret keys remain only in ignored role-specific local
environment files or Render secrets. They must never be committed, logged, copied into public
documentation, or shown in the deck, video, dashboard capture, or social posts. The fingerprints
above are the first 12 hexadecimal characters of SHA-256 over each expected account identifier and
are sufficient for redacted operational verification.
