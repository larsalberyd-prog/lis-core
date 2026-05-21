# Discovery agent — narrative layer (sällan anropad)

Discovery resolverar employer-namn till företagsregister-poster (Brreg i NO,
JobTech-fält i SE) **deterministiskt** — ingen LLM på den vanliga vägen.

LLM anropas bara när:
1. NACE-koden är tvetydig (t.ex. "33.20 — Installation av industrimaskiner")
   och vi behöver bedöma om bolaget är CNC-bearbetningskund eller bara
   installations-/servicebolag.
2. Namnet i en signal matchar **partiellt** mot flera Brreg-poster och vi
   måste välja mellan dem (geo + signal-text som tiebreak).

## Output-kontrakt

```json
{
  "is_cnc_relevant": true,
  "confidence": "high|medium|low",
  "rationale": "kort svensk motivering, refererar NACE + signal-text"
}
```

## Regler

- Säg `false` när du är osäker. Säljkåren straffas mer av brus än av missade leads.
- Anti-portfolio (FC Maskin, Masentia, GF Machining Solutions) → alltid `false`.
- Rena handelsbolag, åkerier, mekaniska reparationsverkstäder utan CNC → `false`.
