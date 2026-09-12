# Post-competition paper experiment

## Status

Prepared, not deployed. Do not restart the official competition account before judging is complete.

The code already has a date-independent development path: `APP_ENV=development` and
`ACCOUNT_ROLE=development` use the development paper account and authorize entries only while the
broker reports the regular market open. Therefore, do not delete the historic competition-clock
constants; they remain part of the audit record and official-performance reporting.

## Activation after reset

1. Reset the development paper account to a clean baseline.
2. Put the development account credentials in `.env.development.local` or a separate Render
   environment. Never copy the competition credentials.
3. Set the existing environment mapping:

   ```dotenv
   APP_ENV=development
   ACCOUNT_ROLE=development
   ALPACA_PAPER_TRADE=true
   ALPACA_EXPECTED_ACCOUNT_ID=<development-paper-account-id>
   APCA_API_BASE_URL=https://paper-api.alpaca.markets
   CLIENT_ORDER_PREFIX=mm-dev
   RUN_MODE=live
   MODEL_PROVIDER=openai
   ```

4. Verify the redacted fingerprint, paper endpoint, empty positions, empty orders, clean
   reconciliation and a fresh $100,000 baseline before enabling an ordinary scheduler cycle.
5. Deploy dashboard and scheduler at the same tested SHA. Deployment itself must not create an
   order. Confirm the first ordinary cycle and its Decision Passport.

## Prospective sleeves

Run these as separate ledgers so results remain attributable:

| Sleeve | Thesis | Frozen first test |
| --- | --- | --- |
| Neutral condor | Broad ETF movement finishes inside the implied range | Original Monday compiler and sizing, held to its specified exit |
| Measured directional | Two-cycle trend confirmation can outperform short option time decay | Defined-risk debit spread, realistic take-profit, fixed stop and maximum hold |
| Cash benchmark | Avoiding weak signals has value | No trades; compare with the same daily equity baseline |

Do not run the final-hour recovery policy as a research sleeve. It changed too many variables and
did not demonstrate an edge.

## Promotion rule

Keep each sleeve frozen for at least 20 market sessions. Score return, maximum drawdown, hit rate,
payoff ratio, fill slippage, time in cash, rejected-trade counterfactuals and correlated exposure.
Promote only after prospective paper performance beats the cash and simple ETF benchmarks without
violating the defined-risk, account, reconciliation or order-integrity controls.
