# lis-core

Lead Intelligence System core — Python agent layer.

Ravema is the launching customer; all client-specific config lives in `clients/ravema.yaml`. If you find yourself writing Python for Ravema-specific logic, the architecture is wrong.

## Quickstart

```bash
pip install -e ".[dev]"
python -m evals.generate_fixtures       # spec/prospects_seed.yaml -> evals/fixtures/, evals/golden/
pytest evals/ -q                        # runs scoring on all fixtures, gate ≥85%
```

## Agent module contract (3-file pattern)

Every agent under `agents/<name>/` has exactly:

- `agent.py` — pure function `run(input) -> output`. No prompt text, no client-specific logic.
- `prompt.md` — instructions, versioned separately.
- `schema.py` — Pydantic input/output classes.

## Model pinning

`claude-sonnet-4-6` exactly. Pinned in `pyproject.toml` (`[tool.lis-core].default_model`) and `shared/llm.py`.

## Eval gate

`pytest evals/` runs the scoring agent against fixtures generated from `spec/prospects_seed.yaml` (Klas + Nejra + ICP.md anchors). Pass threshold: ≥85% within ±1 tier of `expected_tier`.
