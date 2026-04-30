"""
protocol_memory.py — ProtocolMemory v0.2

v0.1 → v0.2 changes:
  - Distortion detection: baseline vs conditional distribution (L1 divergence)
  - Bucket-based adjustment routing: DoQ/COCLI/RII low/mid/high determines which knob to turn
  - Cooldown: same threshold key cannot be touched again within N events
  - Clipping: all thresholds clamped to safe ranges
  - suggest_one_adjustment: picks the single highest-distortion pattern
  - distortion_report with full analysis

Design rules (unchanged):
  1. Delayed learning — no adjustments until 20+ events per pattern
  2. Single change — one threshold adjustment at a time
  3. Reversible — all adjustments logged, can be rolled back
  4. Minimal — stores only what influenced the verdict

Safety additions (v0.2):
  5. Cooldown — same key cannot be re-adjusted within COOLDOWN_EVENTS
  6. Clipping — thresholds clamped to THRESHOLD_LIMITS
  7. sci_reject is never auto-adjusted (safety valve)
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common_schema_models_v02 import (
    DDADecisionCard,
    PatternEvent,
)


# ============================================================
# Constants
# ============================================================

MIN_EVENTS_FOR_FEEDBACK = 20
COOLDOWN_EVENTS = 50
DISTORTION_THRESHOLD = 0.5
SHIFT_THRESHOLD = 0.4

ALL_VERDICTS = ["EXECUTE", "PILOT", "HOLD", "REFRAME", "REJECT", "OBSERVE_MORE"]

THRESHOLD_LIMITS = {
    "doq_reframe": (20.0, 70.0),
    "cocli_hold":  (10.0, 60.0),
    "rii_pilot":   (20.0, 80.0),
    "bcdi_observe": (20.0, 70.0),
}


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ============================================================
# Pattern key
# ============================================================

def pattern_key(e: PatternEvent) -> str:
    return f"{e.chd_phase or 'none'}|{e.lptm_transition or 'none'}|{e.cag_phase or 'none'}"


# ============================================================
# Bucket classification
# ============================================================

def bucket(value: float, low: float = 40.0, high: float = 65.0) -> str:
    if value < low:
        return "low"
    if value < high:
        return "mid"
    return "high"


# ============================================================
# PatternStats
# ============================================================

class PatternStats:

    def __init__(self):
        self.count: int = 0
        self.verdict_counts: dict[str, int] = defaultdict(int)
        self.avg_doq: float = 0.0
        self.avg_cocli: float = 0.0
        self.avg_rii: float = 0.0
        self.first_seen: datetime | None = None
        self.last_seen: datetime | None = None

    def update(self, e: PatternEvent) -> None:
        self.count += 1
        self.verdict_counts[e.verdict] += 1
        self.avg_doq += (e.doq - self.avg_doq) / self.count
        self.avg_cocli += (e.cocli - self.avg_cocli) / self.count
        self.avg_rii += (e.rii - self.avg_rii) / self.count
        if self.first_seen is None:
            self.first_seen = e.ts
        self.last_seen = e.ts

    def verdict_ratio(self, verdict: str) -> float:
        if self.count == 0:
            return 0.0
        return self.verdict_counts.get(verdict, 0) / self.count

    def verdict_distribution(self) -> dict[str, float]:
        return {v: self.verdict_ratio(v) for v in ALL_VERDICTS}

    def dominant_verdict(self) -> str | None:
        if not self.verdict_counts:
            return None
        return max(self.verdict_counts, key=self.verdict_counts.get)

    def bucket_info(self) -> dict[str, str]:
        return {
            "doq": bucket(self.avg_doq),
            "cocli": bucket(self.avg_cocli),
            "rii": bucket(self.avg_rii),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "verdict_counts": dict(self.verdict_counts),
            "avg_doq": round(self.avg_doq, 2),
            "avg_cocli": round(self.avg_cocli, 2),
            "avg_rii": round(self.avg_rii, 2),
            "buckets": self.bucket_info(),
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }


# ============================================================
# ThresholdAdjustment
# ============================================================

class ThresholdAdjustment:

    def __init__(self, type_: str, key: str, delta: float, reason: str,
                 distortion_score: float = 0.0, pattern_key: str = ""):
        self.type = type_
        self.key = key
        self.delta = delta
        self.reason = reason
        self.distortion_score = distortion_score
        self.pattern_key = pattern_key

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type, "key": self.key, "delta": self.delta,
            "reason": self.reason, "distortion_score": round(self.distortion_score, 3),
            "pattern_key": self.pattern_key,
        }

    def __repr__(self) -> str:
        d = "+" if self.delta > 0 else ""
        return f"Adjust({self.key} {d}{self.delta}, distortion={self.distortion_score:.2f}: {self.reason})"


# ============================================================
# Adjustment proposal logic (bucket-aware)
# ============================================================

def propose_adjustment(top_shift: str, shift_magnitude: float,
                       stats: PatternStats, pattern: str) -> ThresholdAdjustment | None:
    b = stats.bucket_info()

    if top_shift == "REJECT" and shift_magnitude > SHIFT_THRESHOLD:
        if b["doq"] == "low":
            return ThresholdAdjustment(
                type_="doq_reframe_up", key="doq_reframe", delta=+5.0,
                reason=f"[{pattern}] REJECT excess + low DoQ → tighten REFRAME gate",
            )
        else:
            return ThresholdAdjustment(
                type_="cocli_hold_down", key="cocli_hold", delta=-3.0,
                reason=f"[{pattern}] REJECT excess, DoQ ok → loosen HOLD for re-observation",
            )

    if top_shift == "HOLD" and shift_magnitude > SHIFT_THRESHOLD:
        return ThresholdAdjustment(
            type_="cocli_hold_down", key="cocli_hold", delta=-3.0,
            reason=f"[{pattern}] HOLD excess → loosen climate gate",
        )

    if top_shift == "PILOT" and shift_magnitude > 0.3:
        return ThresholdAdjustment(
            type_="rii_pilot_down", key="rii_pilot", delta=-3.0,
            reason=f"[{pattern}] PILOT excess → lower RII threshold toward EXECUTE",
        )

    if top_shift == "EXECUTE" and shift_magnitude > 0.3 and stats.avg_doq < 45.0:
        return ThresholdAdjustment(
            type_="doq_reframe_down", key="doq_reframe", delta=-3.0,
            reason=f"[{pattern}] EXECUTE excess + low DoQ ({stats.avg_doq:.0f}) → tighten gate",
        )

    return None


# ============================================================
# ProtocolMemory v0.2
# ============================================================

class ProtocolMemory:

    def __init__(self):
        self.store: dict[str, PatternStats] = {}
        self.adjustment_log: list[dict[str, Any]] = []
        self._cooldown_counters: dict[str, int] = {}
        self._total_ingested: int = 0

    # ── Ingest ──

    def ingest(self, card: DDADecisionCard) -> PatternEvent:
        event = self._card_to_event(card)
        key = pattern_key(event)
        if key not in self.store:
            self.store[key] = PatternStats()
        self.store[key].update(event)
        self._total_ingested += 1
        for k in self._cooldown_counters:
            self._cooldown_counters[k] += 1
        return event

    def ingest_batch(self, cards: list[DDADecisionCard]) -> int:
        for card in cards:
            self.ingest(card)
        return len(cards)

    # ── Baseline & Distortion ──

    def compute_baseline(self) -> dict[str, float]:
        total = sum(s.count for s in self.store.values())
        if total == 0:
            return {v: 0.0 for v in ALL_VERDICTS}
        counts: dict[str, int] = defaultdict(int)
        for stats in self.store.values():
            for v, c in stats.verdict_counts.items():
                counts[v] += c
        return {v: counts.get(v, 0) / total for v in ALL_VERDICTS}

    def distortion(self, stats: PatternStats, baseline: dict[str, float]
                   ) -> tuple[float, dict[str, float], str]:
        cond = stats.verdict_distribution()
        delta = {v: cond.get(v, 0.0) - baseline.get(v, 0.0) for v in ALL_VERDICTS}
        score = sum(abs(d) for d in delta.values())
        top = max(delta, key=lambda v: abs(delta[v]))
        return score, delta, top

    # ── Feedback ──

    def suggest_one_adjustment(self) -> ThresholdAdjustment | None:
        baseline = self.compute_baseline()
        best: tuple[float, ThresholdAdjustment] | None = None

        for key, stats in self.store.items():
            if stats.count < MIN_EVENTS_FOR_FEEDBACK:
                continue
            score, delta, top = self.distortion(stats, baseline)
            if score < DISTORTION_THRESHOLD:
                continue
            proposal = propose_adjustment(top, abs(delta[top]), stats, key)
            if proposal is None:
                continue
            if self._is_on_cooldown(proposal.key):
                continue
            proposal.distortion_score = score
            proposal.pattern_key = key
            if best is None or score > best[0]:
                best = (score, proposal)

        return best[1] if best else None

    def suggest_adjustments(self) -> list[ThresholdAdjustment]:
        adj = self.suggest_one_adjustment()
        return [adj] if adj else []

    def apply_adjustments(self, thresholds: dict[str, float],
                          adjustments: list[ThresholdAdjustment] | None = None,
                          max_adjustments: int = 1) -> list[ThresholdAdjustment]:
        if adjustments is None:
            adjustments = self.suggest_adjustments()
        applied: list[ThresholdAdjustment] = []
        for adj in adjustments[:max_adjustments]:
            if adj.key not in thresholds or adj.key not in THRESHOLD_LIMITS:
                continue
            old_val = thresholds[adj.key]
            lo, hi = THRESHOLD_LIMITS[adj.key]
            new_val = clamp(old_val + adj.delta, lo, hi)
            if new_val == old_val:
                continue
            thresholds[adj.key] = new_val
            applied.append(adj)
            self._cooldown_counters[adj.key] = 0
            self.adjustment_log.append({
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "key": adj.key, "old": old_val, "new": new_val,
                "delta": adj.delta, "effective_delta": new_val - old_val,
                "reason": adj.reason, "distortion_score": round(adj.distortion_score, 3),
                "pattern_key": adj.pattern_key,
            })
        return applied

    def _is_on_cooldown(self, key: str) -> bool:
        if key not in self._cooldown_counters:
            return False
        return self._cooldown_counters[key] < COOLDOWN_EVENTS

    # ── Reports ──

    def distortion_report(self) -> str:
        baseline = self.compute_baseline()
        lines = [
            "=== ProtocolMemory Distortion Report (v0.2) ===", "",
            f"Total events: {self._total_ingested}  |  Patterns: {len(self.store)}",
            f"Baseline: {', '.join(f'{v}={r:.0%}' for v, r in baseline.items() if r > 0)}", "",
        ]
        for key, stats in sorted(self.store.items(), key=lambda x: -x[1].count):
            score, delta, top = self.distortion(stats, baseline)
            maturity = "MATURE" if stats.count >= MIN_EVENTS_FOR_FEEDBACK else "immature"
            b = stats.bucket_info()
            if score >= 0.8:
                status = "HIGH DISTORTION"
            elif score >= DISTORTION_THRESHOLD:
                status = "DISTORTED"
            else:
                status = "normal"

            lines.append(f"Pattern: {key}")
            lines.append(f"  Count: {stats.count} ({maturity})  Status: {status}")
            lines.append(f"  Distortion: {score:.3f}  Top shift: {top} ({delta[top]:+.2f})")
            lines.append(f"  Buckets: DoQ={b['doq']}  COCLI={b['cocli']}  RII={b['rii']}")
            lines.append(f"  Verdicts: {dict(stats.verdict_counts)}")

            if score >= DISTORTION_THRESHOLD and stats.count >= MIN_EVENTS_FOR_FEEDBACK:
                proposal = propose_adjustment(top, abs(delta[top]), stats, key)
                if proposal:
                    cd = "COOLDOWN" if self._is_on_cooldown(proposal.key) else "ready"
                    lines.append(f"  -> Proposal: {proposal.key} {'+' if proposal.delta > 0 else ''}{proposal.delta} ({cd})")
            lines.append("")

        if self.adjustment_log:
            lines.append("--- Adjustment History ---")
            for e in self.adjustment_log:
                lines.append(f"  {e['applied_at']}: {e['key']} {e['old']} -> {e['new']} (d={e.get('distortion_score','?')}, {e['reason']})")
        return "\n".join(lines)

    def bias_report(self) -> str:
        return self.distortion_report()

    def save(self, path: str | Path) -> None:
        data = {
            "version": "0.2", "total_ingested": self._total_ingested,
            "patterns": {k: v.to_dict() for k, v in self.store.items()},
            "adjustment_log": self.adjustment_log,
            "cooldowns": dict(self._cooldown_counters),
        }
        Path(path).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    @staticmethod
    def _card_to_event(card: DDADecisionCard) -> PatternEvent:
        s = card.assessment.sensor_inputs
        return PatternEvent(
            ts=card.created_at, verdict=card.decision.verdict.value,
            chd_phase=s.chd.phase if s and s.chd else None,
            lptm_transition=s.lptm.transition_label if s and s.lptm else None,
            cag_phase=s.cag.phase_label if s and s.cag else None,
            doq=card.assessment.doq, cocli=card.assessment.cocli, rii=card.assessment.rii,
        )


# ============================================================
# Demo
# ============================================================

def demo() -> None:
    import random
    from common_schema_models_v02 import (
        DDAAssessment, DDADecision, DDAVerdict,
        SensorInputs, CHDSensorOutput, LPTMSensorOutput, CAGSensorOutput,
    )

    random.seed(42)
    memory = ProtocolMemory()

    def card(iid, verdict, doq, cocli, rii, chd, lptm, cag):
        return DDADecisionCard(
            issue_id=iid,
            assessment=DDAAssessment(
                doq=doq, cci=60, bcdi=50, rii=rii, hri=50, cocli=cocli,
                sensor_inputs=SensorInputs(
                    chd=CHDSensorOutput(chd_score=45, phase=chd, confidence=0.6),
                    lptm=LPTMSensorOutput(pst=0.6, transition_label=lptm, confidence=0.5),
                    cag=CAGSensorOutput(cag_value=0.1, phase_label=cag, verdict="WAIT", confidence=0.5),
                ),
            ),
            decision=DDADecision(verdict=DDAVerdict(verdict), confidence=0.75, rationale="sim"),
        )

    # A: REJECT-heavy, low DoQ (30 events)
    for i in range(30):
        v = "REJECT" if random.random() < 0.70 else "EXECUTE"
        memory.ingest(card(f"A-{i}", v, 33+random.uniform(-5,10), 55+random.uniform(-8,8), 50,
                           "Pattern Drift", "stable_or_noise", "SYNC"))

    # B: HOLD-heavy (25 events)
    for i in range(25):
        v = "HOLD" if random.random() < 0.65 else "EXECUTE"
        memory.ingest(card(f"B-{i}", v, 60+random.uniform(-5,5), 30+random.uniform(-5,5), 55,
                           "Pre-Collapse", "cob_oscillation", "SYNC"))

    # C: Healthy, immature (15 events)
    for i in range(15):
        memory.ingest(card(f"C-{i}", "EXECUTE", 75, 70, 65, "Silent", "breakout", "ACTION_LEAD"))

    # D: PILOT-heavy (22 events)
    for i in range(22):
        v = "PILOT" if random.random() < 0.60 else "EXECUTE"
        memory.ingest(card(f"D-{i}", v, 55, 60, 40+random.uniform(-5,5), "Silent", "stable_or_noise", "SYNC"))

    print(memory.distortion_report())

    # Apply
    print("\n" + "=" * 60)
    print("APPLYING ADJUSTMENT")
    print("=" * 60)
    from orchestrator_v03 import DDA_THRESHOLDS
    th = dict(DDA_THRESHOLDS)
    print(f"Before: {th}")
    applied = memory.apply_adjustments(th, max_adjustments=1)
    for a in applied:
        print(f"Applied: {a}")
    print(f"After:  {th}")

    # Cooldown test
    print("\n--- Second attempt (should be blocked by cooldown) ---")
    applied2 = memory.apply_adjustments(th, max_adjustments=1)
    print(f"Applied: {len(applied2)} adjustments (expected 0)")

    memory.save("/tmp/protocol_memory_v02.json")
    print("\nSaved to /tmp/protocol_memory_v02.json")


if __name__ == "__main__":
    demo()
