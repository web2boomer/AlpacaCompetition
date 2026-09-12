# Money Machine tournament postmortem

## Official result

| Measure | Result |
| --- | ---: |
| Starting equity | $100,000.00 |
| Final official equity | $86,213.90 |
| Net result | -$13,786.10 (-13.79%) |
| Wednesday close | $99,243.24 |
| Final-day change | -$13,029.34 |

The official account was flat with no working orders at the final 16:00 ET equity lock.

## What we tried

1. Monday: small QQQ and IWM neutral index condors.
2. Tuesday: directional selection and lifecycle repairs, including SPY put spreads.
3. Wednesday: shorter-hold IWM and SPY call spreads with more realistic profit capture.
4. Thursday: progressively larger, repeated SPY and QQQ bullish debit spreads intended to recover
   to the $104,000 target.

The final-day order ledger contains 11 paired round trips. Using the stored entry and exit limit
prices gives approximately -$14,231 of completed-spread P&L. That is a diagnostic estimate, not a
replacement for the official equity result.

## Narrow counterfactuals

These models deliberately change one decision and reuse actual Monday fills.

| Scenario | Method | Modelled ending equity |
| --- | --- | ---: |
| Original Monday strategy | Hold the five filled QQQ condors and the two-contract IWM condor to September 1 expiry; place no later trades | $100,174.00 |
| Freeze after Monday close | Start from $100,445.20 cash after the early QQQ close; settle the remaining expiry obligation at $357; place no later trades | $100,088.20 |
| Stop after Wednesday | Use the observed Wednesday closing equity and place no final-day recovery trades | $99,243.24 |
| Actual | Official competition equity at the 16:00 ET lock | $86,213.90 |

The hold-to-expiry model uses QQQ $707.71 and IWM $290.65 at September 1 expiry. Its structure-level
P&L totals +$174. It would have beaten the actual result by $13,960.10, but it still would have
finished $3,826 below the $104,000 target.

## Lessons

- Pre-agreed safety policy became difficult to change during a short tournament. Future systems
  need pre-approved operating modes with explicit evidence gates.
- Deployment latency changed the opportunity being traded. A fast, tested canary path matters as
  much as model latency.
- Extra model reasoning did not create a measurable edge. Candidate quality, entry timing and exit
  calibration mattered more.
- We changed sizing, cadence, entry logic and exits together, which made attribution impossible.
- SPY, QQQ and IWM created apparent variety while often expressing the same broad-market direction.
- Rejected-trade and hold/exit counterfactuals should have been scored continuously from day one.

## Evidence boundary

Actual equity values come from the official Alpaca competition account snapshots. Counterfactuals
are modelled and are labelled as such. They do not imply that the alternative could have been
executed without slippage, assignment effects or operational error.
