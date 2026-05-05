# P7 — Research

Goal: ground intake or iterate decisions in external evidence. Output is one markdown note per topic under `research/`.

## Tools (in priority order)

1. `WebSearch` for plain web lookups (papers, blog posts, financial news on a strategy class).
2. `WebFetch` to pull a specific URL (arxiv abstract, SSRN paper page, Investopedia article).
3. `mcp__plugin_context7_context7__query-docs` for library / math / API documentation (numpy, py_vollib, pandas_market_calendars). Use when a snippet involves a library API the agent is unsure about.

Skip any tool that is unavailable; if all three are unavailable, write `research/UNAVAILABLE.md` and return.

## When to invoke

- From P1: strategy class is unfamiliar OR user cites a paper. Validate plausibility and pull default-parameter ranges.
- From P6: a result is surprising and the user asks "why". Find published evidence for or against the observed effect.

## Note format

`research/<topic-slug>.md`:

```
# <topic>

Source: <url or paper title>
Queried: <YYYY-MM-DD>
Tool: WebSearch | WebFetch | context7

## Summary
<3-5 bullets, plain English>

## Implication for strategy.py
<1-3 bullets tying back to fields in META or signal logic>

## Citations
- <full citation 1>
- <full citation 2>
```

Append a one-line entry to `research/_log.md` for every note created.

## Anti-fabrication rule

Never write a citation you did not retrieve. If WebSearch returns nothing, write `Source: NONE`, mark confidence low, do not invent.
