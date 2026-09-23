🇮🇹 Italiano | 🇬🇧 [English](README.en.md)

# 🤖 LangBrain

> [!WARNING]
> **LangBrain è un boilerplate funzionante, ma non è pronto per la produzione:** un solo worker, dispositivi simulati con stato in memoria, nessuna observability. I limiti noti e la roadmap sono in [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md).

**Un corpo digitale per i tuoi progetti di automazione intelligente ed agenti gerarchici.**

Un boilerplate LangGraph sperimentale che implementa un pattern di agenti gerarchici ispirato al modo in cui funziona un organismo: un **cervello** (Orchestratore Supremo) che pensa in modo ponderato e concilia i conflitti, e **organi/componenti** (sotto-agenti a N-livelli) che reagiscono in tempo reale, agendo in autonomia quando serve ed escalando ai livelli superiori solo quando la situazione lo richiede.

Il caso d'uso dimostrativo principale è una **smart home**, affiancato da una demo avanzata di **omeostasi medica e fisiologica**, ma l'architettura è pensata per essere trapiantata in qualsiasi dominio — customer support, monitoraggio industriale, gestione flotte, e molto altro.

---

## 🚀 Avvio rapido

Serve Python 3.12 o superiore.

```bash
python -m venv venv && source venv/bin/activate    # su Windows: .\venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
cp .env.example .env                               # inserisci almeno la chiave di un provider LLM
python examples/avvia_demo.py                      # scenario di prova + pagina "grafo in azione"
```

La demo crea un database dedicato (`demo.db`, i tuoi dati non vengono toccati), avvia il server e apre `http://127.0.0.1:8765/demo`. Premi **Esegui un ciclo**: i nodi della gerarchia si illuminano uno dopo l'altro mentre gli agenti (modelli reali) ragionano, gli eventi e i dispositivi si aggiornano e, quando il Brain chiede l'approvazione per la porta, decidi tu con **Approva** o **Respingi**.

Per il server vero: `python -m uvicorn app.api.main:app` oppure `docker compose up`. Le scelte dell'utente (dispositivi e valori ammessi, provider e modelli, HITL, timer) stanno in [`configurazione.toml`](configurazione.toml), i segreti nel `.env`. La guida completa è in [`docs/HOW_TO_CUSTOMIZE.md`](docs/HOW_TO_CUSTOMIZE.md); sicurezza e contributi in [`SECURITY.md`](SECURITY.md) e [`CONTRIBUTING.md`](CONTRIBUTING.md).

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

- **Dove si applica (scelta dell'utente):** in `configurazione.toml`, `[hitl] livello` sceglie tra `nodi` (il wrapper ferma i nodi che indichi), `brain` (il Brain chiede l'approvazione per i target critici) ed `entrambi`. Le pause di emergenza (modello LLM non utilizzabile, guasto di un dispositivo non risolto) restano attive con qualsiasi livello.
- **Timer:** con `[hitl] timer_attivo = 1` l'API espone i secondi rimanenti e, alla scadenza, decide il sistema (il Brain con il suo modello), respinge oppure lascia il grafo in pausa, secondo `azione_alla_scadenza`.
- **Attivazione Dinamica via API:**
  `POST /hitl/config` consente di specificare nodi (`hitl_nodes`), sensori protetti (`hitl_targets`), azioni critiche (`hitl_actions`) e l'attesa massima in secondi (`max_wait_seconds`).

- **Tre modalità di Resume (`POST /graph/resume`):**

  | `decision` | Comportamento |
  |---|---|
  | `APPROVA` | Il Brain applica al dispositivo l'azione proposta (se è un comando ammesso da `configurazione.toml`) e scrive `RECONCILED_<action>` nel DB. |
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
LangBrain/
├── README.md / README.en.md
├── REQUIREMENTS.md / REQUIREMENTS.en.md
├── SECURITY.md                     # Come segnalare vulnerabilità e cosa aspettarsi dalla sicurezza
├── CONTRIBUTING.md                 # Come contribuire
├── LICENCE
├── Dockerfile
├── docker-compose.yml
├── requirements.txt                # Dipendenze dirette, versioni esatte
├── requirements.lock               # Elenco completo fissato (usato dal Dockerfile)
├── requirements-dev.txt            # In più pytest
├── .env.example                    # Template dei segreti (chiavi dei provider e dell'API); il tuo .env resta fuori da Git
├── configurazione.toml             # Scelte dell'utente: dispositivi e valori ammessi, provider LLM, HITL e timer
├── run_loop.py                     # Loop event-driven con produttore di eventi dei sensori
├── .github/workflows/ci.yml        # CI: test su Python 3.12/3.14, scansione segreti e dipendenze
├── app/
│   ├── MAO/
│   │   └── model_access_object.py  # Model Access Object (OpenRouter, Google AI Studio, Mistral, LLM locale)
│   ├── agents/
│   │   ├── base_agent.py           # DNA comune di ogni agente (applica stato, priorità, escalation)
│   │   ├── agent_climate.py        # Agente Clima nativo
│   │   ├── dynamic_agent.py        # Agente dinamico configurabile a runtime (Livelli 1..N)
│   │   ├── agent_registry.py       # Registro gerarchico su SQLite
│   │   └── medical_agents.py       # Agenti fisiologici (Cardiovascolare, Respiratorio)
│   ├── core/
│   │   ├── configurazione.py       # Lettura e validazione di configurazione.toml
│   │   ├── constants.py            # Flag di controllo e TTL
│   │   ├── errori_llm.py           # Errori del modello classificati (token, chiave, limiti) e oscuramento segreti
│   │   ├── modelli_agenti.py       # Modello LLM per singolo agente, con ereditarietà dal padre
│   │   ├── priorita.py             # Regole di priorità dei blocchi
│   │   ├── risultati.py            # Esito strutturato di lettura/attuazione dei tool
│   │   └── ruoli.py                # Ruoli di chi chiama l'API e matrice dei permessi
│   ├── graph/
│   │   ├── orchestrator.py         # Cervello (BrainAgent - Livello 0)
│   │   ├── builder.py              # Builder del grafo LangGraph con wrapper HITL
│   │   ├── hitl_config.py          # Configurazione dinamica HITL
│   │   ├── timer_hitl.py           # Timer di attesa dell'operatore e azione alla scadenza
│   │   └── state.py                # GraphState condiviso
│   ├── tools/
│   │   ├── baseTool.py             # Classe base astratta per i tool
│   │   ├── sensor_tools.py         # Tool smart home simulati e registry condiviso (registra_tool)
│   │   ├── medical_tools.py        # Tool medici (Pacemaker, Ventilatore SpO2, Normalizzatore)
│   │   ├── event_log.py            # Sistema nervoso: audit log degli eventi e sblocchi
│   │   └── tool_wrapper.py         # Attuazione con controllo di priorità e override
│   ├── db/
│   │   ├── database.py             # Schema SQLite (events, readings, ...)
│   │   └── scenario.py             # Scenari di dati riproducibili
│   ├── api/
│   │   ├── main.py                 # API REST FastAPI (grafo, streaming, HITL, agenti, tool, eventi)
│   │   └── energia.py              # Rotte /energia/... della simulazione energetica
│   ├── simulazione/                # Rete elettrica e gas simulata (motore, scenari, esecuzione in background)
│   ├── static/
│   │   ├── demo_grafo.html         # Pagina "grafo in azione", servita su GET /demo
│   │   └── energia.html            # Pagina della rete energetica simulata, servita su GET /energia
│   └── checkpointer.py             # Checkpointer LangGraph persistente su SQLite
├── examples/
│   ├── avvia_demo.py               # Server + scenario di prova + pagina web del grafo
│   ├── crea_scenario.py            # Azzera un database e crea uno scenario di prova
│   ├── hierarchical_pattern/
│   │   └── demo_hierarchy.py       # Gerarchia smart home N-livelli da codice
│   └── medical_homeostasis/
│       └── demo_medical_homeostasis.py # Omeostasi fisiologica e risoluzione di patologie
├── scripts/
│   ├── scansione_sicurezza.sh      # Scansione di segreti e dipendenze
│   └── stress_energia.py           # Prova di carico della simulazione energetica
├── tests/                          # Suite pytest (database temporanei, nessun LLM reale)
└── docs/
    ├── HOW_TO_CUSTOMIZE.md         # Guida alla personalizzazione e mappatura API
    ├── PROJECT_STATUS.md           # Stato del progetto, limiti noti e roadmap
    ├── SIMULAZIONE_ENERGIA.md      # Simulazione della rete energetica: modello, pagina, API, prova di carico
    ├── API_SMOKE_TEST.md           # Smoke test dell'API con curl (Bash)
    └── API_SMOKE_TEST_WINDOWS.ps1  # Smoke test dell'API per PowerShell
```

---

## 🛠️ Stack Tecnologico

| Componente | Scelta | Perché |
|---|---|---|
| Framework Agenti | **LangGraph** | Grafi stateful con cicli, routing condizionale e checkpointing nativo |
| Provider LLM | **MAO Proxy** | OpenRouter, Google AI Studio (Gemini), Mistral e LLM locali (vLLM, LM Studio), con modello per singolo agente |
| API Server | **FastAPI** | Server HTTP/REST asincrono 100% headless con Swagger UI interattiva |
| Database | **SQLite (aiosqlite)** | Zero setup, persistenza audit log eventi, registro agenti e stato |

---

## ⚙️ Configurazione MAO e contratti di dominio

Le chiamate LLM sono asincrone. Il timeout HTTP del MAO è configurato da `MAO_TIMEOUT_SECONDS` e vale **40 secondi** se la variabile non è presente o non è valida. Per un modello su un'altra macchina della LAN, imposta sia `LOCAL_MODEL_BASE_URL` sia `LOCAL_MODEL_DOCKER_BASE_URL` sull'endpoint OpenAI-compatible raggiungibile (per esempio `http://192.168.1.50:8080/v1`) e usa in `LOCAL_MODEL` l'ID restituito da `GET /v1/models`.

LangBrain tratta `action`, `old_value` e `new_value` come dati estensibili. Il boilerplate non può conoscere gli stati fisici validi di ogni dominio: chi aggiunge un tool o agente deve implementare e testare la propria validazione/mappatura (per esempio `LOCKED`/`UNLOCKED` per una serratura). I flag interni come `REJECTED` e `BLOCKED` vengono segnalati dall'health check, ma non trasformati automaticamente in uno stato fisico.

Chi comanda cosa: le chiavi dell'API hanno tre ruoli (`tirocinante`, `medico_di_guardia`, `primario`) e nessun agente può attuare un dispositivo o un valore non elencato in `configurazione.toml`. Dettagli in [`docs/HOW_TO_CUSTOMIZE.md`](docs/HOW_TO_CUSTOMIZE.md) e [`SECURITY.md`](SECURITY.md).

---

## 🎬 Esempi Dimostrativi Inclusi

1. **Grafo in azione (`examples/avvia_demo.py`):** server con uno scenario di prova e la pagina `/demo` che mostra in tempo reale i nodi eseguiti, gli eventi, i dispositivi e la richiesta di approvazione dell'operatore.
   ```bash
   python examples/avvia_demo.py [--scenario conflitto_porta|base|finestra_aperta] [--modello Brain=mistral:ministral-8b-latest]
   ```
2. **Scenari di dati (`examples/crea_scenario.py`):** azzera un database e ci crea gerarchia, storico ed eventuale conflitto, per provare l'API a mano (fa prima un backup).
3. **Gerarchia Smart Home (`examples/hierarchical_pattern/demo_hierarchy.py`):** crea da codice la gerarchia Brain → Organi → Componenti e mostra l'escalation ricorsiva dal componente serratura al Brain, che chiede l'approvazione dell'operatore.
4. **Omeostasi medica (`examples/medical_homeostasis/demo_medical_homeostasis.py`):** tachicardia (160 BPM) e ipossia (82% SpO2): l'agente respiratorio riporta la saturazione a 95%; la tachicardia severa sale al Brain e, se la respinge, l'operatore sblocca e il protocollo ripristina 100 BPM.

5. **Rete energetica simulata (`/energia`):** elettricità, gas e accumuli su dati plausibili dell'Italia (o su una rete generata grande per le prove di carico), con durata fino ad anni e velocità regolabile, guasti, crisi del gas e ondate di calore. Sopra la rete lavora una gerarchia di agenti (Brain, organi elettrico e gas, quattro componenti) svegliata dal sistema nervoso quando compaiono anomalie: gli agenti azionano le leve della rete e chiedono all'operatore le misure che fermano l'industria. Dettagli in [`docs/SIMULAZIONE_ENERGIA.md`](docs/SIMULAZIONE_ENERGIA.md).
   ```bash
   python -m uvicorn app.api.main:app --port 8765     # poi apri http://127.0.0.1:8765/energia
   python scripts/stress_energia.py --anni 10          # prova di carico senza server
   ```

Le demo 3 e 4 usano un proprio database (`demo_gerarchia.db`, `demo_medica.db`) e il provider LLM predefinito del `.env`.

---

## 🧪 Test e Controlli

```bash
python -m pytest tests                 # intera suite: database temporanei, nessuna chiamata a LLM reali
bash scripts/scansione_sicurezza.sh    # segreti e dipendenze vulnerabili (servono gitleaks e pip-audit)
```

Gli stessi controlli girano in CI a ogni push (`.github/workflows/ci.yml`). Per verificare a mano un server in esecuzione: [smoke test Bash](docs/API_SMOKE_TEST.md) o [PowerShell per Windows](docs/API_SMOKE_TEST_WINDOWS.ps1) (attenzione: eseguono un reset del database del server, usali su un'istanza di prova, per esempio quella di `examples/avvia_demo.py`).

---

## 📄 Licenza

Polyform Small Business License 1.0.0, libero per uso personale ed aziendale fino a soglia di fatturato.
