# 🤖 Agentic IoT Boilerplate (bodyAgent)

**Un corpo digitale per i tuoi progetti di automazione intelligente ed agenti gerarchici.**

Un boilerplate LangGraph pronto per produzione che implementa un pattern di agenti gerarchici ispirato al modo in cui funziona un organismo: un **cervello** (Orchestratore Supremo) che pensa in modo ponderato e concilia i conflitti, e **organi/componenti** (sotto-agenti a N-livelli) che reagiscono in tempo reale, agendo in autonomia quando serve ed escalando ai livelli superiori solo quando la situazione lo richiede.

Il caso d'uso dimostrativo principale è una **smart home**, affiancato da una demo avanzata di **omeostasi medica e fisiologica**, ma l'architettura è pensata per essere trapiantata in qualsiasi dominio — customer support, monitoraggio industriale, gestione flotte, e molto altro.

---

## 🧠 L'idea: un sistema nervoso, non un albero di funzioni

La maggior parte dei sistemi multi-agente che si trovano in giro sono organigrammi rigidi: un capo che decide tutto, e sotto-agenti che eseguono ordini senza mai muovere un dito da soli. È un buon modello per un ufficio burocratico. È un modello pessimo per un corpo che deve sopravvivere nel mondo reale.

Il tuo corpo non funziona così. Se metti la mano su una piastra bollente, **non aspetti che il cervello elabori la situazione** e ti mandi il comando di ritirarla — il midollo spinale reagisce da solo, in millisecondi, tramite un arco riflesso. Il cervello viene informato *dopo*, quando serve capire cosa è successo e magari decidere qualcosa di più strategico (es. "non toccare più quella zona della cucina").

Questo boilerplate replica esattamente questa logica:

- **Il Cervello (Orchestratore Supremo - Livello 0)** — pensa in modo ciclico, guarda la storia recente e decide aggiustamenti strategici o risoluzioni di conflitti.
- **Gli Organi & Componenti (Sotto-agenti N-Livelli)** — specializzati per macro-aree o periferiche. Reagiscono in autonomia entro le loro soglie di competenza, ed **escalano al Padre** quando la situazione è ambigua o conflittuale.
- **Il Sistema Nervoso (Event Log & Audit)** — è il canale attraverso cui ogni agente registra cosa ha fatto e legge le azioni recenti per evitare conflitti o sovrascrizioni.

---

## 🧬 Un organismo ad espandibilità ricorsiva (N-Livelli)

Un vero corpo non si ferma a "cervello + organi". Ogni organo, se lo guardi da vicino, è a sua volta un sistema fatto di sotto-strutture, ognuna specializzata:

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

In pratica: **ogni nodo del grafo può essere, a sua volta, un piccolo cervello per il livello sottostante.** Lo stesso `base_agent.py`, lo stesso `DynamicAgent`, ed il meccanismo di audit log si applicano in modo ricorsivo sia coordinando 2 organi principali che 20 sotto-componenti innestati su N livelli.

---

## 🦾 Anatomia del Sistema

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

### Il Cervello (Orchestratore Supremo)
Possiede i privilegi massimi (priorità `1000.0`). Riceve le escalation dei sotto-agenti quando scattano conflitti non risolvibili ed esegue il check di routine dell'intero sistema (`check_body_status`).

### Gli Organi e Componenti (`DynamicAgent` & `BaseAgent`)
Agiscono in autonomia per il loro target sensore/dispositivo. Se rilevano un conflitto nel DB o un'anomalia severa (es. aritmia cardiaca o sblocco sospetto), generano un'escalation strutturata verso il loro agente Padre.

---

## 🎛️ Human-in-the-Loop (HITL) Dinamico

L'interruzione per approvazione umana può essere inserita **dinamicamente ovunque nel flusso del grafo** tramite il wrapper in `app/graph/builder.py` e gestita 100% via API REST senza riavviare il server.

- **Attivazione Dinamica via API:**
  `POST /hitl/config` consente di specificare nodi (`hitl_nodes`), sensori protetti (`hitl_targets`), azioni critiche (`hitl_actions`) e l'attesa massima in secondi (`max_wait_seconds`).

- **Tre modalità di Resume (`POST /graph/resume`):**

  | `decision` | Comportamento |
  |---|---|
  | `APPROVA` | Scrive `RECONCILED_<action>` nel DB. Nessuna modifica fisica al dispositivo. |
  | `RESPINGI` | Scrive `REJECTED_<action>` nel DB. Il device rimane bloccato fino a TTL o unblock manuale. |
  | `OVERRIDE` | **God Mode Semantico**: il campo `reasoning` in linguaggio naturale viene inviato al MAO con un prompt di Arbitrato Semantico. Il MAO traduce la frase in un array JSON di comandi `{target, action, value}` eseguiti fisicamente via `force_execute_tool`, che bypassa tutti i lock di priorità e traccia ogni azione nel DB con `actor: "Brain_Override"`. |

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


## 🗂️ Struttura del Progetto

```text
IoTBoilerplate/
├── REQUIREMENTS.md
├── docker-compose.yml
├── requirements.txt
├── test_results.json               # Esito in tempo reale della suite di test
├── .env                            # Provider LLM e Prompt configurabili del Brain
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

## 🛠️ Stack Tecnologico

| Componente | Scelta | Perché |
|---|---|---|
| Framework Agenti | **LangGraph** | Grafi stateful con cicli, routing condizionale e checkpointing nativo |
| Provider LLM | **MAO Proxy** | Supporto per OpenRouter, Google AI Studio (Gemini) e LLM Locali (vLLM, LM Studio) |
| API Server | **FastAPI** | Server HTTP/REST asincrono 100% headless con Swagger UI interattiva |
| Database | **SQLite (aiosqlite)** | Zero setup, persistenza audit log eventi, registro agenti e stato |

---

## 🚀 Esempi Dimostrativi Inclusi

1. **Gerarchia Smart Home (`examples/hierarchical_pattern/demo_hierarchy.py`):**
   ```bash
   python3 examples/hierarchical_pattern/demo_hierarchy.py
   ```
   Dimostra l'escalation ricorsiva da un sotto-componente serratura (Livello 2) all'organo sicurezza (Livello 1) fino al Cervello (Livello 0).

2. **Omeostasi Medica & Patologie (`examples/medical_homeostasis/demo_medical_homeostasis.py`):**
   ```bash
   python3 examples/medical_homeostasis/demo_medical_homeostasis.py
   ```
   Simula l'insorgenza di patologie cliniche (Tachicardia 160 BPM, Ipossia 82% SpO2) e l'intervento automatico degli Agenti Fisiologici per riportare l'organismo in omeostasi.

---

## 🧪 Esecuzione della Suite di Test

Per eseguire tutti i 38 test automatizzati e riaggiornare `test_results.json`:
```bash
python3 tests/test_all.py
```

---

## 📄 Licenza

MIT License. Libero per utilizzi commerciali e personali.