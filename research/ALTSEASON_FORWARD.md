# Frozen KC paper observations v1

This is an independent research logger for KC50 R1/R2. It does not change scores, weights, orders, the CY logger, or existing observations.

- Definitions: `altseason-forward-spec.json`, copied without parameter search from the ninth-round specification. Both strategies have separate paper portfolios. Primary fixed horizon20; diagnostics10/40. The unsuccessful overheating exit is not activated.
- Schedule: the existing weekday update job at09:15UTC /17:15Asia/Shanghai. GitHub scheduling can be delayed. Holidays or unavailable current data produce a status, not an invented trading day. No extra ChatGPT reminder is needed.
- Only process today's snapshot generated after15:10Asia/Shanghai, while current time is also after15:10. Historical prices may warm rolling features, but first waiting states start empty on the first logged day. No historical setups or signals are imported.
- KC raw index OHLC: Tencent raw `day`, cross-checked against quote history and CSI closing prices. Amount: CSI000688 `tradingValue`, native units and within-series ratios. Benchmark: dated SH000001 close. At least65 common observations, ordinarily85, are required.
- Each first daily observation preserves the actual input window, timestamp, feature values, wait states, events, source descriptions, snapshot digest and hash chain. An input failure does not append a valid observation; a later observation gap cancels waiting. No replay reconstructs missed signals or entry fills from subsequent historical data.
- Signal is known after close. Signal-day open is never the entry price. The next session's raw index open is first recorded when its postclose snapshot becomes available; before then `entry_price=null`. This is a hypothetical price proxy, not a claim that an ETF order executed at that price.
- For each horizon, deduplication consumes overlapping events and reserves pending positions. No mature result means `win_pct=null`; a missing held-session record is unscorable, never a win. Strategies and horizons are separate observations, not a ranked allocation or combined portfolio.
- Existing CY breadth-repair v1 and `research/forward` remain unchanged. New status/journal live under `research/forward-altseason-kc`. Both failure states remain visible; the new step cannot block market-data commits merely because its output directory is absent.
- Do not edit the implementation/spec after the first observation and append to the same journal. A changed engine requires a new version/directory and an explicit record of why.
- Review after3/6 calendar months from the first valid observation. Duration alone is not evidence of adequate sample size; no optimization or automatic promotion to production is scheduled.

Checks: `python -m unittest discover -s tests -p test_altseason_forward.py`.

CLI: `python research/altseason_forward.py --data data.js`.

