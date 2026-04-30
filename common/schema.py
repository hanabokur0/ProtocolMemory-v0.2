from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ConfigDict, field_validator


# ============================================================
# Enums
# ============================================================

class Domain(str, Enum):
    geopolitics = "geopolitics"
    operations = "operations"
    workflow = "workflow"
    cognitive = "cognitive"
    media = "media"
    finance = "finance"
    social = "social"
    custom = "custom"


class Source(str, Enum):
    Grok = "Grok"
    LPTM = "LPTM"
    PFI = "PFI"
    COCLI = "COCLI"
    ProtocolEngine = "ProtocolEngine"
    CoordinateDecompositionEngine = "CoordinateDecompositionEngine"
    HumanObserver = "HumanObserver"
    ExternalAPI = "ExternalAPI"
    Custom = "Custom"


class EngineName(str, Enum):
    LPTM = "LPTM"
    PFI = "PFI"
    COCLI = "COCLI"
    ProtocolEngine = "ProtocolEngine"
    CHD = "CHD"
    HumanReview = "HumanReview"
    CoordinateDecompositionEngine = "CoordinateDecompositionEngine"
    FeatureExtraction = "FeatureExtraction"
    # v0.2 additions
    DDA = "DDA"
    CAG = "CAG"
    MetaObserver = "MetaObserver"
    LCA = "LCA"


class InputType(str, Enum):
    field_voice_batch = "field_voice_batch"
    text = "text"
    event_log = "event_log"
    tabular = "tabular"
    metric_snapshot = "metric_snapshot"
    protocol_case = "protocol_case"
    mixed = "mixed"


class Priority(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class IndicatorKind(str, Enum):
    core = "core"
    proxy = "proxy"
    derived = "derived"


class ProtocolStatus(str, Enum):
    auto = "auto"
    human = "human"
    unknown = "unknown"


class NextAction(str, Enum):
    continue_ = "continue"
    route_to_protocol = "route_to_protocol"
    route_to_human = "route_to_human"
    open_chd_branch = "open_chd_branch"
    store_only = "store_only"
    halt = "halt"
    # v0.2 — DDA verdict mappings
    dda_execute = "dda_execute"            # full auto execution
    dda_pilot = "dda_pilot"                # limited execution + observation continues
    dda_hold = "dda_hold"                  # no execution, schedule re-evaluation
    dda_reframe = "dda_reframe"            # transform question, re-enter pipeline upstream
    dda_reject = "dda_reject"              # reject with reason + RII boundary update
    dda_observe_more = "dda_observe_more"  # request additional observation (return to sensors)


# ============================================================
# Small reusable models
# ============================================================

class NumericIndicator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float
    unit: str = "score"
    scale_min: float | None = None
    scale_max: float | None = None
    kind: IndicatorKind = IndicatorKind.proxy
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    method: str | None = None


class FieldVoice(BaseModel):
    """
    Semi-structured voice item from Grok / human / external collectors.
    """
    model_config = ConfigDict(extra="forbid")

    priority: Literal["low", "medium", "high"]
    content: str = Field(..., min_length=1)
    source_account: str | None = None
    location: str | None = None
    observed_at: datetime | None = None
    language: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("content")
    @classmethod
    def validate_content(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("content must not be empty")
        return v


class EventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str
    description: str
    event_time: datetime | None = None
    severity: Priority | None = None
    flags: list[str] = Field(default_factory=list)


class AttachmentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image", "file", "url", "json", "csv", "log"]
    uri: str
    label: str | None = None


class TabularRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["csv", "jsonl", "parquet", "xlsx"]
    uri: str | None = None
    row_count: int | None = Field(default=None, ge=0)


class TraceStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: str
    at: datetime
    action: str | None = None
    note: str | None = None


class EngineOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: EngineName
    states: dict[str, Any] = Field(default_factory=dict)
    indicators: dict[str, NumericIndicator] = Field(default_factory=dict)
    summary: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ProtocolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ProtocolStatus | None = None
    result: str | None = None
    description: str | None = None
    error_type: str | None = None


# ============================================================
# v0.2 — DDA / Sensor / LCA models
# ============================================================

class DDAVerdict(str, Enum):
    """Six-mode verdict from Deliberative Decision Architecture."""
    EXECUTE = "EXECUTE"
    PILOT = "PILOT"
    HOLD = "HOLD"
    REFRAME = "REFRAME"
    REJECT = "REJECT"
    OBSERVE_MORE = "OBSERVE_MORE"


class LPTMSensorOutput(BaseModel):
    """Structured output from LPTM phase transition sensor."""
    model_config = ConfigDict(extra="forbid")

    pst: float = Field(..., ge=0.0, le=1.0)
    delta_pst: float | None = None
    delta2_pst: float | None = None
    layer: str | None = None                             # L0 / L1 / L2 / L3
    transition_label: str | None = None                  # stable_or_noise / cob_oscillation / phase_rising / false_peak / breakout
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class CAGSensorOutput(BaseModel):
    """Structured output from CAG cognitive-action gap sensor."""
    model_config = ConfigDict(extra="forbid")

    cag_value: float                                     # cognition - action
    delta_cag: float | None = None
    delta2_cag: float | None = None
    phase_label: str | None = None                       # COG_LEAD / SYNC / ACTION_LEAD
    verdict: str | None = None                           # GO / WAIT / STOP
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class CHDSensorOutput(BaseModel):
    """Structured output from CHD-Protocol hazard detection."""
    model_config = ConfigDict(extra="forbid")

    chd_score: float | None = Field(default=None, ge=0.0, le=100.0)
    chd_s: float | None = None                           # Silence Risk
    chd_p: float | None = None                           # Pattern Drift
    chd_r: float | None = None                           # Resilience Resistance
    chd_f: float | None = None                           # Friction Failure
    chd_e: float | None = None                           # Emergent Hazard
    phase: str | None = None                             # Silent / Pattern Drift / Structural Break / Chain Collapse / Irreversible
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class MetaObserverOutput(BaseModel):
    """Structured output from Meta-Observer urban functional coordinate."""
    model_config = ConfigDict(extra="forbid")

    functional_plasticity: float | None = None
    temporal_dynamic: float | None = None
    governance_response_latency: float | None = None
    functional_region: str | None = None                 # Adaptive Friction / Elastic Flow / Structural Stress / Critical
    city_id: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class SensorInputs(BaseModel):
    """Aggregated sensor outputs fed into DDA for verdict computation."""
    model_config = ConfigDict(extra="forbid")

    lptm: LPTMSensorOutput | None = None
    cag: CAGSensorOutput | None = None
    chd: CHDSensorOutput | None = None
    meta_observer: MetaObserverOutput | None = None


class DDAAssessment(BaseModel):
    """LoPAS indicator stack + sensor inputs for DDA evaluation."""
    model_config = ConfigDict(extra="forbid")

    # Mandatory DDA indicators (0-100)
    doq: float = Field(..., ge=0.0, le=100.0)
    cci: float = Field(..., ge=0.0, le=100.0)
    bcdi: float = Field(..., ge=0.0, le=100.0)
    rii: float = Field(..., ge=0.0, le=100.0)
    hri: float = Field(..., ge=0.0, le=100.0)
    cocli: float = Field(..., ge=0.0, le=100.0)

    # Recommended (optional)
    rdi: float | None = Field(default=None, ge=0.0, le=100.0)
    trs: float | None = Field(default=None, ge=0.0, le=100.0)
    sci: float | None = Field(default=None, ge=0.0, le=100.0)
    cdi: float | None = Field(default=None, ge=0.0, le=100.0)

    # Sensor inputs (v0.2)
    sensor_inputs: SensorInputs | None = None


class DDADecision(BaseModel):
    """DDA verdict output."""
    model_config = ConfigDict(extra="forbid")

    verdict: DDAVerdict
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str | None = None


class DDADecisionCard(BaseModel):
    """Complete DDA Decision Card (input + output + learning hooks)."""
    model_config = ConfigDict(extra="forbid")

    issue_id: str
    issue_title: str | None = None
    current_question: str | None = None

    assessment: DDAAssessment
    decision: DDADecision

    reject_conditions: list[str] = Field(default_factory=list)
    withholding_conditions: list[str] = Field(default_factory=list)
    revisit_triggers: list[str] = Field(default_factory=list)

    learning: dict[str, Any] = Field(default_factory=dict)
    # expected keys: boundary_update_required (bool), rii_update_pending (bool), notes (str)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "0.3"


class LCAValidationResult(BaseModel):
    """LCA structural validation output."""
    model_config = ConfigDict(extra="forbid")

    learning_validated: bool
    structural_change_detected: dict[str, Any] = Field(default_factory=dict)
    # expected keys: cci_increased, trs_changed, bcdi_increased, rigidity_reduced, phase_shift
    validation_confidence: float = Field(..., ge=0.0, le=1.0)
    route: str | None = None    # "reclassify" / "retain" / "reject"


class LCPOutput(BaseModel):
    """LCP learning classification output."""
    model_config = ConfigDict(extra="forbid")

    learning_valid: bool
    learning_labels: list[str] = Field(default_factory=list)
    # labels: KNOWLEDGE_LEARNING / PROCEDURAL_LEARNING / TRANSFER_LEARNING / CONDITIONAL_LEARNING / LAW_LEARNING / UNKNOWN
    confidence: float = Field(..., ge=0.0, le=1.0)
    preserve_unknown: bool = False
    candidate_protocol_update: dict[str, Any] = Field(default_factory=dict)


class PatternEvent(BaseModel):
    """Minimal decision fingerprint extracted from DDADecisionCard for ProtocolMemory."""
    model_config = ConfigDict(extra="forbid")

    ts: datetime
    verdict: str
    chd_phase: str | None = None
    lptm_transition: str | None = None
    cag_phase: str | None = None
    doq: float
    cocli: float
    rii: float

class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_type: InputType
    raw_text: str | None = None
    summary: str | None = None
    field_voices: list[FieldVoice] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    attachments: list[AttachmentRef] = Field(default_factory=list)
    tabular_ref: TabularRef | None = None


class Meta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confidence: float = Field(..., ge=0.0, le=1.0)
    human_review_required: bool
    review_reason: str | None = None
    flags: list[str] = Field(default_factory=list)
    priority: Priority | None = None
    trace: list[TraceStep] = Field(default_factory=list)
    provenance: dict[str, str] | None = None


class Routing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_engines: list[EngineName] = Field(default_factory=list)
    fallback_engine: EngineName | None = None
    next_action: NextAction | None = None


class Results(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine_outputs: list[EngineOutput] = Field(default_factory=list)
    protocol_result: ProtocolResult | None = None


# ============================================================
# Top-level observation
# ============================================================

class LoPASObservation(BaseModel):
    """
    Common Schema v0.2 top-level model.
    """
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1", "0.2"] = "0.2"
    observation_id: str = Field(default_factory=lambda: f"obs-{uuid4()}")
    timestamp: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    domain: Domain
    source: Source
    engine_route: list[EngineName] = Field(default_factory=list)

    payload: Payload
    indicators: dict[str, NumericIndicator] = Field(default_factory=dict)
    states: dict[str, Any] = Field(default_factory=dict)
    meta: Meta

    routing: Routing | None = None
    results: Results | None = None

    # v0.2 — DDA / Sensor / LCA
    sensor_inputs: SensorInputs | None = None
    dda_decision_card: DDADecisionCard | None = None
    lcp_output: LCPOutput | None = None
    lca_validation: LCAValidationResult | None = None
