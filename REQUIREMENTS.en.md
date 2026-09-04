🇬🇧 English | 🇮🇹 [Italiano](REQUIREMENTS.md)

# Project: LangBrain

## Product Goal

A demonstration LangGraph boilerplate aimed at general developers and indie hackers. It shows a hierarchical-agent pattern (orchestrator + sub-agents), but it is **not production-ready**. The concrete use case is IoT/Smart Home, and the architecture can be adapted to other domains after implementing security, persistence, and domain-specific contracts.

## Technology Stack

- **Agent Framework**: LangGraph
- **API Server**: FastAPI (with REST/WebSocket streaming support)
- **Database**: SQLite for the boilerplate (schema easily migrated to Postgres)
- **Containerization**: Docker & Docker Compose

## Conceptual Architecture

### Brain (Orchestrator)

- High-performance model (for example, Mistral Large).
- Cyclical execution interval (not real-time reactive).
- Reads event history from the DB during every cycle.
- Decides macro adjustments based on historical patterns.
- Has access to activation/adjustment tools with priority/final authority during its cycle.

### Sub-agents (By Sensor Domain)

- Examples: Climate, Security, Lighting.
- Lightweight, inexpensive model (for example, Mistral Small).
- Receive mock sensor data (simulated data/preloaded scenarios).
- Decide autonomously within known thresholds.
- Escalate to the Brain when a case is ambiguous or outside its threshold.
- Tools to manage sensors/actuators independently of the Brain.

## Conflict Management (Event Log + Reconciliation)

1. **Reading recent events**: The sub-agent calls `get_recent_events(target)` (5–10 minute window).
2. **Action/Logging**: If there are no conflicts, it acts and writes `log_event()`.
3. **Escalation**: If it detects a conflict (for example, a recent action by the Brain), it escalates to the Brain.
4. **Reconciliation**: During its cycle, the Brain reads all recent events and performs reconciliation (confirm, correct, or ignore).

## Human-in-the-Loop (HITL)

Configurable by node, device, or action through the dynamic `hitl_config.py` manager or via the REST API (`POST /hitl/config`).
Graph cycles submitted through `POST /graph/run` accept a unique `thread_id` to seamlessly connect execution progress (`GET /graph/state?thread_id=...`) with a possible human operator decision (`POST /graph/resume`).

### Three HITL Decision Modes (`POST /graph/resume`)

| `decision` | Behavior |
|---|---|
| `APPROVA` | The wrapper writes `RECONCILED_<action>` to the DB and ends the cycle. |
| `RESPINGI` | The wrapper writes `REJECTED_<action>` to the DB, blocks the device, and ends the cycle. |
| `OVERRIDE` | **Semantic God Mode**: the `reasoning` field is sent to the MAO with the Semantic Arbitration prompt. The MAO translates the natural-language sentence into a JSON array of `{target, action, value}` commands. Each command is executed through `force_execute_tool`, which deliberately bypasses priority locks (`check_priority_lock`) and records the action in the DB with `actor: "Brain_Override"`. |

### On-Demand Tools

Unregistered devices are automatically created as an `IoTDeviceTool` with an `OFF` state upon first access through `GET/POST /tools/{device_id}` or while executing a semantic Override.

### MAO Timeout and Domain Contracts

- The MAO uses asynchronous clients and an HTTP timeout configured through `MAO_TIMEOUT_SECONDS` (default: `40` seconds).
- `LOCAL_MODEL_BASE_URL` configures native access; `LOCAL_MODEL_DOCKER_BASE_URL` configures the endpoint seen by the container. They may be identical for a model hosted on another LAN machine.
- `LOCAL_MODEL` must exactly match an ID returned by the OpenAI-compatible `/v1/models` endpoint.
- The semantics and normalization of `action`, `old_value`, and `new_value` are application-domain contracts. Developers must validate allowed values in each tool/agent; the core does not automatically convert symbolic actions into physical states.
- The health check reports control flags (`REJECTED`, `BLOCKED`, etc.) as `MACRO_ADJUSTMENT_REQUIRED` without selecting a replacement physical state.

## Data Models & DB

### DB Schema (SQLite)

- `events`: `event_id` (PK), `actor`, `action`, `target`, `old_value`, `new_value`, `reasoning`, `timestamp`, `escalated` (bool). Indexes on `target` and `timestamp`.
- `readings`: `reading_id` (PK), `sensor_id`, `agent_owner`, `value`, `unit`, `timestamp`.
- `agents_registry`: `name` (PK), `level`, `parent_agent_name`, `managed_targets`, `sub_agent_names`, `system_prompt_template`, `user_prompt_template`, `conflict_window_minutes`, `priority_weight`.

### Shared State (`GraphState`)

- `readings`: Current sensor readings.
- `recent_events`: Recent window of events read from the DB.
- `pending_escalations`: List of pending escalations.
- `hitl_required`: Human-in-the-loop flag/state.
- `next_agent`: Destination node in the LangGraph graph (`"brain"`, `"organ_security"`, `"END"`, etc.).
- `config`: Thresholds, HITL flags, and dynamic configuration.

## Project Structure

```text
LangBrain/
├── REQUIREMENTS.md
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── test_results.json               # Real-time result of the test suite
├── .env                            # LLM provider and Brain prompts configuration
├── .env.example                    # Template configuration file (no secrets)
├── app/
│   ├── MAO/
│   │   └── model_access_object.py  # MAO (Model Access Object for local/OpenAI-compatible LLMs)
│   ├── agents/
│   │   ├── base_agent.py           # Abstract base class for sub-agents
│   │   ├── agent_climate.py        # Native Climate sub-agent
│   │   ├── dynamic_agent.py        # Configurable N-Level Dynamic Agent
│   │   ├── agent_registry.py       # Hierarchical registry persisted in SQLite
│   │   └── medical_agents.py       # Physiological agents (Cardiovascular, Respiratory)
│   ├── graph/
│   │   ├── orchestrator.py         # Brain (BrainAgent - Level 0)
│   │   ├── builder.py              # LangGraph builder with HITL wrapper
│   │   ├── hitl_config.py          # Dynamic HITL configuration manager
│   │   └── state.py                # Shared GraphState
│   ├── tools/
│   │   ├── baseTool.py             # Abstract base class for tools
│   │   ├── tool_wrapper.py         # execute_tool_safely (priority checks) + force_execute_tool (Override)
│   │   ├── event_log.py            # log_event(), mark_resolved(), unblock_target()
│   │   ├── sensor_tools.py         # Mock tools for IoT sensors/actuators (on-demand singleton)
│   │   └── medical_tools.py        # Medical tools (Pacemaker, SpO2, Normalizer)
│   ├── db/
│   │   └── database.py             # SQLite setup (tabelle events, readings, agents_registry)
│   ├── api/
│   │   └── main.py                 # FastAPI (REST + RunCycleRequest con thread_id + HITL config)
│   └── observability/
│       └── tracing.py              # Logging strutturato / tracing
├── examples/
│   ├── hierarchical_pattern/
│   │   └── demo_hierarchy.py       # Smart Home Hierarchy Demo (N-levels)
│   └── medical_homeostasis/
│       └── demo_medical_homeostasis.py # Medical homeostasis demo
└── docs/
    └── HOW_TO_CUSTOMIZE.md
```
