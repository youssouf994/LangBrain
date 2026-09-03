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
Configurabile per nodo, dispositivo o azione tramite il manager dinamico `hitl_config.py` o via API REST (`POST /hitl/config`).
I cicli del grafo inviati tramite `POST /graph/run` accettano un `thread_id` univoco per legare senza discontinuità lo stato di avanzamento (`GET /graph/state?thread_id=...`) e l'eventuale decisione dell'operatore umano (`POST /graph/resume`).

### Tre modalità di decisione HITL (`POST /graph/resume`)
| `decision` | Comportamento |
|---|---|
| `APPROVA` | Il wrapper scrive `RECONCILED_<action>` nel DB e termina il ciclo. |
| `RESPINGI` | Il wrapper scrive `REJECTED_<action>` nel DB, blocca il device, termina il ciclo. |
| `OVERRIDE` | **God Mode Semantico**: il campo `reasoning` viene inviato al MAO con il prompt di Arbitrato Semantico. Il MAO traduce la frase in linguaggio naturale in un array JSON di comandi `{target, action, value}`. Ogni comando viene eseguito via `force_execute_tool` che bypassa deliberatamente i lock di priorità (`check_priority_lock`) e registra l'azione con `actor: "Brain_Override"` nel DB. |

### Tool On-Demand
I dispositivi non pre-registrati vengono creati automaticamente come `IoTDeviceTool` con stato `OFF` al primo accesso tramite `GET/POST /tools/{device_id}` o durante l'esecuzione di un Override semantico.

## Modelli Dati & DB

### DB Schema (SQLite)
- `events`: `event_id` (PK), `actor`, `action`, `target`, `old_value`, `new_value`, `reasoning`, `timestamp`, `escalated` (bool). Indici su `target` e `timestamp`.
- `readings`: `reading_id` (PK), `sensor_id`, `agent_owner`, `value`, `unit`, `timestamp`.
- `agents_registry`: `name` (PK), `level`, `parent_agent_name`, `managed_targets`, `sub_agent_names`, `system_prompt_template`, `user_prompt_template`, `conflict_window_minutes`, `priority_weight`.

### Shared State (`GraphState`)
- `readings`: Letture correnti dei sensori.
- `recent_events`: Finestra recente di eventi letti dal DB.
- `pending_escalations`: Lista delle escalation pendenti.
- `hitl_required`: Flag/Stato per Human-in-the-loop.
- `next_agent`: Nodo di destinazione nel grafo LangGraph (`"brain"`, `"organ_security"`, `"END"`, etc.).
- `config`: Soglie, flag HITL, configurazioni dinamiche.

## Struttura del Progetto

```text
IoTBoilerplate/
├── REQUIREMENTS.md
├── docker-compose.yml
├── requirements.txt
├── test_results.json               # Esito in tempo reale della suite di test
├── .env                            # Provider LLM e Prompt configurabili del Brain
├── app/
│   ├── MAO/
│   │   └── model_access_object.py  # Mao (Model Access Object per LLM locale/OpenAI API)
│   ├── agents/
│   │   ├── base_agent.py           # Classe base astratta per sotto-agenti
│   │   ├── agent_climate.py        # Sotto-agente Clima nativo
│   │   ├── dynamic_agent.py        # Agente Dinamico Livello N configurabile
│   │   ├── agent_registry.py       # Registro gerarchico salvato su DB SQLite
│   │   └── medical_agents.py       # Agenti Fisiologici (Cardiovascolare, Respiratorio)
│   ├── graph/
│   │   ├── orchestrator.py         # Cervello (BrainAgent - Livello 0)
│   │   ├── builder.py              # Builder del grafo LangGraph con wrapper HITL
│   │   ├── hitl_config.py          # Manager della configurazione dinamica HITL
│   │   └── state.py                # GraphState condiviso
│   ├── tools/
│   │   ├── baseTool.py             # Classe base astratta per i tool
│   │   ├── tool_wrapper.py         # execute_tool_safely (controllo priorità) + force_execute_tool (God Mode Override)
│   │   ├── event_log.py            # log_event(), mark_resolved(), unblock_target()
│   │   ├── sensor_tools.py         # Tool mock per sensori/attuatori IoT (creazione on-demand singleton)
│   │   └── medical_tools.py        # Tool medici (Pacemaker, SpO2, Normalizzatore)
│   ├── db/
│   │   └── database.py             # SQLite setup (tabelle events, readings, agents_registry)
│   ├── api/
│   │   └── main.py                 # FastAPI (REST + RunCycleRequest con thread_id + HITL config)
│   └── observability/
│       └── tracing.py              # Logging strutturato / tracing
├── examples/
│   ├── hierarchical_pattern/
│   │   └── demo_hierarchy.py       # Demo Gerarchia Smart Home N-Livelli
│   └── medical_homeostasis/
│       └── demo_medical_homeostasis.py # Demo Omeostasi Fisiologica Medica
└── docs/
    └── HOW_TO_CUSTOMIZE.md
```

