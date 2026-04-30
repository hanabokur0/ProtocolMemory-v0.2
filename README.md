# ProtocolMemory v0.2

Minimal adaptive layer for decision systems

---

## What This Is

ProtocolMemory is **NOT** a learning system.

It is:

> **a constraint layer that prevents decision systems from drifting**

---

## What It Does

ProtocolMemory:

- detects distortion in decision patterns  
- compares conditional vs baseline distributions  
- suggests minimal threshold adjustments  
- applies them slowly and safely  

---

## What It Does NOT Do

ProtocolMemory:

- does not optimize aggressively  
- does not retrain models  
- does not maximize performance  

---

## Core Philosophy

Most systems fail because:

- they learn too fast  
- they adjust too much  
- they lose structure  

ProtocolMemory does the opposite:

> **learn slowly, change minimally, preserve structure**

---

## Key Mechanisms

- **Delayed learning** (no adjustment before 20+ events)  
- **Single adjustment** (one change at a time)  
- **Cooldown** (no repeated changes on the same parameter)  
- **Threshold clipping** (safe bounds enforced)  
- **Distortion-based feedback** (not performance-based)  

---

## Why This Matters

In unstable environments:

- markets  
- geopolitics  
- social systems  

the problem is not lack of intelligence.

It is:

> **loss of stability due to overreaction**

---

## Design Goal

> **Keep the system usable under instability.**

---

## Usage

```python
memory = ProtocolMemory()

memory.ingest(card)

adjustments = memory.suggest_adjustments()
memory.apply_adjustments(thresholds)
Position in LoPAS

ProtocolMemory is:

NOT the brain (DDA)
NOT the sensor (LPTM / CAG / CHD)

It is:

the memory layer that prevents structural collapse

Final Line

Intelligence learns fast.
Systems that survive learn slowly.
