---
name: guidance-tracker
description: "Compare the guidance management gave last quarter with what the company reported this quarter, line by line, and write a short guidance scorecard: met, beat or missed, by how much, plus new, raised, cut or withdrawn guidance and the reasons management gave. Use when asked whether a company hit its guidance, how results compare with what management guided, or what changed in the outlook. Not for full earnings update reports (use earnings-analysis), pre-earnings previews (use earnings-preview), house versus consensus comparisons (use estimate-revision), changing model numbers (use model-update) or checking an investment thesis (use thesis-tracker)."
---

# Guidance tracker

Compare last quarter's management guidance with this quarter's reported results for one company, and say what was met, beaten or missed.

## Workflow

1. Read the prior-quarter source (earnings call or release) and the current-quarter sources (press release, call transcript). Note each source's date.
2. List every guided line in the prior source: period, metric, and the range or point given.
3. For each line, find the reported figure in the current sources. Only compare lines that both sources cover, and compare like with like (a quarter with a quarter, a year with a year).
4. Decide a result per line:
   - Reported figure inside the range: "Met".
   - Above the top of the range (or above a point guide): "Beat". Below the bottom: "Missed".
   - Give the gap to the nearest end of the range and to the midpoint, as an amount and a percentage.
5. List guidance changes: new, raised, cut or withdrawn, with the old and new range and the reason management gave. If no reason is stated, say so.
6. Note any prior statement that the current quarter contradicts, such as a "no further charges" comment.
7. Write the scorecard in the format below.

## Output format

```
## Guidance scorecard: <company> <period>

| Line | Prior guidance | Reported | Result | Gap vs range | Gap vs midpoint |
|---|---|---|---|---|---|

- **Guidance changes:** old range to new range, with management's reason.
- **Contradicted or withdrawn:** one bullet each, or "none in the materials".
- **Not covered:** lines with no guidance or no reported figure.
**Sources:** <prior source, date>; <current sources, dates>.
```

## Rules

- Never invent a figure. If a line has no prior guidance or no reported figure, write "not in the materials" and leave the result empty.
- Use the company's own wording for reasons. Do not guess a reason that was not stated.
- If two sources disagree on a figure, say so and use the press release.
- Show revenue in $M with one decimal, EPS in dollars with two decimals, and margins in % with one decimal.
- Keep the note to one screen.
