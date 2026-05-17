# Why "untestable" is a first-class verdict

The hardest engineering choice in TradingHypothesisLab isn't the Pine synthesis or the MCP wiring. It's a single design rule: **the system is allowed to say "I don't know."**

This sounds trivial. It isn't. Almost every autonomous LLM-driven validation system in production fabricates confidence rather than admitting ignorance. They return scores. They produce numbers. They generate plausible verdicts. They very rarely return "untestable — here's the specific structural reason I cannot evaluate this." That refusal is what this post is about.

## The default failure mode

Build an autonomous validation pipeline naively and the failure mode is invisible. The pipeline runs. The pipeline produces output. The output looks confident — usually a number, sometimes a label like "pass" or "fail." The user sees the output, trusts the number, makes a decision.

The problem: a meaningful fraction of those outputs are confabulations. The system reached the end of its execution and produced *something* because that's what the orchestration code asked it to do. The output is shaped like a verdict but its underlying epistemic state is "I don't actually know."

In trading-video validation specifically, this shows up in three repeating patterns:

- A claim about an ICT-style strategy with entry, stop, and target all specified — but no instrument named. The naive system assumes EURUSD, runs the backtest, and reports a 67% win rate. That number is a hallucination with a confidence interval attached. The video never said EURUSD.
- A claim about "RSI divergence works on volatile pairs" with no measurable threshold for what counts as a divergence and no defined exit rule. The naive system picks a definition and reports a hit rate. The number is meaningless because the definition wasn't in the source claim.
- A claim that's actually just market commentary, mindset content, or a sales pitch — no testable structure at all. The naive system runs through its motions anyway, picks something to test, generates a number.

Each of these is a *worse* output than no output, because users act on numbers more confidently than on silence.

## The architectural choice

The architecture of TradingHypothesisLab has five verdicts: `pass`, `fail`, `partial`, `untestable`, `error`. The first four are normal. The crucial one is `untestable`, and it isn't an exception path — it's an expected, well-tested, first-class result.

`untestable` fires when the system has genuinely reached a state where it cannot responsibly produce a verdict. The trigger conditions are explicit and named:

- **Required input is missing from the claim.** Strategy claim with no named instrument. Indicator claim with no timeframe. Level claim with no measurable price.
- **The claim type isn't supported by the configured test types.** Some kinds of trading content (mindset videos, course pitches, market predictions) don't make checkable claims by nature. The system identifies these earlier in the pipeline and stops before the validation step.
- **Required data isn't available.** Backtest period requested before the instrument existed, or for a symbol the data provider doesn't carry.
- **A downstream tool failed irrecoverably.** TradingView MCP timed out repeatedly. The system doesn't retry into oblivion or fake a number — it returns `untestable — MCP not responding` and persists what it has.

Each `untestable` return ships with a one-sentence reason. The reason isn't a debugging breadcrumb; it's part of the contract. The user reading the report can act on it: "the video didn't specify the instrument" is actionable. "The system encountered an error" is not.

## The implementation principle

The implementation rule that makes this work: **verdicts come from code, not from the model.**

The LLM does extraction. It pulls testable claims out of unstructured transcript text. It synthesises Pine Script when a strategy needs to be backtested. It does not decide whether a claim holds. That decision is a function of real numbers — hit rate, profit factor, trade count — checked against explicit thresholds defined in `config.yml`.

The shape of the verdict logic, simplified:

```python
def compute_verdict(metrics, thresholds):
    if metrics.trades < thresholds.min_trades:
        return "untestable", "fewer than min_trades — insufficient evidence"
    if metrics.profit_factor >= thresholds.pf_holds:
        return "pass", f"PF {metrics.profit_factor:.2f} above threshold"
    if metrics.profit_factor < thresholds.pf_fails:
        return "fail", f"PF {metrics.profit_factor:.2f} below threshold"
    return "partial", f"PF {metrics.profit_factor:.2f} in middle band"
```

There is no LLM call in that function. The same metrics always produce the same verdict. The thresholds are auditable, version-controlled, and explicit. When the system says `fail`, the user can open `config.yml` and see the rule that was applied. When the system says `untestable`, the user can read the one-line reason and know whether the missing input is something they can supply themselves.

This separation matters because it bounds the LLM's stochasticity. The model can be wrong about what claims to extract; it cannot be wrong about whether a 0.91 profit factor crosses the 1.0 threshold.

## The generalisation

This pattern isn't specific to trading. The same architecture applies to any autonomous LLM system that's expected to produce judgments under real-world ambiguity:

- A document-analysis system asked to extract a contract clause that isn't actually in the document. Return `not present` with the section that was searched. Don't generate a plausible-looking clause.
- An eval pipeline asked to score LLM outputs against a rubric that the outputs don't actually map to. Return `rubric mismatch` rather than a fake score.
- A customer-support automation handed a query it doesn't have data to answer. Hand back to a human with the reason it stopped, rather than producing a confident-sounding non-answer.

The shape is the same: define a verdict for "I cannot responsibly answer this," name the conditions that trigger it, ship it as a real output rather than swallowing the exception. The user gains the ability to act on the system's honesty. The system gains trust as a downstream consequence.

## Why this took deliberate engineering

The reason this design choice is rare in production systems is that it works against several local pressures:

- It makes the system look less impressive on the first run. The first videos that go through TradingHypothesisLab mostly return `untestable`. That's the system working correctly — most trading content doesn't make checkable claims — but it doesn't generate the demo-friendly numbers users initially want to see.
- It requires the orchestration code to handle every "I don't know" path explicitly, rather than letting the LLM paper over gaps with plausible output.
- It requires resisting the tempting instinct to add "confidence scores" to compensate. Confidence scores are usually worse than honest refusal, because they create a false sense of measurability around an underlying epistemic state that doesn't support it.

But the system that ships fewer answers, more reliably, is the system that gets used in production. Engineers who build LLM systems against real workloads tend to arrive at the same conclusion eventually: the verdict structure is the most important part of the contract.

---

*Repo: [github.com/rsipavan/TradingHypothesisLab](https://github.com/rsipavan/TradingHypothesisLab). Verdict thresholds in `config.yml`. The `untestable` paths are explicit branches in `report.py`. Full failure-mode taxonomy in [`docs/failure_handling.md`](failure_handling.md).*
