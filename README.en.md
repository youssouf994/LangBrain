🇬🇧 English | 🇮🇹 [Italiano](README.md)

# 🤖 LangBrain

> [!WARNING]
> **LangBrain is not ready for production use.** It is a demonstration prototype/boilerplate: before using it in real-world environments, complete the security, persistence, concurrency, deployment, observability, and testing work listed in [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md).

**A digital body for your intelligent automation and hierarchical-agent projects.**

An experimental LangGraph boilerplate that implements a hierarchical-agent pattern inspired by how an organism works: a **brain** (Supreme Orchestrator) that thinks deliberately and reconciles conflicts, and **organs/components** (N-level sub-agents) that react in real time, acting autonomously when needed and escalating to higher levels only when the situation requires it.

The main demonstration use case is a **smart home**, accompanied by an advanced **medical and physiological homeostasis** demo, but the architecture is designed to be transplanted into any domain—customer support, industrial monitoring, fleet management, and much more.

---

## 🧠 The idea: a nervous system, not a function tree

Most multi-agent systems out there are rigid organization charts: a boss who decides everything and sub-agents that execute orders without ever acting on their own. It is a good model for a bureaucratic office. It is a terrible model for a body that must survive in the real world.

Your body does not work that way. If you put your hand on a hot plate, **you do not wait for your brain to process the situation** and send the command to pull it away—the spinal cord reacts on its own, in milliseconds, through a reflex arc. The brain is informed *afterward*, when it needs to understand what happened and perhaps make a more strategic decision (for example, “do not touch that area of the kitchen again”).

This boilerplate reproduces exactly that logic:

- **The Brain (Supreme Orchestrator - Level 0)** — thinks cyclically, reviews recent history, and decides on strategic adjustments or conflict resolutions.
- **The Organs & Components (N-Level Sub-agents)** — specialize in macro areas or peripherals. They react autonomously within their scope and **escalate to the Parent** when the situation is ambiguous or conflicting.
- **The Nervous System (Event Log & Audit)** — the channel through which every agent records what it did and reads recent actions to avoid conflicts or overwrites.

---

## 🧬 An organism with recursive extensibility (N Levels)

A real body does not stop at “brain + organs.” When viewed closely, each organ is itself a system made of specialized substructures:

```text
🧠 Cervello (Orchestratore centrale - Livello 0)
   │
   ├── 🔒 Organo Sicurezza (Livello 1)
   │      │
   │      └── 🔑 Componente Serratura (Livello 2)
   │
   └── ❤️ Organo Cardiovascolare (Livello 1)
          │
          └── 🫀 Componente Frequenza Cardiaca (Livello 2)
```

In practice, **every graph node can in turn act as a small brain for the level below it.** The same `base_agent.py`, the same `DynamicAgent`, and the same audit-log mechanism apply recursively, whether coordinating two main organs or twenty subcomponents nested across N levels.

---

## 🦾 System Anatomy

```text
                          ┌─────────────────────┐
                          │   🧠 CERVELLO         │
                          │   (Orchestratore L0)  │
                          │   Priorità: 1000.0    │
                          └──────────┬────────────┘
                                     │
                    legge/scrive sul sistema nervoso
                                     │
                     ┌───────────────┼───────────────┐
                     │               │               │
              ┌──────▼─────┐  ┌──────▼─────┐  ┌──────▼──────┐
              │ 🌡️ Clima    │  │ 🔒 Sicurezza│  │ ❤️ Cardio    │
              │ (Organo L1) │  │ (Organo L1) │  │ (Organo L1)  │
              └──────┬──────┘  └──────┬──────┘  └──────┬───────┘
                     │                │                │
              ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼───────┐
              │ Sensori/    │  │ Componente  │  │ Componente   │
              │ Attuatori   │  │ Serratura L2│  │ Pacemaker L2 │
              └─────────────┘  └─────────────┘  └──────────────┘

              ═══════════════════════════════════════════════
                    🩸 SISTEMA NERVOSO (Event Log / DB)
              ═══════════════════════════════════════════════
```

### The Brain (Supreme Orchestrator)

It has maximum privileges (priority `1000.0`). It receives escalations from sub-agents when unresolvable conflicts arise and performs the entire system's routine check (`check_body_status`).

### The Organs and Components (`DynamicAgent` & `BaseAgent`)

They act autonomously on their sensor/device targets. If they detect a conflict in the DB or a severe anomaly (such as cardiac arrhythmia or suspicious unlocking), they generate a structured escalation to their Parent agent.

---

## 🎛️ Dynamic Human-in-the-Loop (HITL)

Human-approval interruptions can be inserted **dynamically anywhere in the graph flow** through the wrapper in `app/graph/builder.py` and managed entirely through the REST API without restarting the server.

- **Dynamic Activation via API:**
  `POST /hitl/config` lets you specify nodes (`hitl_nodes`), protected sensors (`hitl_targets`), critical actions (`hitl_actions`), and the maximum wait in seconds (`max_wait_seconds`).

- **Three Resume Modes (`POST /graph/resume`):**

  | `decision` | Behavior |
  |---|---|
  | `APPROVA` | Writes `RECONCILED_<action>` to the DB. No physical change is made to the device. |
  | `RESPINGI` | Writes `REJECTED_<action>` to the DB. The device remains blocked until the TTL expires or it is manually unblocked. |
  | `OVERRIDE` | **Semantic God Mode**: the natural-language `reasoning` field is sent to the MAO with a Semantic Arbitration prompt. The MAO translates the sentence into a JSON array of `{target, action, value}` commands that are physically executed through `force_execute_tool`, which bypasses all priority locks and records every action in the DB with `actor: "Brain_Override"`. |

  ```http
  POST /graph/resume
  Content-Type: application/json

  {
    "decision": "OVERRIDE",
    "reasoning": "Ignora il blocco dell'energia: mia nonna ha freddo. Accendi la stufa a 22 gradi.",
    "thread_id": "api_session"
  }
  ```

---


## 🗂️ Project Structure

```text
LangBrain/
├── REQUIREMENTS.md
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── test_results.json               # Esito in tempo reale della suite di test
├── .env                            # Provider LLM e Prompt configurabili del Brain
├── .env.example                    # Template di configurazione senza segreti
├── app/
│   ├── MAO/
│   │   └── model_access_object.py  # Model Access Object (OpenRouter, Gemini, LLM Locale)
│   ├── agents/
│   │   ├── base_agent.py           # DNA comune di ogni agente (applica stato, idoneità, escalation)
│   │   ├── agent_climate.py        # Agente Clima nativo
│   │   ├── dynamic_agent.py        # Agente Dinamico configurabile a runtime (Livelli 1..N)
│   │   ├── agent_registry.py       # Registro gerarchico salvato su DB SQLite
│   │   └── medical_agents.py       # Agenti Fisiologici (Cardiovascolare, Respiratorio)
│   ├── graph/
│   │   ├── orchestrator.py         # Cervello (BrainAgent - Livello 0)
│   │   ├── builder.py              # Builder del grafo LangGraph con wrapper HITL
│   │   ├── hitl_config.py          # Manager della configurazione dinamica HITL
│   │   └── state.py                # GraphState condiviso
│   ├── tools/
│   │   ├── baseTool.py             # Classe base astratta per tutti i tool IoT/Medici
│   │   ├── sensor_tools.py         # Tool Smart Home (AC, Serratura, Allarme)
│   │   ├── medical_tools.py        # Tool Medici (Pacemaker, Ventilatore SpO2, Normalizzatore)
│   │   ├── event_log.py            # Sistema Nervoso: audit log eventi e sblocco TTL
│   │   └── tool_wrapper.py         # Tool execution logging/wrapper
│   ├── db/
│   │   └── database.py             # Setup SQLite (tabelle events, readings, agents_registry)
│   ├── api/
│   │   └── main.py                 # API REST FastAPI complete (100% headless con DELETE /system/reset)
│   ├── checkpointer.py             # Checkpointer LangGraph per la persistenza
│   └── observability/
│       └── tracing.py              # Tracing e logging strutturato
├── examples/
│   ├── hierarchical_pattern/
│   │   └── demo_hierarchy.py       # Demo Gerarchia Smart Home N-Livelli
│   └── medical_homeostasis/
│       └── demo_medical_homeostasis.py # Demo Omeostasi Fisiologica & Risoluzione Patologie
├── tests/
│   └── test_all.py                 # Suite di test automatizzata (38 test unitari & integrati)
└── docs/
    └── HOW_TO_CUSTOMIZE.md         # Guida alla personalizzazione e mappatura API
```

---

## 🛠️ Technology Stack

| Component | Choice | Why |
|---|---|---|
| Agent Framework | **LangGraph** | Stateful graphs with cycles, conditional routing, and native checkpointing |
| LLM Provider | **MAO Proxy** | Support for OpenRouter, Google AI Studio (Gemini), and local LLMs (vLLM, LM Studio) |
| API Server | **FastAPI** | Fully headless asynchronous HTTP/REST server with interactive Swagger UI |
| Database | **SQLite (aiosqlite)** | Zero setup, persistent event audit log, agent registry, and state |

---

## ⚙️ MAO Configuration and Domain Contracts

LLM calls are asynchronous. The MAO HTTP timeout is configured through `MAO_TIMEOUT_SECONDS` and defaults to **40 seconds** when the variable is missing or invalid. For a model hosted on another LAN machine, set both `LOCAL_MODEL_BASE_URL` and `LOCAL_MODEL_DOCKER_BASE_URL` to the reachable OpenAI-compatible endpoint (for example, `http://172.16.77.153:8080/v1`) and set `LOCAL_MODEL` to the ID returned by `GET /v1/models`.

LangBrain treats `action`, `old_value`, and `new_value` as extensible data. The boilerplate cannot know the valid physical states of every domain: developers adding a tool or agent must implement and test their own validation/mapping (for example, `LOCKED`/`UNLOCKED` for a lock). Internal flags such as `REJECTED` and `BLOCKED` are reported by the health check but are not automatically converted into a physical state.

The `smoke_test_full.ps1` and `smoke_test_full_v2.ps1` scripts first verify `/v1/models`, configure a real interrupt for the override target, and explicitly decode UTF-8 responses on Windows PowerShell 5.1.

---

## 🚀 Included Demonstration Examples

1. **Smart Home Hierarchy (`examples/hierarchical_pattern/demo_hierarchy.py`):**
   ```bash
   python3 examples/hierarchical_pattern/demo_hierarchy.py
   ```
   Demonstrates recursive escalation from a lock subcomponent (Level 2) to the security organ (Level 1) and finally to the Brain (Level 0).

2. **Medical Homeostasis & Pathologies (`examples/medical_homeostasis/demo_medical_homeostasis.py`):**
   ```bash
   python3 examples/medical_homeostasis/demo_medical_homeostasis.py
   ```
   Simulates the onset of clinical pathologies (tachycardia at 160 BPM and hypoxia at 82% SpO2) and the intervention of Physiological Agents to return the organism to homeostasis.

---

## 🧪 Running the Test Suite

To run the legacy custom suite and update `test_results.json`:
```bash
python3 tests/test_all.py
```

To run the standard asynchronous regression tests:

```bash
python -m unittest tests.test_blocking_regressions -v
```

For the end-to-end local-model test, use `smoke_test_full_v2.ps1`. For a compact API check, use the [Windows PowerShell smoke test](docs/API_SMOKE_TEST_WINDOWS.ps1); a [Bash version](docs/API_SMOKE_TEST.md) is also available.

---

## 📄 License

Polyform Small Business License 1.0.0, free for personal and business use up to the revenue threshold.
