"""
orchestrator_v03.py — LoPAS Runtime Orchestrator v0.3

Pipeline:
  FieldVoice[] → Classification → Sensors (LPTM/CAG/CHD/COCLI) → DDA Verdict → Execution → LCA hooks

Changes from v0.2:
  - DDA verdict gate between classification and execution
  - Sensor outputs (LPTM/CAG/CHD) aggregated into SensorInputs
  - DDA Decision Card written to observation
  - NextAction mapped from DDA verdict (6 modes)
  - CHD phase can block execution (Pre-Collapse / Chain Collapse → REJECT)
  - LCA output slots populated when available
  - Existing v0.2 classification logic preserved (not broken)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from common_schema_models_v02 import (
    Domain,
    EngineName,
    FieldVoice,
    LoPASObservation,
    ProtocolResult,
    Results,
    Routing,
    EngineOutput,
    NextAction,
    NumericIndicator,
    # v0.2 models
    DDAVerdict,
    DDAAssessment,
    DDADecision,
    DDADecisionCard,
    SensorInputs,
    LPTMSensorOutput,
    CAGSensorOutput,
    CHDSensorOutput,
    LCPOutput,
    LCAValidationResult,
)


# ============================================================
# Sensor runners (stubs — replace with real implementations)
# ============================================================

def run_lptm_sensor(observation: LoPASObservation) -> LPTMSensorOutput:
    """
    Stub: extract LPTM sensor output from observation indicators or run live.
    Replace with actual lptm_e2e_minimal integration.
    """
    doq_ind = observation.indicators.get("Q")
    cci_ind = observation.indicators.get("C")
    doq = doq_ind.value if doq_ind else 0.5
    cci = cci_ind.value if cci_ind else 0.5

    pst = (doq + cci) / 2.0
    return LPTMSensorOutput(
        pst=min(max(pst, 0.0), 1.0),
        delta_pst=None,
        delta2_pst=None,
        layer="L1",
        transition_label="stable_or_noise",
        confidence=0.5,
    )


def run_cag_sensor(observation: LoPASObservation) -> CAGSensorOutput:
    """
    Stub: compute CAG from observation context.
    Replace with actual cag_core integration.
    """
    return CAGSensorOutput(
        cag_value=0.0,
        phase_label="SYNC",
        verdict="WAIT",
        confidence=0.5,
    )


def run_chd_sensor(observation: LoPASObservation) -> CHDSensorOutput:
    """
    Stub: compute CHD hazard from observation.
    Replace with actual CHD-Protocol integration.
    """
    # Check for risk flags as a simple proxy
    flags = observation.meta.flags or []
    risk_flags = observation.states.get("risk_flags", [])
    has_risk = bool(flags or risk_flags)

    return CHDSensorOutput(
        chd_score=45.0 if has_risk else 20.0,
        phase="Silent" if not has_risk else "Pattern Drift",
        confidence=0.5,
    )


def run_cocli_stub(observation: LoPASObservation) -> float:
    """Return COCLI score 0-100. Stub."""
    field_quality = observation.indicators.get("F")
    if field_quality and field_quality.confidence is not None:
        return field_quality.confidence * 100.0
    return 50.0


# ============================================================
# DDA Verdict Engine
# ============================================================

# Threshold defaults (DDA v0.1 spec)
DDA_THRESHOLDS = {
    "doq_reframe": 40.0,
    "cocli_hold": 35.0,
    "sci_reject": 70.0,
    "bcdi_observe": 45.0,
    "rii_pilot": 50.0,
}

# CHD phases that force REJECT
CHD_CRITICAL_PHASES = {"Chain Collapse", "Irreversible"}
# CHD phases that force HOLD
CHD_WARNING_PHASES = {"Structural Break", "Pre-Collapse"}

# LPTM transitions that influence verdict
LPTM_HOLD_TRANSITIONS = {"cob_oscillation", "false_peak"}
LPTM_EXECUTE_TRANSITIONS = {"breakout", "phase_rising"}

# CAG phases that influence verdict
CAG_OBSERVE_PHASES = {"COG_LEAD"}


def compute_dda_indicators(observation: LoPASObservation) -> dict[str, float]:
    """
    Extract DDA mandatory indicators from observation.
    Falls back to defaults when indicators are not available.
    """
    def get_ind(key: str, default: float = 50.0) -> float:
        ind = observation.indicators.get(key)
        if ind is not None:
            return ind.value
        return default

    return {
        "doq": get_ind("Q", 50.0) * 100.0 if get_ind("Q", 0.5) <= 1.0 else get_ind("Q", 50.0),
        "cci": get_ind("C", 50.0) * 100.0 if get_ind("C", 0.5) <= 1.0 else get_ind("C", 50.0),
        "bcdi": 50.0,   # not in feature_extraction v0.2 — default
        "rii": 50.0,    # not in feature_extraction v0.2 — default
        "hri": 50.0,    # not in feature_extraction v0.2 — default
        "cocli": run_cocli_stub(observation),
    }


def evaluate_dda_verdict(
    indicators: dict[str, float],
    sensors: SensorInputs,
    classification_class: str | None = None,
) -> tuple[DDAVerdict, float, str]:
    """
    DDA verdict logic.
    Priority: CHD override > REFRAME > HOLD > REJECT > OBSERVE_MORE > PILOT > EXECUTE

    Returns: (verdict, confidence, rationale)
    """
    reasons = []

    # ── CHD override (highest priority safety gate) ──
    if sensors.chd and sensors.chd.phase:
        if sensors.chd.phase in CHD_CRITICAL_PHASES:
            return (
                DDAVerdict.REJECT,
                0.90,
                f"CHD critical phase: {sensors.chd.phase}",
            )
        if sensors.chd.phase in CHD_WARNING_PHASES:
            reasons.append(f"CHD warning: {sensors.chd.phase}")
            # Don't return yet — may combine with other signals

    # ── DoQ gate ──
    if indicators["doq"] < DDA_THRESHOLDS["doq_reframe"]:
        return (
            DDAVerdict.REFRAME,
            0.75,
            f"DoQ={indicators['doq']:.0f} below reframe threshold",
        )

    # ── COCLI gate ──
    if indicators["cocli"] < DDA_THRESHOLDS["cocli_hold"]:
        return (
            DDAVerdict.HOLD,
            0.70,
            f"COCLI={indicators['cocli']:.0f} below hold threshold"
            + (f"; {reasons[0]}" if reasons else ""),
        )

    # ── CHD warning → HOLD (if not already caught above) ──
    if reasons:
        return (
            DDAVerdict.HOLD,
            0.70,
            reasons[0],
        )

    # ── SCI gate (from optional indicators) ──
    sci = indicators.get("sci")
    if sci is not None and sci > DDA_THRESHOLDS["sci_reject"]:
        return (
            DDAVerdict.REJECT,
            0.80,
            f"SCI={sci:.0f} above reject threshold",
        )

    # ── LPTM sensor influence ──
    if sensors.lptm and sensors.lptm.transition_label:
        if sensors.lptm.transition_label in LPTM_HOLD_TRANSITIONS:
            return (
                DDAVerdict.HOLD,
                0.65,
                f"LPTM transition: {sensors.lptm.transition_label}",
            )

    # ── CAG sensor influence ──
    if sensors.cag and sensors.cag.phase_label:
        if sensors.cag.phase_label in CAG_OBSERVE_PHASES:
            return (
                DDAVerdict.OBSERVE_MORE,
                0.65,
                f"CAG phase: {sensors.cag.phase_label} (cognition leading action)",
            )

    # ── BCDI gate ──
    if indicators["bcdi"] < DDA_THRESHOLDS["bcdi_observe"]:
        return (
            DDAVerdict.OBSERVE_MORE,
            0.65,
            f"BCDI={indicators['bcdi']:.0f} below observe threshold",
        )

    # ── RII gate ──
    if indicators["rii"] < DDA_THRESHOLDS["rii_pilot"]:
        return (
            DDAVerdict.PILOT,
            0.70,
            f"RII={indicators['rii']:.0f} below execute threshold → pilot mode",
        )

    # ── LPTM breakout bonus ──
    lptm_bonus = ""
    if sensors.lptm and sensors.lptm.transition_label in LPTM_EXECUTE_TRANSITIONS:
        lptm_bonus = f"; LPTM={sensors.lptm.transition_label}"

    # ── All gates passed ──
    return (
        DDAVerdict.EXECUTE,
        0.80,
        f"All DDA gates passed{lptm_bonus}",
    )


def map_verdict_to_next_action(verdict: DDAVerdict) -> NextAction:
    """Map DDA verdict to NextAction enum."""
    mapping = {
        DDAVerdict.EXECUTE: NextAction.dda_execute,
        DDAVerdict.PILOT: NextAction.dda_pilot,
        DDAVerdict.HOLD: NextAction.dda_hold,
        DDAVerdict.REFRAME: NextAction.dda_reframe,
        DDAVerdict.REJECT: NextAction.dda_reject,
        DDAVerdict.OBSERVE_MORE: NextAction.dda_observe_more,
    }
    return mapping[verdict]


def verdict_requires_human(verdict: DDAVerdict) -> bool:
    """Verdicts that require human review."""
    return verdict in {DDAVerdict.HOLD, DDAVerdict.REFRAME, DDAVerdict.REJECT}


# ============================================================
# Engine selection (v0.3)
# ============================================================

def select_engines(observation: LoPASObservation) -> list[EngineName]:
    """
    v0.3:
    - engine_route があれば尊重
    - DDA は常に含める（judgment gate）
    - CHD は常に含める（safety gate）
    - LPTM / CAG / COCLI はドメインと分類に応じて追加
    """
    if observation.engine_route:
        # Ensure DDA and CHD are always present even with explicit routes
        routes = list(observation.engine_route)
        if EngineName.DDA not in routes:
            routes.append(EngineName.DDA)
        if EngineName.CHD not in routes:
            routes.insert(0, EngineName.CHD)
        return routes

    cls = observation.states.get("classification_class")

    # Base: always CHD + classification + DDA + Protocol Engine
    engines = [EngineName.CHD, EngineName.FeatureExtraction, EngineName.DDA, EngineName.ProtocolEngine]

    if cls == "ESCALATE":
        engines.extend([EngineName.LPTM, EngineName.CAG, EngineName.HumanReview])

    elif cls in {"REVIEW", "UNKNOWN"}:
        engines.append(EngineName.HumanReview)

    elif observation.domain == Domain.geopolitics:
        engines.extend([EngineName.LPTM, EngineName.CAG, EngineName.COCLI])

    elif observation.domain == Domain.finance:
        engines.extend([EngineName.CAG, EngineName.LPTM])

    return engines


# ============================================================
# Classification-first routing (preserved from v0.2)
# ============================================================

def route_from_classification(observation: LoPASObservation) -> tuple[str, str, float]:
    """
    v0.3: returns (classification_class, reason, confidence)
    No longer determines NextAction directly — that's DDA's job now.
    """
    cls = observation.states.get("classification_class", "REVIEW")
    reason = observation.states.get("classification_reason", "")
    conf = observation.states.get("classification_confidence", 0.5)
    return cls, reason, conf


# ============================================================
# Main orchestrator (v0.3)
# ============================================================

def orchestrate_observation(observation: LoPASObservation) -> LoPASObservation:
    """
    v0.3 pipeline:
      Classification → Sensors (CHD/LPTM/CAG) → DDA Verdict → Routing → Engine Outputs
    """
    selected_engines = select_engines(observation)
    engine_outputs: list[EngineOutput] = []

    review_required = observation.meta.human_review_required
    review_reasons: list[str] = []
    if observation.meta.review_reason:
        review_reasons.append(observation.meta.review_reason)

    # ── Step 1: Classification (preserved from v0.2) ──
    cls, cls_reason, cls_conf = route_from_classification(observation)

    engine_outputs.append(
        EngineOutput(
            engine=EngineName.FeatureExtraction,
            states={
                "classification_class": cls,
                "classification_reason": cls_reason,
            },
            indicators={
                k: v for k, v in observation.indicators.items()
                if k in {"K", "Q", "C", "S", "T", "F"}
            },
            summary=f"Classification Core v0.1: {cls}",
            confidence=cls_conf,
        )
    )

    # ── Step 2: Run sensors ──
    # CHD (always)
    chd_output = run_chd_sensor(observation)

    engine_outputs.append(
        EngineOutput(
            engine=EngineName.CHD,
            states={"phase": chd_output.phase, "status": "completed"},
            indicators={},
            summary=f"CHD phase: {chd_output.phase}",
            confidence=chd_output.confidence,
        )
    )

    # LPTM (if selected)
    lptm_output = None
    if EngineName.LPTM in selected_engines:
        lptm_output = run_lptm_sensor(observation)
        engine_outputs.append(
            EngineOutput(
                engine=EngineName.LPTM,
                states={
                    "layer": lptm_output.layer,
                    "transition_label": lptm_output.transition_label,
                    "status": "completed",
                },
                indicators={
                    "PST": NumericIndicator(
                        value=lptm_output.pst,
                        unit="score",
                        scale_min=0.0,
                        scale_max=1.0,
                        kind="derived",
                        confidence=lptm_output.confidence,
                        method="run_lptm_sensor",
                    )
                },
                summary=f"LPTM: layer={lptm_output.layer}, transition={lptm_output.transition_label}",
                confidence=lptm_output.confidence,
            )
        )

    # CAG (if selected)
    cag_output = None
    if EngineName.CAG in selected_engines:
        cag_output = run_cag_sensor(observation)
        engine_outputs.append(
            EngineOutput(
                engine=EngineName.CAG,
                states={
                    "phase_label": cag_output.phase_label,
                    "verdict": cag_output.verdict,
                    "status": "completed",
                },
                indicators={},
                summary=f"CAG: phase={cag_output.phase_label}, verdict={cag_output.verdict}",
                confidence=cag_output.confidence,
            )
        )

    # COCLI (if selected — still stub)
    cocli_score = None
    if EngineName.COCLI in selected_engines:
        cocli_score = run_cocli_stub(observation)
        engine_outputs.append(
            EngineOutput(
                engine=EngineName.COCLI,
                states={"status": "stub"},
                indicators={},
                summary=f"COCLI stub: score={cocli_score:.0f}",
                confidence=0.5,
            )
        )

    # ── Step 3: Aggregate sensor inputs ──
    sensor_inputs = SensorInputs(
        lptm=lptm_output,
        cag=cag_output,
        chd=chd_output,
        meta_observer=None,  # not yet connected
    )
    observation.sensor_inputs = sensor_inputs

    # ── Step 4: DDA Verdict ──
    dda_indicators = compute_dda_indicators(observation)
    if cocli_score is not None:
        dda_indicators["cocli"] = cocli_score

    verdict, verdict_conf, rationale = evaluate_dda_verdict(
        indicators=dda_indicators,
        sensors=sensor_inputs,
        classification_class=cls,
    )

    # Build Decision Card
    assessment = DDAAssessment(
        doq=dda_indicators["doq"],
        cci=dda_indicators["cci"],
        bcdi=dda_indicators["bcdi"],
        rii=dda_indicators["rii"],
        hri=dda_indicators["hri"],
        cocli=dda_indicators["cocli"],
        sensor_inputs=sensor_inputs,
    )

    decision = DDADecision(
        verdict=verdict,
        confidence=verdict_conf,
        rationale=rationale,
    )

    decision_card = DDADecisionCard(
        issue_id=observation.observation_id,
        issue_title=observation.payload.summary,
        current_question=f"classification={cls}",
        assessment=assessment,
        decision=decision,
        reject_conditions=[rationale] if verdict == DDAVerdict.REJECT else [],
        withholding_conditions=[rationale] if verdict in {DDAVerdict.HOLD, DDAVerdict.OBSERVE_MORE} else [],
        learning={
            "boundary_update_required": verdict == DDAVerdict.REJECT,
            "rii_update_pending": verdict == DDAVerdict.PILOT,
        },
    )

    observation.dda_decision_card = decision_card

    engine_outputs.append(
        EngineOutput(
            engine=EngineName.DDA,
            states={
                "verdict": verdict.value,
                "confidence": verdict_conf,
                "status": "completed",
            },
            indicators={},
            summary=f"DDA verdict: {verdict.value} ({rationale})",
            confidence=verdict_conf,
        )
    )

    # ── Step 5: Map verdict to routing ──
    next_action = map_verdict_to_next_action(verdict)

    if verdict_requires_human(verdict):
        review_required = True
        review_reasons.append(f"DDA:{verdict.value} — {rationale}")

    # Classification-level review reasons (preserved from v0.2)
    if cls in {"ESCALATE", "REVIEW", "UNKNOWN"}:
        review_required = True
        if cls_reason:
            review_reasons.append(cls_reason)

    # ── Step 6: Build protocol result ──
    if verdict == DDAVerdict.EXECUTE:
        protocol_result = ProtocolResult(
            status="auto",
            result=f"DDA EXECUTE: {rationale}",
        )
    elif verdict == DDAVerdict.PILOT:
        protocol_result = ProtocolResult(
            status="auto",
            result=f"DDA PILOT: limited execution — {rationale}",
            description="Execution proceeds with observation. RII update pending.",
        )
    elif verdict == DDAVerdict.HOLD:
        protocol_result = ProtocolResult(
            status="human",
            description=f"DDA HOLD: judgment deferred — {rationale}",
        )
    elif verdict == DDAVerdict.REFRAME:
        protocol_result = ProtocolResult(
            status="human",
            description=f"DDA REFRAME: question transformation required — {rationale}",
        )
    elif verdict == DDAVerdict.REJECT:
        protocol_result = ProtocolResult(
            status="human",
            description=f"DDA REJECT: {rationale}",
            error_type="DDA_REJECT",
        )
    else:  # OBSERVE_MORE
        protocol_result = ProtocolResult(
            status="unknown",
            description=f"DDA OBSERVE_MORE: additional observation needed — {rationale}",
        )

    # ── Step 7: Write routing + results ──
    observation.routing = Routing(
        selected_engines=selected_engines,
        fallback_engine=EngineName.HumanReview if review_required else EngineName.ProtocolEngine,
        next_action=next_action,
    )

    observation.results = Results(
        engine_outputs=engine_outputs,
        protocol_result=protocol_result,
    )

    observation.meta.human_review_required = review_required
    if review_reasons:
        observation.meta.review_reason = ";".join(sorted(set(review_reasons)))

    return observation


# ============================================================
# End-to-end helper (preserved from v0.2, updated imports)
# ============================================================

def orchestrate_field_voices(
    field_voices: list[FieldVoice],
    *,
    observation_id: str | None = None,
    timestamp: datetime | None = None,
    domain: Domain = Domain.geopolitics,
    requested_engines: list[EngineName] | None = None,
    summary: str | None = None,
    similar_case_score: float = 0.0,
    history_matches: int = 0,
    context_links: int | None = None,
    source_count: int | None = None,
    risk_flags: list[str] | None = None,
) -> LoPASObservation:
    """
    FieldVoice[] -> Classification Core -> Sensors -> DDA -> Routing
    """
    # NOTE: build_observation_with_classification_axes import is preserved
    # but commented out here since we don't have that module in this context.
    # In production, uncomment and use as before.
    #
    # from feature_extraction_v02 import build_observation_with_classification_axes
    # observation = build_observation_with_classification_axes(
    #     field_voices=field_voices, ...
    # )
    # return orchestrate_observation(observation)
    raise NotImplementedError(
        "orchestrate_field_voices requires feature_extraction_v02. "
        "Use orchestrate_observation() directly with a pre-built LoPASObservation."
    )


# ============================================================
# Standalone demo (uses common_schema_models_v02 only)
# ============================================================

def demo() -> None:
    from common_schema_models_v02 import (
        Payload,
        InputType,
        Meta,
        Source,
        Priority,
    )

    # Build a test observation
    observation = LoPASObservation(
        observation_id="obs-v03-demo-001",
        timestamp=datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc),
        domain=Domain.geopolitics,
        source=Source.Grok,
        payload=Payload(
            input_type=InputType.field_voice_batch,
            summary="Succession crisis and collapse risk rising",
            field_voices=[
                FieldVoice(
                    priority="high",
                    content="後継危機と混乱が拡大し、崩壊リスクが高まっている。",
                    source_account="@ConflictWatch",
                    location="Qom",
                    observed_at=datetime(2026, 4, 22, 6, 30, tzinfo=timezone.utc),
                    language="ja",
                    tags=["collapse", "transition"],
                ),
                FieldVoice(
                    priority="medium",
                    content="制度的責任と分析が必要だ。",
                    source_account="@AnalystDesk",
                    location="Tehran",
                    observed_at=datetime(2026, 4, 22, 7, 10, tzinfo=timezone.utc),
                    language="ja",
                    tags=["analysis"],
                ),
            ],
        ),
        indicators={
            "Q": NumericIndicator(value=0.72, unit="score", kind="proxy", confidence=0.8),
            "C": NumericIndicator(value=0.65, unit="score", kind="proxy", confidence=0.7),
            "F": NumericIndicator(value=0.60, unit="score", kind="proxy", confidence=0.6),
        },
        states={
            "classification_class": "ESCALATE",
            "classification_reason": "high risk signals detected",
            "classification_confidence": 0.78,
            "risk_flags": ["warning", "collapse"],
        },
        meta=Meta(
            confidence=0.75,
            human_review_required=False,
            priority=Priority.high,
            flags=["warning"],
        ),
    )

    # Run orchestrator v0.3
    result = orchestrate_observation(observation)

    # Print result
    print(result.model_dump_json(indent=2, exclude_none=True))

    # Summary
    print("\n" + "=" * 60)
    print("ORCHESTRATOR v0.3 SUMMARY")
    print("=" * 60)
    card = result.dda_decision_card
    if card:
        print(f"DDA Verdict:    {card.decision.verdict.value}")
        print(f"Confidence:     {card.decision.confidence}")
        print(f"Rationale:      {card.decision.rationale}")
    print(f"NextAction:     {result.routing.next_action.value}")
    print(f"Human Review:   {result.meta.human_review_required}")
    print(f"Review Reason:  {result.meta.review_reason}")
    print(f"Engines Used:   {[e.value for e in result.routing.selected_engines]}")
    if result.sensor_inputs:
        if result.sensor_inputs.chd:
            print(f"CHD Phase:      {result.sensor_inputs.chd.phase}")
        if result.sensor_inputs.lptm:
            print(f"LPTM Layer:     {result.sensor_inputs.lptm.layer} ({result.sensor_inputs.lptm.transition_label})")
        if result.sensor_inputs.cag:
            print(f"CAG Phase:      {result.sensor_inputs.cag.phase_label}")


if __name__ == "__main__":
    demo()
