# 04_alphainsider_promo_walkthrough

**URL:** https://youtu.be/RzJIhCWj3rQ

**Why this video:** A promotional walkthrough of AlphaInsider + Alpaca for auto-trading TradingView strategies. Shows the agent correctly classifying as `promotion` and short-circuiting to a summary-only report. This exercises the `summary.skip_extraction == True` path: when the video is, by nature, not a claim about market behavior (a course pitch, a platform demo, a community ad), the pipeline doesn't run claim extraction at all — it returns the summary plus an honest "nothing to validate, and why."

The behavior to notice: the agent does NOT manufacture trading claims out of a software demo just because "TradingView" appears in the transcript. That kind of forced-extraction failure mode is what `summary.py` is there to prevent.
