from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from .evidence import Evidence


class IndustrialSignalType(str, Enum):
    CAPEX_ANNOUNCEMENT = "CAPEX_ANNOUNCEMENT"
    PLANT_EXPANSION = "PLANT_EXPANSION"
    GEOGRAPHIC_EXPANSION = "GEOGRAPHIC_EXPANSION"
    NEW_FACILITY_BUILDING_PERMIT = "NEW_FACILITY_BUILDING_PERMIT"

    HIRE_OPERATIONS_DIRECTOR = "HIRE_OPERATIONS_DIRECTOR"
    HIRE_PRODUCTION_MANAGER = "HIRE_PRODUCTION_MANAGER"
    HIRE_MANUFACTURING_ENGINEER = "HIRE_MANUFACTURING_ENGINEER"
    HIRE_AFTER_SALES_MANAGER = "HIRE_AFTER_SALES_MANAGER"
    HIRE_CNC_OPERATORS_BATCH = "HIRE_CNC_OPERATORS_BATCH"
    HIRE_CNC_ROLE = "HIRE_CNC_ROLE"

    OWNERSHIP_CHANGE = "OWNERSHIP_CHANGE"
    CEO_TRANSITION = "CEO_TRANSITION"
    OWNERSHIP_GROUP_INVESTMENT = "OWNERSHIP_GROUP_INVESTMENT"

    CERTIFICATION_CHANGE = "CERTIFICATION_CHANGE"
    MAJOR_CUSTOMER_WIN = "MAJOR_CUSTOMER_WIN"
    MAJOR_CUSTOMER_LOSS = "MAJOR_CUSTOMER_LOSS"
    SUPPLIER_SWITCH_SIGNAL = "SUPPLIER_SWITCH_SIGNAL"
    REGULATORY_PRESSURE = "REGULATORY_PRESSURE"

    QUARTERLY_REPORT_TRIGGER = "QUARTERLY_REPORT_TRIGGER"
    PUBLIC_PROCUREMENT_WIN = "PUBLIC_PROCUREMENT_WIN"
    FUNDING_GRANT = "FUNDING_GRANT"

    ACTIVE_SALES_DIALOGUE = "ACTIVE_SALES_DIALOGUE"


class SignalAtCapture(BaseModel):
    """Hand-curated signal from a sales person / spec YAML. No evidence required —
    treated as expert input. Promoted to a real Signal once an autonomous source
    confirms it.
    """

    type: IndustrialSignalType
    detail: str
    date: str | None = None
    source_hint: str | None = None


class Signal(BaseModel):
    """An observed signal with full evidence chain. Validator rejects if evidence is
    missing or fails the substring check.
    """

    signal_type: IndustrialSignalType
    points_awarded: int = Field(ge=0)
    evidence: list[Evidence] = Field(min_length=1)
    detected_at: datetime
