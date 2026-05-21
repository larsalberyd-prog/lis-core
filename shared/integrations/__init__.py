"""External data-source integrations.

Each module exposes a `fetch_*` function that returns raw event dicts —
no classification, no scoring. The signals agent picks the events up and
maps them to `IndustrialSignalType` with evidence.
"""
