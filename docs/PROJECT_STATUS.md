# Stato del progetto LangBrain

Audit statico iniziale eseguito il 4 settembre 2026 e aggiornato dopo i fix e gli smoke test Docker dello stesso giorno. La suite di regressione viene ora eseguita in un container Python isolato; le osservazioni storiche corrette sono marcate come tali nelle sezioni seguenti.

> **Aggiornamento corrente:** corretti caricamento eventi DB, routing case-insensitive, propagazione gerarchica, fallback `next_agent`, client LLM sincrono, gestione errori `/llm/invoke`, timeout MAO e smoke test UTF-8/HITL Override. Il timeout MAO è configurabile con `MAO_TIMEOUT_SECONDS` (default 40 secondi). Docker raggiunge modelli OpenAI-compatible su host o LAN tramite `LOCAL_MODEL_DOCKER_BASE_URL`. La normalizzazione degli stati fisici resta intenzionalmente un contratto a carico dello sviluppatore del dominio.

## Sintesi esecutiva

La repository contiene un prototipo sostanziale: agenti, grafo LangGraph, audit log SQLite, API FastAPI, HITL e demo sono implementati. Non è però “pronto per produzione” come dichiarato nel README. I blocchi principali sono il routing gerarchico non affidabile, la lettura del DB bypassata nel grafo compilato, il checkpointer solo in memoria, Docker e observability vuoti, la mancata differenziazione dei modelli per livello e una suite di test custom non riproducibile nell'ambiente corrente e con diversi falsi positivi/coperture mancanti.

## 1. Struttura effettiva rispetto alla struttura dichiarata

### Struttura reale rilevante

```text
LangBrain/
├── .gitignore
├── LICENCE
├── README.md
├── REQUIREMENTS.md
├── Dockerfile / docker-compose.yml
├── .dockerignore / .env.example
├── requirements.txt
├── run_loop.py
├── app/
│   ├── checkpointer.py            # vuoto (0 byte)
│   ├── MAO/model_access_object.py
│   ├── agents/{base_agent,agent_climate,dynamic_agent,agent_registry,medical_agents}.py
│   ├── api/main.py
│   ├── core/{__init__,constants}.py
│   ├── db/database.py
│   ├── graph/{builder,hitl_config,orchestrator,state}.py
│   ├── observability/tracing.py   # vuoto (0 byte)
│   └── tools/{baseTool,event_log,medical_tools,sensor_tools,tool_wrapper}.py
├── docs/HOW_TO_CUSTOMIZE.md
├── examples/{hierarchical_pattern,medical_homeostasis}/...
├── tests/{test_all,test_blocking_regressions}.py
├── smoke_test_full.ps1
└── smoke_test_full_v2.ps1
```

Non risultano cartelle vuote, esclusa la normale struttura interna di `.git` che non fa parte del prodotto.

### Discrepanze

| Voce | Dichiarato | Effettivo | Valutazione |
|---|---|---|---|
| Radice | `LangBrain/` | repository/cartella `LangBrain/` | Naming allineato dopo la rinomina del progetto. |
| `.env` | Presente nell'albero di README e REQUIREMENTS | Presente localmente e correttamente ignorato da Git | `.env.example` documenta anche timeout MAO e URL locale/Docker. |
| `test_results.json` | Presente e aggiornato dalla suite | Assente e ignorato da Git | Il README promette un artefatto che non è versionato. |
| `docker-compose.yml` | Containerizzazione pronta | Servizio operativo con build, health check, volume SQLite ed env | Funzionante per sviluppo/demo; non production-ready. |
| `app/checkpointer.py` | Checkpointer persistente | File presente ma vuoto; il grafo usa `MemorySaver` direttamente in `builder.py` | Dichiarazione fuorviante. |
| `app/observability/tracing.py` | Tracing/logging strutturato | File vuoto | Placeholder. |
| `run_loop.py` | Non mostrato negli alberi dichiarati | Presente e contiene il loop event-driven | File applicativo importante non documentato nella struttura. |
| `app/core/` | Non mostrato negli alberi dichiarati | Presente con costanti TTL/control flag | Modulo reale non documentato. |
| `tests/test_all.py` | Nel README; non nell'albero di REQUIREMENTS | Presente | REQUIREMENTS incompleto. |
| `LICENCE`, `.gitignore` | Non mostrati | Presenti | File extra legittimi, ma l'albero non è esaustivo. |
| TODO/roadmap | Implicitamente richiesto dal task se presente | Nessun `TODO.md` o equivalente | Non è stata creata una traduzione TODO. |

## 2. Stato funzionale per modulo

Legenda richiesta: **Completo e testato** / **Funzionante ma non testato a fondo** / **Placeholder/stub** / **Mancante**.

| Componente | Stato | Evidenza dal codice reale |
|---|---|---|
| Orchestratore `BrainAgent` | **Funzionante ma non testato a fondo** | Implementa readout, reconciliation, arbitrato LLM, override e health check. I test mockano il modello e chiamano prevalentemente `process()` direttamente; non coprono il ciclo reale API + DB + checkpoint. L'approvazione usa spesso `action` come valore fisico senza normalizzazione. |
| Base agent `BaseAgent` | **Funzionante ma non testato a fondo** | Centralizza lettura eventi, lock, applicazione e log. È testato su idempotenza/conflitto/escalation, ma il grafo compilato ne bypassa `__call__()`. `apply_status()` può dichiarare successo anche senza tool o dopo errori di attuazione/log. |
| Sotto-agente clima `ClimateAgent` | **Funzionante ma non testato a fondo** | Ha test mockati per ACTION/NONE/blocco/escalation. È specializzato e hardcoded su `ac_living_room` e `22.5°C`; l'integrazione DB reale nel grafo non è coperta. |
| Sotto-agente dinamico `DynamicAgent` | **Funzionante ma non testato a fondo** | Registro e istanziazione sono testati, ma non il routing end-to-end. Le azioni sono ridotte a `TURN_ON`/`ON`, viene gestito solo il primo target e la delega ai figli avviene solo se non ci sono target diretti. |
| Registro agenti `AgentRegistry` | **Funzionante ma non testato a fondo** | CRUD, istanziazione e albero hanno un test diretto. Non valida cicli, parent inesistenti, livelli incoerenti o nomi nodo; non persiste `user_prompt_template` benché richiesto/documentato. |
| Agente cardiovascolare | **Funzionante ma non testato a fondo** | Logica deterministica e test diretto presenti. L'escalation restituisce `next_agent: "Brain"`, che non corrisponde al nodo `brain`, e la demo ripristina manualmente il pacemaker fuori dal flusso di reconciliation. |
| Agente respiratorio | **Funzionante ma non testato a fondo** | Normalizzazione e ripristino sono testati direttamente; nessun test del grafo/API o delle policy HITL/priorità. |
| Event log / audit SQLite | **Funzionante ma non testato a fondo** | CRUD eventi, risoluzione, unblock e TTL sono implementati con query parametrizzate. Mancano test di concorrenza, transazioni multi-step e ordinamento a timestamp uguali; alcune eccezioni vengono assorbite. |
| Tool IoT in memoria | **Funzionante ma non testato a fondo** | Singleton e get/set hanno test. Lo stato non è persistente, non è protetto per accessi concorrenti e i tool on-demand possono sparire dalla mappa API dopo una ricompilazione. |
| Tool medici / normalizzatore | **Completo e testato** | Per il limitato scopo mock dichiarato, get/set e normalizzazione deterministica sono coperti da test diretti. Non costituiscono integrazione con hardware clinico. |
| `execute_tool_safely` | **Funzionante ma non testato a fondo** | Implementato, ma non usato dal resto dell'applicazione e senza test dedicato; il parametro `actor_priority` non viene usato. |
| `force_execute_tool` | **Funzionante ma non testato a fondo** | Ha un test diretto e viene usato dall'override. `mark_resolved()` chiude solo escalation, non tutti i lock che il commento afferma di annullare. |
| MAO / accesso modelli | **Funzionante ma non testato a fondo** | Usa `AsyncOpenAI`, timeout `MAO_TIMEOUT_SECONDS` (40 secondi di default), esclusione provider senza credenziali e fallback configurabile. Testato con mock e con un modello OpenAI-compatible su LAN; mancano circuit breaker e test di carico. |
| API FastAPI | **Funzionante ma non testato a fondo** | Gli endpoint principali e lo smoke test sono operativi; `/llm/invoke` converte i fallimenti provider in `503` controllato. Mancano autenticazione, concorrenza, streaming e WebSocket. |
| HITL | **Funzionante ma non testato a fondo** | Config manager, wrapper, interrupt/resume e Override sono coperti da regressioni e smoke test. `max_wait_seconds` resta metadata e non implementa timeout/fallback automatico. Esistono due livelli HITL parzialmente sovrapposti (wrapper e Brain). |
| Checkpointer | **Placeholder/stub** | `app/checkpointer.py` è vuoto. `MemorySaver` offre resume solo in memoria e viene ricreato a ogni ricompilazione/restart. |
| Observability | **Placeholder/stub** | `app/observability/tracing.py` è vuoto. Restano log standard non strutturati e nessuna metrica/trace/correlation ID. |
| Streaming REST/WebSocket richiesto | **Mancante** | Nessun endpoint WebSocket né risposta streaming è implementato. |
| Containerizzazione operativa | **Funzionante ma non testato a fondo** | `Dockerfile` e Compose sono operativi, con health check, volume SQLite, singolo worker e configurazione per modelli su host/LAN. |

### Stato dei test

`tests/test_all.py` resta uno script custom eseguito all'import. È stato aggiunto `tests/test_blocking_regressions.py`, una suite `unittest` asincrona eseguita con successo in Docker, che copre routing, gerarchia, MAO asincrono, timeout configurabile, fallback provider, risposta API 503 e health check sui flag di controllo. Restano da isolare e standardizzare i test legacy.

Criticità della suite:

- su una checkout pulita, `db.init_db_memory` inizializza una connessione `:memory:` separata e non crea le tabelle nel `DB_PATH` poi usato dai test dell'event log;
- il test `mao.init_providers` si aspetta `openrouter`, mentre il codice usa `google_studio` come default in assenza di env;
- `mao.call_model_live` viene registrato come superato anche se la chiamata fallisce per qualunque eccezione;
- i test API non avviano realmente il lifespan FastAPI;
- non sono testati resume/checkpoint reali, timeout HITL, routing ricorsivo, concorrenza SQLite, persistenza, Docker, observability e streaming;
- più test condividono singleton, configurazione globale e DB, quindi ordine e stato preesistente possono influire sui risultati.

## 3. Bug noti e comportamenti sospetti

### Bloccanti o ad alta priorità

1. **Corretto — lettura eventi DB nel grafo.** Il wrapper passa dagli agenti callable e quindi da `BaseAgent.__call__()`; la reconciliation DB è coperta dallo smoke test.
2. **Corretto — routing case-insensitive e `next_agent`.** Il router risolve i nomi registrati senza dipendere dal casing; agenti assenti e ritorni a ciclo concluso diventano `END` anziché conservare fallback inesistenti.
3. **Parzialmente corretto — coordinazione N-livelli.** `DynamicAgent` visita ciascun figlio una volta per ciclo e propaga le escalation verso il padre. Restano da validare cicli, parent inesistenti e topologie incoerenti.
4. **Priorità dichiarata ma non applicata.** `priority_weight` viene memorizzato e loggato, ma i lock non confrontano la priorità dell'attore. Anche `actor_priority` in `execute_tool_safely()` è inutilizzato. Qualunque evento altrui con pattern bloccante può prevalere indipendentemente dal peso.
5. **Esito di attuazione non affidabile.** `BaseAgent.apply_status()` restituisce `True` quando il tool non esiste, quando `set_tool_value()` solleva un'eccezione o quando il log DB fallisce. Può quindi registrare o comunicare un'azione come applicata senza conferma fisica.
6. **Checkpointer non persistente.** `MemorySaver` perde thread e interrupt a restart. Ogni creazione/eliminazione agente ricompila il grafo e sostituisce il saver, perdendo anche lo stato in-flight nello stesso processo.
7. **Race sulla ricompilazione globale.** `_graph` e `_shared_tools` vengono sostituiti senza lock mentre richieste concorrenti possono usarli. In deployment multi-worker, registry tool, HITL e checkpoint in memoria divergerebbero tra processi.
8. **Override pericolosamente permissivo.** Un output MAO non valido produce comunque un comando fallback ed esecuzione fisica; target non registrati vengono creati on-demand. Mancano allowlist, validazione per-device, autenticazione/autorizzazione e conferma ulteriore. Gli endpoint diretti `/tools`, `/system/reset` e `/graph/resume` sono anch'essi privi di auth.

### Incompletezze e incoerenze funzionali

- Il requisito di modelli diversi per livello non è implementato: tutti gli agenti costruiscono lo stesso `Mao` e usano il provider/modello globale; il registry non contiene campi provider/modello.
- FastAPI promette streaming REST/WebSocket nei requisiti, ma offre solo request/response REST.
- `max_wait_seconds` HITL viene incluso nel payload, senza timer, scadenza o fallback automatico.
- Il Brain ha una seconda logica `interrupt()` oltre al wrapper. Le policy differiscono: il Brain applica target HITL predefiniti (`alarm_system`, `front_door_lock`) anche se il manager globale è vuoto.
- Nel pre-interrupt del Brain, APPROVA/RESPINGI opera su tutti gli eventi recenti globali non risolti, non solo sull'escalation/target che ha causato l'interrupt. APPROVA termina il ciclo senza eseguire il nodo.
- Nel post-interrupt, il rifiuto svuota l'escalation ma non scrive necessariamente il flag `REJECTED`; l'azione autonoma può essere già stata eseguita prima dell'interrupt perché il controllo è post-process.
- Le decisioni HITL non sono validate con un enum. Nel wrapper, una stringa sconosciuta cade nel ramo di approvazione; nella logica interna del Brain tende invece al rifiuto.
- La reconciliation del Brain applica `new_value=action`. Azioni simboliche come `TURN_ON` possono diventare stato fisico letterale; la normalizzazione esiste solo nel percorso semantic override.
- `DynamicAgent` considera solo `managed_targets[0]` e traduce ogni ACTION in `ON`, indipendentemente da target e risposta del modello.
- ~~Il Brain instradava verso `agent_climate` anche quando non registrato.~~ Corretto: riceve dal builder l'elenco dei nodi effettivi e termina con `END` quando il ciclo è completo o il target non esiste.
- `GraphState.reduce_readings` accumula senza applicare `readings_window_hours`; lo stato può crescere senza limite e duplicarsi quando viene reinviato sullo stesso thread.
- La tabella `readings` viene creata ma non viene mai popolata dal codice applicativo.
- `AgentRegistry` non crea/persiste la colonna `user_prompt_template`, in contrasto con REQUIREMENTS e guida. `CreateSubAgentSchema` esiste ma l'endpoint accetta solo una stringa JSON dentro `CreateSubAgentRequest`, non il dizionario promesso dalla docstring.
- `Database.init_db()` elimina e reinserisce un conflitto demo a ogni startup. È una mutazione sorprendente per un DB dichiarato persistente e non adatta a produzione.
- Lo stato dei tool è solo memoria di processo e non viene riconciliato con il DB al restart.
- `force_execute_tool()` afferma di annullare i blocchi ma `mark_resolved()` modifica solo `ESCALATION_PROPOSED`, non `REJECTED_*` o `FORCE_SHUTDOWN`.
- Errori DB in `mark_resolved`, `unblock_target` e TTL sono loggati ma non propagati; l'API può rispondere con successo anche quando l'operazione è fallita.
- ~~Le chiamate LLM sincrone (`OpenAI` client) vengono effettuate dentro endpoint e nodi async, bloccando l'event loop.~~ **Corretto dopo l'audit:** il MAO usa `AsyncOpenAI` e i chiamanti attendono le coroutine in modo nativo.
- Le query SQLite non impostano WAL/busy timeout e alcune operazioni logiche multi-step non sono protette come unità atomiche; sotto concorrenza sono possibili lock o stati parziali.
- L'ordinamento eventi usa timestamp SQLite con precisione al secondo; eventi contemporanei possono avere un ordine non deterministico.
- La demo gerarchica semina `MANUAL_UNLOCK_OVERRIDE`, ma `DynamicAgent` riconosce come conflitto soltanto `FORCE_SHUTDOWN`, `SECURITY_LOCK` e `REJECTED_*`; la demo non prova in modo deterministico l'escalation dichiarata.
- La demo medica ripristina manualmente il pacemaker con `cardio_agent.apply_status()` dopo l'escalation, quindi non dimostra che il Brain chiuda realmente il percorso end-to-end.

## 4. Dipendenze e configurazione

### `requirements.txt`

| Dipendenza | Vincolo | Nota |
|---|---|---|
| `langgraph` | `>=0.2.0` | Non pinnata; range aperto a breaking change. |
| `langchain-core` | `>=0.3.0` | Non pinnata. |
| `langchain-mistralai` | `>=0.2.0` | Non pinnata e non importata dal codice. |
| `fastapi` | `>=0.110.0` | Non pinnata. |
| `uvicorn[standard]` | `>=0.28.0` | Non pinnata. |
| `pydantic` | `>=2.6.0` | Non pinnata. |
| `aiosqlite` | `>=0.20.0` | Non pinnata. |
| `python-dotenv` | `>=1.0.0` | Non pinnata. |
| `openai` | nessun vincolo | Completamente non pinnata. |

`httpx>=0.27.0` è ora dichiarata direttamente. Non esistono `package.json`, lockfile Python, `pyproject.toml` o separazione dipendenze dev/test; i vincoli restano range aperti.

### Variabili d'ambiente attese dal codice

- Database/runtime: `DB_PATH`, `FLAG_TTL_MINUTES`, `MAO_TIMEOUT_SECONDS` (default `40`).
- Provider: `DEFAULT_PROVIDER`.
- Google: `GOOGLE_BASE_URL`, `GEMINI_API_KEY` o `GOOGLE_API_KEY`, `GEMINI_MODEL`.
- Locale: `LOCAL_MODEL_BASE_URL`, `LOCAL_MODEL_DOCKER_BASE_URL`, `LOCAL_API_KEY`, `LOCAL_MODEL`.
- OpenRouter: `OPENROUTER_BASE_URL`, `OPENROUTER_API_KEY`, `OPENROUTER_REFERER`, `OPENROUTER_APP_TITLE`, `OPENROUTER_MODEL`.
- Prompt Brain: `BRAIN_SYSTEM_PROMPT`, `BRAIN_USER_PROMPT_TEMPLATE`.

`.env.example` documenta queste variabili. Priorità, target, valori fisici dei device, frequenze e intervalli del loop restano in parte hardcoded o demandati all'implementazione del dominio.

### Coerenza Docker Compose

`Dockerfile` e `docker-compose.yml` sono operativi: espongono la porta configurabile, usano un volume SQLite, health check, singolo worker e pass-through esplicito delle variabili. `LOCAL_MODEL_DOCKER_BASE_URL` separa l'endpoint visto dal container da quello nativo; per un modello su un'altra macchina LAN i due URL possono coincidere.

## 5. Copertura documentazione

`docs/HOW_TO_CUSTOMIZE.md` esiste ed è ampia: mappa gli endpoint e descrive agenti, tool, prompt, HITL, override e TTL. Non riflette però accuratamente diversi dettagli correnti:

- usa link assoluti `file:///home/logichole/...`, non portabili;
- documenta la ricorsione gerarchica implementata; restano i limiti di validazione topologica descritti sopra;
- descrive `user_prompt_template`, ma il registry non lo persiste;
- dice che `agent_definition` può essere stringa o dizionario, ma lo schema API accetta una stringa;
- descrive `max_wait_seconds` come attesa massima, ma non esiste enforcement;
- presenta il checkpointer come stato consultabile senza chiarire che è volatile e viene perso a restart/ricompilazione;
- documenta avvio locale/Docker, timeout MAO, provider locale e contratti degli stati; restano da ampliare sicurezza, assenza di auth e comportamento di seed a startup;
- non spiega come assegnare provider/modelli differenti ai livelli, perché la funzione non esiste;
- descrive gli smoke test e Docker operativo; restano da documentare meglio observability (stub), streaming/WebSocket (assenti) e gestione degli errori non LLM;
- il routing dei nomi registrati è ora case-insensitive;
- gli esempi di risposta sono illustrativi e non verificati come contratti OpenAPI end-to-end.

Sezioni da aggiungere o correggere prima della pubblicazione: quickstart completo, matrice env, lifecycle/persistenza, modello di sicurezza, limiti HITL, procedura realmente funzionante per aggiungere e collegare un sotto-agente, configurazione dei modelli per livello, troubleshooting, strategia test, deployment e migrazioni DB.

## 6. Checklist pre-pubblicazione

### Già pronto o riutilizzabile

- [x] Licenza presente (`LICENCE`).
- [x] Separazione di base tra agenti, grafo, API, DB, tool ed esempi.
- [x] Audit log SQLite con transizioni old/new e query parametrizzate.
- [x] Tool mock IoT e medicali utilizzabili per demo locali.
- [x] Registry e albero descrittivo degli agenti dinamici.
- [x] API REST e Swagger generati da FastAPI.
- [x] Due demo e guida di personalizzazione sostanziale.
- [x] Traduzioni inglesi di README e REQUIREMENTS aggiunte con link reciproci.

### Da sistemare prima di dichiarare la repo pubblica/production-ready

- [x] Ripristinare la lettura DB nel percorso del grafo e aggiungere un test della reconciliation.
- [x] Normalizzare i nomi nodo (`brain`) e testare escalation figlio → padre → Brain.
- [ ] Completare la validazione di parent, cicli e target della delega N-livelli.
- [ ] Applicare realmente `priority_weight` o rimuovere la promessa di priorità.
- [ ] Rendere affidabile il risultato dei tool: fallire su tool assente/eccezione e separare attuazione da audit.
- [ ] Sostituire `MemorySaver` con un checkpointer persistente o documentare esplicitamente il limite; preservare i thread durante la ricompilazione.
- [ ] Rendere atomica/sincronizzata la ricompilazione e definire il comportamento multi-worker.
- [ ] Mettere auth/RBAC e allowlist davanti a tool write, override e reset; validare decisioni e comandi per device.
- [ ] Implementare davvero timeout/fallback HITL oppure rimuovere `max_wait_seconds` dalla promessa pubblica.
- [ ] Eliminare la doppia semantica HITL o renderla coerente e testata.
- [ ] Non seminare conflitti demo automaticamente nel DB di produzione.
- [ ] Rendere la suite isolata e standard (`pytest`), correggere i falsi positivi, allineare il conteggio e pubblicare un risultato CI reale.
- [ ] Aggiungere test HTTP/lifespan, graph resume reale, concorrenza, errori DB/LLM e sicurezza.
- [x] Creare Dockerfile e compose funzionanti con volume DB, env e health check.
- [ ] Implementare observability o dichiararla come assente.
- [ ] Completare quickstart/versione Python e aggiungere pin/lock; `.env.example` e `httpx` diretto sono presenti.
- [x] Correggere README/REQUIREMENTS/HOW_TO_CUSTOMIZE per eliminare le affermazioni production-ready non supportate.
- [ ] Aggiungere CI, policy di sicurezza/contribuzione e scansione segreti/dipendenze.

### Roadmap futura dichiarabile apertamente nel README

- [ ] Streaming REST e WebSocket.
- [ ] Backend Postgres e migrazioni versionate.
- [ ] MQTT/webhook reali al posto del producer mock.
- [ ] Modelli/provider differenziati per livello con budget, timeout e circuit breaker.
- [ ] Tracing distribuito, metriche, dashboard e correlation ID.
- [ ] Persistenza/versionamento dello stato fisico dei tool e adapter hardware.
- [ ] Scheduler robusto per macro health check e TTL.
- [ ] UI/dashboard operatore HITL.
