---
name: estimate-revision
description: Compare house estimates with the latest consensus and write a short estimate-revision note, with a table of house versus consensus, the gap on each line, and what to revise. Use when asked how our numbers compare with the street, whether to raise or cut estimates, or for an estimate check before a print. Not for building models, DCFs, comps or full earnings previews.
---

# Estimate revision

Compare the house estimates with consensus for one company and period, and say what to revise.

## Workflow

1. Read the house estimates file and the consensus file. Note each file's as-of date and source.
2. For each line that both files have (revenue, non-GAAP EPS, gross margin), compute the gap: house minus consensus, as an amount and as a percentage of consensus.
3. Decide an action per line:
   - Gap within 1%: "In line, no change".
   - Gap above 1%: "Review", and give the likely reason if the materials state one.
4. Write the note in the format below.

## Output format

```
## Estimate revision: <company> <period>

| Line | House | Consensus | Gap | Gap % | Action |
|---|---|---|---|---|---|

**What to revise:** one to three bullets.
**Sources:** <house file, as-of date>; <consensus source, as-of date>.
```

## Rules

- Never invent a number. If a period or line is missing from either file, say "not in the materials" and leave the gap empty.
- Keep the house rating and price target unless the user asks to change them. Then tie the change to the gaps.
- Show revenue in $M with one decimal, EPS in dollars with two decimals, and margins in % with one decimal.
- Keep the note to one screen.
