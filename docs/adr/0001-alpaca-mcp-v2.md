# ADR 0001: Official Alpaca MCP Server V2 is the broker boundary

Status: accepted

## Decision

Money Machine launches Alpaca's official `alpaca-mcp-server` V2 over stdio with the official MCP Python client. The adapter discovers the live tool list and fails closed when a required V2 tool is missing. It uses MCP for account identity/state, market clock, portfolio history, orders, fill activities, positions, SPY/QQQ/IWM stock snapshots, option chains, option quotes, cancellations, and multi-leg option orders.

The order adapter follows V2's current `place_option_order` contract: `order_class=mleg`, `type=limit`, `time_in_force=day`, one strategy quantity, up to four explicit legs with ratio, side, and position intent, and negative net limit for a credit structure.

## Data caveat

The V2 option-chain snapshot can omit daily contract volume or open interest depending on the underlying Alpaca response/feed. Missing fields are assigned zero so the liquidity compiler rejects the structure. It does not infer liquidity from trade size or relax policy to force a candidate. A production calibration may enrich shortlisted contracts through additional V2 contract/bar tools, but it may not bypass this fail-closed behavior.

## Direct API fallback

No direct Trading API fallback is currently required: V2 exposes multi-leg placement, cancellation, order retrieval, positions, activities, and portfolio history. If an MCP execution/reconciliation operation is later proven unsupported, a separate ADR and tests are required before adding a narrowly scoped direct API adapter. MCP must remain the meaningful account and market workflow.

Source reviewed during implementation: the official Alpaca MCP Server V2 README and current `place_option_order` override in `alpacahq/alpaca-mcp-server`.
