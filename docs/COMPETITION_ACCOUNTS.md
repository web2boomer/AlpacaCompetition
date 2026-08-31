# Competition account mapping

The account roles are immutable for this competition deployment:

| Environment | Purpose | Alpaca paper account |
| --- | --- | --- |
| Local development | Development and acceptance testing only | `PA3DHUGI63AA` |
| Render production | Official competition execution only | `PA3MX339UDPS` |

The account identifiers are verification values, not credentials. API and secret keys remain
only in ignored role-specific local environment files or Render secrets. They must never be
committed, logged, or copied into documentation.
