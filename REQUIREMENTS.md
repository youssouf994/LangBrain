# Progetto: Agentic IoT Boilerplate (LangGraph)

## Obiettivo del Prodotto
Boilerplate LangGraph vendibile (Gumroad o simile) rivolto a dev/indie hacker generici. Dimostra un pattern di agenti gerarchici (orchestratore + sotto-agenti) pronto per produzione. Il caso d'uso concreto è IoT/Smart Home, ma l'architettura è facilmente riadattabile ad altri domini.

## Stack Tecnologico
- **Framework Agenti**: LangGraph
- **API Server**: FastAPI (con supporto streaming REST/WebSocket)
- **Database**: SQLite per il boilerplate (schema facilmente migrabile a Postgres)
- **Containerizzazione**: Docker & Docker Compose

## Architettura Concettuale

### Cervello (Orchestratore)
- Modello ad alte prestazioni (es. Mistral Large).
- Intervallo di esecuzione ciclico (non reattivo in tempo reale).
- Legge lo storico eventi dal DB ad ogni ciclo.
- Decide aggiustamenti macro basati su pattern storici.
- Ha accesso ai tool di attivazione/regolazione con priorità/ultima parola nel proprio ciclo.

### Sotto-agenti (Per dominio sensoristico)
- Esempi: Clima, Sicurezza, Illuminazione.
- Modello leggero ed economico (es. Mistral Small).
- Ricevono dati mock da sensori (dati simulati/scenari precaricati).
- Decidono in autonomia entro soglie note.
- Escalation al cervello in caso di caso ambiguo o fuori soglia.
- Tool per gestire sensori/attuatori indipendentemente dal cervello.

## Gestione Conflitti (Log Eventi + Reconciliation)
1. **Reading recent events**: Il sotto-agente chiama `get_recent_events(target)` (finestra 5-10 minuti).
2. **Action/Logging**: Se non ci sono conflitti, agisce e scrive `log_event()`.
3. **Escalation**: Se rileva un conflitto (es. azione recente del cervello), fa escalation al cervello.
4. **Reconciliation**: Il cervello nel suo ciclo legge tutti gli eventi recenti ed effettua la reconciliation (conferma, corregge o ignora).

## Human-in-the-Loop (HITL)
Configurabile per nodo tramite parametri di configurazione (`interrupt_before` / `interrupt_after` nativi di LangGraph). Esempio: `hitl_nodes: ["cervello", "sicurezza"]`.

## Modelli Dati & DB

### DB Schema (SQLite)
- `events`: `event_id` (PK), `actor`, `action`, `target`, `value`, `reasoning`, `timestamp`, `escalated` (bool). Indici su `target` e `timestamp`.
- `readings`: `reading_id` (PK), `sensor_id`, `agent_owner`, `value`, `unit`, `timestamp`.

### Shared State (`GraphState`)
- `readings`: Letture correnti dei sensori.
- `recent_events`: Finestra recente di eventi letti dal DB.
- `pending_escalations`: Lista delle escalation pendenti.
- `hitl_required`: Flag/Stato per Human-in-the-loop.
- `config`: Soglie, flag HITL, configurazioni dinamiche.

## Struttura del Progetto

```text
IoTBoilerplate/
├── REQUIREMENTS.md
├── docker-compose.yml
├── requirements.txt
├── app/
│   ├── MAO/
│   │   └── model_access_object.py # Mao (Model Access Object per LLM locale/OpenAI API)
│   ├── graph/
│   │   ├── orchestrator.py      # Cervello (Mistral Large) - routing e reconciliation
│   │   ├── agents/
│   │   │   ├── base_agent.py    # Classe base astratta per sotto-agenti
│   │   │   ├── agent_climate.py # Sotto-agente Clima
│   │   │   ├── agent_security.py# Sotto-agente Sicurezza
│   │   │   └── agent_lighting.py# Sotto-agente Illuminazione
│   │   ├── state.py             # Modelli Pydantic e GraphState condiviso
│   │   └── checkpointer.py      # Persistenza LangGraph (SQLite / Postgres ready)
│   ├── tools/
│   │   ├── tool_wrapper.py      # Decoratore con retry/timeout/logging
│   │   ├── event_log.py         # log_event() e get_recent_events()
│   │   └── sensor_tools.py      # Tool mock per sensori/attuatori IoT
│   ├── db/
│   │   └── database.py          # SQLite setup (tabelle events e readings)
│   ├── api/
│   │   └── main.py              # FastAPI (REST + SSE/Streaming)
│   └── observability/
│       └── tracing.py           # Logging strutturato / tracing
├── examples/
│   └── hierarchical_pattern/
└── docs/
    └── HOW_TO_CUSTOMIZE.md
```

