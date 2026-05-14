# 05_orb_acceptance_short_no_claims

**URL:** https://youtu.be/IuvsvMUon5k

**Why this video:** A 99-word YouTube Short pitching an "Opening Range Breakout buyer-acceptance filter." The presenter claims high win rates and stop-loss reduction but provides no instrument, no timeframe, no quantified threshold for "acceptance," and no defined exit rules. The agent classifies as `educational` with `has_checkable_claims: false` and short-circuits to a summary-only report.

The behavior to notice: the transcript IS shaped like a strategy claim — "first candle break, buyers dominate, very high win rate" — but on close read it's directional advice, not a checkable relationship. The agent's job here is to refuse to treat hand-wavy strategy talk as a testable claim. A different run of the same LLM might (correctly) classify this as `strategy_or_claim` with a `partial`-testable claim and `strategy_backtest` test type; either honest output is acceptable. What's NOT acceptable is fabricating a test against a phantom instrument.
