🇬🇧 English | 🇮🇹 [Italiano](README.md)

# 🤖 LangBrain

> [!WARNING]
> **LangBrain is a working boilerplate, but it is not production-ready:** a single worker, simulated devices with in-memory state, no observability. Known limits and the roadmap are in [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md).

**A digital body for your intelligent automation and hierarchical-agent projects.**

An experimental LangGraph boilerplate that implements a hierarchical-agent pattern inspired by how an organism works: a **brain** (Supreme Orchestrator) that thinks deliberately and reconciles conflicts, and **organs/components** (N-level sub-agents) that react in real time, acting autonomously when needed and escalating to higher levels only when the situation requires it.

The main demonstration use case is a **smart home**, accompanied by an advanced **medical and physiological homeostasis** demo, but the architecture is designed to be transplanted into any domain—customer support, industrial monitoring, fleet management, and much more.

---

## 🚀 Quick start

Python 3.12 or newer is required.

```bash
python -m venv venv && source venv/bin/activate    # on Windows: .\venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
cp .env.example .env                               # add at least one LLM provider key
python examples/avvia_demo.py                      # sample scenario + "graph in action" page
```

The demo creates a dedicated database (`demo.db`, your data is untouched), starts the server and opens `http://127.0.0.1:8765/demo`. Press **Esegui un ciclo** (run a cycle): the hierarchy nodes light up one after another while the agents (real models) reason, events and devices update and, when the Brain asks for approval on the door, you decide with **Approva** (approve) or **Respingi** (reject).

For the real server: `python -m uvicorn app.api.main:app` (one process), `python -m app.serve` (several processes: the app can be used by several people at the same time) or `docker compose up`. User choices (devices and allowed values, providers and models, HITL, timer) live in [`configurazione.toml`](configurazione.toml), secrets in `.env`. The full guide is [`docs/HOW_TO_CUSTOMIZE.md`](docs/HOW_TO_CUSTOMIZE.md) (in Italian); security and contributions are covered in [`SECURITY.md`](SECURITY.md) and [`CONTRIBUTING.md`](CONTRIBUTING.md).

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
🧠 Brain (Central Orchestrator - Level 0)
  │
  ├── 🔒 Security Organ (Level 1)
  │      │
  │      └── 🔑 Lock Component (Level 2)
  │
  └── ❤️ Cardiovascular Organ (Level 1)
       │
       └── 🫀 Heart Rate Component (Level 2)
```

In practice, **every graph node can in turn act as a small brain for the level below it.** The same `base_agent.py`, the same `DynamicAgent`, and the same audit-log mechanism apply recursively, whether coordinating two main organs or twenty subcomponents nested across N levels.

---

## 🧬 The vocabulary of the body

The public names of the framework are those of the human body, so the same core fits any domain (a home, a plant, a clinic):

| Part | Meaning |
|---|---|
| **Brain · Organ · Tissue · Cell** | the agents that use a model, by level of the hierarchy |
| **Reflex** (`app.tools.reflex`) | a deterministic action, no model and no decision |
| **Receptor** | turns an outside stimulus into a **signal**; it may use a model but decides nothing |
| **Effector** (`[effectors.*]`, `/effectors`) | the thing that acts on the world; `SimulatedEffector` is the in-memory one |
| **Nervous system** (`NervousSystem`) | the log of events, signals and node runs |

The old names (`BaseTool`, `IoTDeviceTool`, `EventLog`, `[devices.*]`, `/tools`, `sensor_readings`) keep working as aliases; the full table is in [`CHANGELOG.md`](CHANGELOG.md). The **model reads, deterministic code decides, doubt goes to a human**: `Reflex`es are the code that decides, agents the model that reads.

### Extensions and API first

The core is an API: everything the framework does is a REST call, including the statistics for charts (`GET /events/stats`, `/graph/stats`, `/signals/stats`) and the configuration (`/config/api/*`), with generic parameters that fit any domain. A use case does not change the core: a module in `app/nodes/` exposes `ESTENSIONE` and brings its own routes, pages, settings and prompts ([docs/HOW_TO_CUSTOMIZE.en.md](docs/HOW_TO_CUSTOMIZE.en.md#10-extensions-a-node-that-plugs-into-the-api)). The `/config` page edits the same configuration files (`.env`, `configurazione.toml`) with a form; the administrator can **restart the app** from there.

---

## 🦾 System Anatomy

```text
           ┌─────────────────────┐
           │   🧠 BRAIN            │
           │   (Orchestrator L0)  │
           │   Priority: 1000.0   │
           └──────────┬────────────┘
            │
          reads/writes to the nervous system
            │
      ┌───────────────┼───────────────┐
      │               │               │
    ┌──────▼─────┐  ┌──────▼─────┐  ┌──────▼──────┐
    │ 🌡️ Climate  │  │ 🔒 Security │  │ ❤️ Cardio    │
    │ (Organ L1)  │  │ (Organ L1)  │  │ (Organ L1)   │
    └──────┬──────┘  └──────┬──────┘  └──────┬───────┘
      │                │                │
    ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼───────┐
    │ Sensors/    │  │ Component   │  │ Component    │
    │ Actuators   │  │ Lock L2     │  │ Pacemaker L2 │
    └─────────────┘  └─────────────┘  └──────────────┘

    ═══════════════════════════════════════════════
          🩸 NERVOUS SYSTEM (Event Log / DB)
    ═══════════════════════════════════════════════
```

### The Brain (Supreme Orchestrator)

It has maximum privileges (priority `1000.0`). It receives escalations from sub-agents when unresolvable conflicts arise and performs the entire system's routine check (`check_body_status`).

### The Organs and Components (`DynamicAgent` & `BaseAgent`)

They act autonomously on their sensor/device targets. If they detect a conflict in the DB or a severe anomaly (such as cardiac arrhythmia or suspicious unlocking), they generate a structured escalation to their Parent agent.

---

## 🎛️ Dynamic Human-in-the-Loop (HITL)

Human-approval interruptions can be inserted **dynamically anywhere in the graph flow** through the wrapper in `app/graph/builder.py` and managed entirely through the REST API without restarting the server.

- **Where it applies (user choice):** in `configurazione.toml`, `[hitl] livello` selects `nodi` (the wrapper stops the nodes you list), `brain` (the Brain asks for approval on critical targets) or `entrambi` (both). Emergency pauses (unusable LLM, unresolved device fault) stay active at any level.
- **Timer:** with `[hitl] timer_attivo = 1` the API exposes the remaining seconds and, on expiry, the system decides (the Brain with its model), rejects, or leaves the graph paused, according to `azione_alla_scadenza`.
- **Dynamic Activation via API:**
  `POST /hitl/config` lets you specify nodes (`hitl_nodes`), protected sensors (`hitl_targets`), critical actions (`hitl_actions`), and the maximum wait in seconds (`max_wait_seconds`).

- **Three Resume Modes (`POST /graph/resume`):**

  | `decision` | Behavior |
  |---|---|
  | `APPROVA` | The Brain applies the proposed action to the device (if `configurazione.toml` allows that command) and writes `RECONCILED_<action>` to the DB. |
  | `RESPINGI` | Writes `REJECTED_<action>` to the DB. The device remains blocked until the TTL expires or it is manually unblocked. |
  | `OVERRIDE` | **Semantic God Mode**: the natural-language `reasoning` field is sent to the MAO with a Semantic Arbitration prompt. The MAO translates the sentence into a JSON array of `{target, action, value}` commands that are physically executed through `force_execute_tool`, which bypasses all priority locks and records every action in the DB with `actor: "Brain_Override"`. |

  ```http
  POST /graph/resume
  Content-Type: application/json

  {
    "decision": "OVERRIDE",
    "reasoning": "Override the energy lock: elderly occupant needs heat. Set heater to 22°C.",
    "thread_id": "api_session"
  }
  ```

---


## 🗂️ Project Structure

```text
LangBrain/
├── README.md / README.en.md
├── CHANGELOG.md                    # What changed and the migration table of the names
├── REQUIREMENTS.md / REQUIREMENTS.en.md
├── SECURITY.md                     # How to report vulnerabilities and what to expect from security
├── CONTRIBUTING.md                 # How to contribute
├── LICENCE
├── Dockerfile
├── docker-compose.yml
├── requirements.txt                # Direct dependencies, exact versions
├── requirements.lock               # Full pinned list (used by the Dockerfile)
├── requirements-dev.txt            # Plus pytest
├── .env.example                    # Template of the secrets (provider and API keys); your .env stays out of Git
├── configurazione.toml             # User choices: effectors and accepted values, LLM providers, HITL, signals (config.example.toml: English version)
├── run_loop.py                     # Event-driven loop with a producer of sensor events
├── .github/workflows/ci.yml        # CI: tests on Python 3.12/3.14, secret and dependency scan
├── app/
│   ├── MAO/
│   │   └── model_access_object.py  # Model Access Object (OpenRouter, Google AI Studio, Mistral, local LLM)
│   ├── agents/
│   │   ├── base_agent.py           # Common DNA of every agent (applies state, priorities, escalation)
│   │   ├── dynamic_agent.py        # Agent configurable at runtime (Levels 1..N), one model call per cycle
│   │   ├── agent_registry.py       # Hierarchical registry on SQLite
│   │   ├── agenti_nativi.py        # Loads the native example agents from examples/ by name
│   │   └── agent_climate.py, medical_agents.py   # Aliases of the old paths (now in examples/)
│   ├── core/
│   │   ├── configurazione.py       # Reading and validation of configurazione.toml (Italian or English names)
│   │   ├── constants.py            # Control flags, TTL, level names (Brain, Organ, Tissue, Cell)
│   │   ├── errori_llm.py           # Classified model errors (tokens, key, limits) and secret masking
│   │   ├── estensioni.py           # How a node plugs into the API (routes, pages, settings, prompts)
│   │   ├── lingua.py               # Language of prompts, messages and pages (it, en)
│   │   ├── modelli_agenti.py       # LLM model per agent, inherited from the parent
│   │   ├── pagine_web.py           # Localised web pages (texts in app/static/i18n, shared theme)
│   │   ├── priorita.py             # Priority rules for locks
│   │   ├── prompt.py               # Configurable prompts (customisable role, answer format in the code)
│   │   ├── riavvio.py              # Restart of the application from the /config page (admin only)
│   │   ├── risultati.py            # Structured outcome of tool reading/actuation
│   │   ├── ruoli.py                # Roles of API callers and permission matrix
│   │   └── settings_editor.py      # Editor of .env and configurazione.toml behind the /config page
│   ├── graph/
│   │   ├── orchestrator.py         # Brain (BrainAgent - Level 0), one call for all the escalations of the cycle
│   │   ├── builder.py              # LangGraph builder with HITL wrapper and trace of the node runs
│   │   ├── hitl_config.py          # Dynamic HITL configuration
│   │   ├── timer_hitl.py           # Operator wait timer and action at expiry
│   │   └── state.py                # Shared GraphState (signals with a time window)
│   ├── tools/
│   │   ├── reflex.py               # Reflex (deterministic action) and Receptor (stimulus → signal)
│   │   ├── effectors.py            # Simulated in-memory effectors and shared registry (registra_tool)
│   │   ├── motor_control.py        # Actuation with priority check and override
│   │   ├── nervous_system.py       # Nervous system: event audit log, signals, node runs
│   │   ├── statistiche_eventi.py   # Nervous system indicators (GET /events/stats)
│   │   ├── statistiche_grafo.py    # Statistics for charts: graph nodes and signals over time
│   │   └── baseTool.py, sensor_tools.py, event_log.py, tool_wrapper.py, medical_tools.py   # Aliases of the old names
│   ├── db/
│   │   ├── database.py             # SQLite schema (events, signals, node_runs, ...)
│   │   └── scenario.py             # Reproducible data scenarios
│   ├── api/
│   │   ├── main.py                 # FastAPI REST API (graph, streaming, HITL, agents, effectors, events, statistics)
│   │   ├── settings_routes.py      # Routes of the /config page (read, save, restore, restart)
│   │   └── energia.py              # /energia/... routes of the energy simulation
│   ├── nodes/                      # Extensions: modules put here plug into the API (energia.py: the energy simulation)
│   ├── simulazione/                # Simulated electricity and gas network (engine, scenarios, background run)
│   ├── static/
│   │   ├── demo_grafo.html         # "Graph at work" page, served on GET /demo
│   │   ├── config.html             # Settings page, served on GET /config
│   │   ├── energia.html            # Page of the simulated energy network, served on GET /energia
│   │   ├── settings_form.js, common.css   # Form module and theme shared by the pages
│   │   └── i18n/                   # Page texts in Italian and English
│   └── checkpointer.py             # Persistent LangGraph checkpointer on SQLite
├── examples/
│   ├── avvia_demo.py               # Server + test scenario + web page of the graph
│   ├── crea_scenario.py            # Resets a database and creates a test scenario
│   ├── smart_home/                 # Example domain: Climate agent
│   ├── hierarchical_pattern/
│   │   └── demo_hierarchy.py       # N-level smart home hierarchy from code
│   └── medical_homeostasis/        # Example domain: physiological agents and tools
│       └── demo_medical_homeostasis.py # Physiological homeostasis and pathology resolution
├── scripts/
│   ├── scansione_sicurezza.sh      # Secret and dependency scan
│   └── stress_energia.py           # Load test of the energy simulation
├── tests/                          # pytest suite (temporary databases, no real LLM)
├── tests_windows/                  # The same tests with Windows-friendly paths, plus those written on Windows
└── docs/
    ├── HOW_TO_CUSTOMIZE.md / .en.md # Customisation guide, API map, extensions
    ├── PROJECT_STATUS.md / .en.md  # Project status, known limits and roadmap
    ├── SIMULAZIONE_ENERGIA.md      # Energy network simulation: model, page, API, load test (Italian)
    ├── API_SMOKE_TEST.md           # API smoke test with curl (Bash)
    └── API_SMOKE_TEST_WINDOWS.ps1  # API smoke test for PowerShell
```

---

## 🛠️ Technology Stack

| Component | Choice | Why |
|---|---|---|
| Agent Framework | **LangGraph** | Stateful graphs with cycles, conditional routing, and native checkpointing |
| LLM Provider | **MAO Proxy** | OpenRouter, Google AI Studio (Gemini), Mistral and local LLMs (vLLM, LM Studio), with a model per agent |
| API Server | **FastAPI** | Fully headless asynchronous HTTP/REST server with interactive Swagger UI |
| Database | **SQLite (aiosqlite)** | Zero setup, persistent event audit log, agent registry, and state |

---

## ⚙️ MAO Configuration and Domain Contracts

LLM calls are asynchronous. The MAO HTTP timeout is configured through `MAO_TIMEOUT_SECONDS` and defaults to **40 seconds** when the variable is missing or invalid. For a model hosted on another LAN machine, set both `LOCAL_MODEL_BASE_URL` and `LOCAL_MODEL_DOCKER_BASE_URL` to the reachable OpenAI-compatible endpoint (for example, `http://192.168.1.50:8080/v1`) and set `LOCAL_MODEL` to the ID returned by `GET /v1/models`.

LangBrain treats `action`, `old_value`, and `new_value` as extensible data. The boilerplate cannot know the valid physical states of every domain: developers adding a tool or agent must implement and test their own validation/mapping (for example, `LOCKED`/`UNLOCKED` for a lock). Internal flags such as `REJECTED` and `BLOCKED` are reported by the health check but are not automatically converted into a physical state.

Who can do what: API keys have three roles (`tirocinante` read-only, `medico_di_guardia` operator, `primario` administrator) and no agent can command a device or a value that is not listed in `configurazione.toml`. Details in [`docs/HOW_TO_CUSTOMIZE.md`](docs/HOW_TO_CUSTOMIZE.md) and [`SECURITY.md`](SECURITY.md).

---

## 🎬 Included Demonstration Examples

1. **Graph in action (`examples/avvia_demo.py`):** a server with a sample scenario and the `/demo` page showing executed nodes, events, devices and the operator approval request in real time.
   ```bash
   python examples/avvia_demo.py [--scenario conflitto_porta|base|finestra_aperta] [--modello Brain=mistral:ministral-8b-latest]
   ```
2. **Data scenarios (`examples/crea_scenario.py`):** resets a database and creates hierarchy, history and an optional conflict, to try the API by hand (it makes a backup first).
3. **Smart Home hierarchy (`examples/hierarchical_pattern/demo_hierarchy.py`):** builds the Brain → Organs → Components hierarchy from code and shows the recursive escalation from the door-lock component up to the Brain, which asks for operator approval.
4. **Medical homeostasis (`examples/medical_homeostasis/demo_medical_homeostasis.py`):** tachycardia (160 BPM) and hypoxia (82% SpO2): the respiratory agent restores saturation to 95%; the severe tachycardia climbs to the Brain and, if rejected, the operator unblocks and the protocol restores 100 BPM.

5. **Simulated energy network (`/energia`):** electricity, gas and storage on plausible data of Italy (or a large generated network for load tests), lasting up to years at an adjustable speed, with failures, gas crises and heat waves. A hierarchy of agents (Brain, electricity and gas organs, four components) works on top of the network, woken by the nervous system when anomalies appear: the agents operate the levers of the network and ask the operator for the measures that stop industry. It is an extension of the core (`app/nodes/energia.py`). Details in [`docs/SIMULAZIONE_ENERGIA.md`](docs/SIMULAZIONE_ENERGIA.md) (Italian).
   ```bash
   python -m uvicorn app.api.main:app --port 8765     # then open http://127.0.0.1:8765/energia (settings: /config)
   python scripts/stress_energia.py --anni 10          # load test without a server
   ```

Demos 3 and 4 use their own databases (`demo_gerarchia.db`, `demo_medica.db`) and the default LLM provider from `.env`.

---

## 🧪 Tests and Checks

```bash
python -m pytest tests                 # whole suite: temporary databases, no real LLM calls
python -m pytest tests_windows         # on Windows: the same tests with suitable paths, plus those written there
bash scripts/scansione_sicurezza.sh    # secrets and vulnerable dependencies (needs gitleaks and pip-audit)
```

The same checks run in CI on every push (`.github/workflows/ci.yml`). To check a running server by hand: [Bash smoke test](docs/API_SMOKE_TEST.md) or [Windows PowerShell](docs/API_SMOKE_TEST_WINDOWS.ps1) (note: they reset the server's database, so use a throwaway instance such as the one from `examples/avvia_demo.py`).

---

## 📄 License

Polyform Small Business License 1.0.0, free for personal and business use up to the revenue threshold.
