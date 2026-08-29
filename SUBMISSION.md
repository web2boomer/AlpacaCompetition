# Money Machine submission draft

## Short description

An auditable AI volatility governor that auctions a finite risk budget across defined-risk Alpaca paper option structures—and makes abstention explainable.

## Long description

Money Machine is an autonomous options agent built for the Options Alpha Agents track. It observes SPY, QQQ, and IWM market and paper-account state through Alpaca MCP Server V2, compiles only fully bounded multi-leg structures, and lets a structured model choose among those candidate IDs or hold cash. The model never controls strikes, quantity, account, endpoint, order type, or risk limits.

Every selection passes deterministic liquidity, event, timing, correlation, daily-loss, drawdown, position-count, and total-defined-loss checks. An approved structure receives a position size calculated from worst-case loss and rounded down; a rejected structure stays visible. Broker orders use deterministic client IDs and reconciliation halts new entries on unexplained exposure.

The public dashboard is organized around a Decision Passport that joins evidence, alternatives, model reasoning, every risk check, execution, and outcome. Replay and counterfactual results are conspicuously separated from official Alpaca competition P&L. Money Machine's differentiator is not an LLM that trades at any cost—it is an auditable volatility governor that proves why a trade, or cash, deserved the portfolio's scarce risk budget.

## Categories and technology

- Categories: Finance, Investment, Web Application, ProjectFromScratch
- Track: Options Alpha Agents
- Technology: Alpaca MCP Server V2, OpenAI structured outputs, FastAPI, PostgreSQL, SQLAlchemy, Render

## Required links and final evidence

- Repository: https://github.com/web2boomer/AlpacaCompetition
- Dashboard: pending deployment
- Video: pending
- Pitch deck: pending
- Competition account ID: provide directly in the private submission field; never commit here
- Final official performance snapshot: pending

## Thursday operational submission checklist

- [ ] Prepare the final performance export, dashboard capture, video, deck, and written evidence
  before opening the submission form.
- [ ] Confirm new entries stopped at 2:30 PM ET and no opening order remains working.
- [ ] Confirm forced liquidation began by 3:15 PM ET.
- [ ] At the 3:45 PM ET internal target, confirm Alpaca positions and relevant working orders are
  both empty; if not, preserve close-only recovery and record the incident.
- [ ] Capture the authoritative Alpaca account-equity checkpoint at Thursday 4:00 PM ET after any
  exercise or assignment effects.
- [ ] Generate `money-machine competition-performance-export` and verify that it states Alpaca is
  authoritative for the official result.
- [ ] Enter the competition account ID only in the private submission field. Never paste it into
  the repository, public dashboard, video, deck, or public submission copy.
- [ ] Verify every final artifact before submission. The submission is made once on Thursday; do
  not rely on a correction or second submission.
