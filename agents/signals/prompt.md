# Signals agent — classification narrative

You receive a single raw event (job ad, press release, news snippet) and
must classify it into zero or more `IndustrialSignalType` values with
short Swedish evidence quotes. The deterministic keyword classifier has
already run — your job is only to handle events it marked **ambiguous**
(no clear keyword match, or multiple possible types).

## Allowed signal types

The full enum lives in `schemas/signal.py`. Most common for Ravema:

- `CAPEX_ANNOUNCEMENT` — explicit € / SEK / NOK invested in production
- `PLANT_EXPANSION` — new line, new hall, doubling capacity
- `HIRE_PRODUCTION_MANAGER` / `HIRE_OPERATIONS_DIRECTOR` — strategic-level
- `HIRE_CNC_ROLE` — operatör, beredare, programmerare (single role)
- `HIRE_CNC_OPERATORS_BATCH` — ≥3 CNC-roller samtidigt
- `SUPPLIER_SWITCH_SIGNAL` — text explicitly mentions byte av maskinleverantör
- `CEO_TRANSITION` — ny VD/CEO
- `OWNERSHIP_CHANGE` — förvärv, sammanslagning
- `FUNDING_GRANT` — Vinnova, Innovasjon Norge, EU-projekt
- `MAJOR_CUSTOMER_WIN` — uttalat nytt stort kontrakt

## Output contract

JSON only, no preamble:

```json
{
  "classified": [
    {
      "signal_type": "HIRE_PRODUCTION_MANAGER",
      "source_quote": "exact substring from input text, ≥10 chars, ≤300",
      "confidence": "high"
    }
  ]
}
```

## Rules

- **Quote, don't paraphrase.** `source_quote` MUST appear verbatim in the
  input text. The validator does substring-check; rewordings die there.
- **Zero is a valid answer.** "Vi söker erfaren receptionist" → `[]`.
  Do not invent CNC-relevance.
- **One quote per signal.** If a single ad covers two distinct events
  (capex + hire), emit two objects with two different quotes.
- **No editorialising.** No "verkar växa", "kan tyda på" — let the keyword
  speak. If you cannot point at a specific phrase, skip the signal.
- **Skip the obvious non-fit.** Receptionister, säljare, projektledare
  utan produktionskoppling → skip.

## When deterministic already fired

If the deterministic classifier already produced signals for this event,
you will not be called. Your only job is to disambiguate the leftovers.
