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
- Video: pending; maximum five minutes
- Pitch deck: `output/presentation/money-machine-tournament-postmortem-final.pptx`
- Required one-page explanation: slide 2 covers AI logic, risk gates, and Alpaca infrastructure
- Competition account ID: provide directly in the private submission field; never commit here
- Final official performance snapshot: $86,213.90 at Thursday 4:00 PM ET
- Submission deadline: Friday, September 4 at 11:00 AM ET

## Final submission checklist

- [x] Capture the authoritative Thursday 4:00 PM ET Alpaca equity: $86,213.90.
- [x] Confirm the official account is flat with no working orders.
- [x] Add one visible page covering AI logic, risk gates, and Alpaca implementation.
- [x] Update the deck with actual, modelled, and lesson-learned results.
- [ ] Record and verify a video under five minutes.
- [ ] Add the final public dashboard and video links.
- [ ] Generate the final performance export and verify that Alpaca is authoritative for the result.
- [ ] Enter the competition account ID only in the private submission field. Never paste it into
  the repository, public dashboard, video, deck, or public submission copy.
- [ ] Submit by Friday, September 4 at 11:00 AM ET.
