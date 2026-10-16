# Stato del progetto LangBrain

🇮🇹 Italiano | 🇬🇧 [English](PROJECT_STATUS.en.md)

Aggiornato il 2026-10-16. La versione precedente di questo documento era un audit statico con l'elenco dei difetti trovati; quei difetti sono stati corretti (la cronologia Git conserva l'audit originale) e qui resta lo stato attuale, i limiti noti e la roadmap.

## Sintesi

LangBrain è un boilerplate funzionante per sistemi di agenti gerarchici su LangGraph, con un dominio dimostrativo (smart home) e uno di prova (omeostasi medica), nominato con il vocabolario del corpo (Brain → Organ → Tissue → Cell; `Reflex`, `Receptor`, `Effector`, `NervousSystem`) e capace di ospitare nodi come estensioni (`app/nodes`). Il ciclo completo Brain → organi → tessuti → cellule, le escalation, l'approvazione dell'operatore, l'API, la persistenza su SQLite o Postgres, la sicurezza di base e la documentazione sono in ordine e coperti da test. **Non è pronto per la produzione**: gira con un solo worker per impostazione predefinita (più worker con `python -m app.serve`), gli effettori sono simulati e il loro stato fisico non è riconciliato dopo un riavvio, mancano metriche e tracing. I limiti sono elencati sotto.

## Cosa c'è ed è verificato

| Area | Stato |
|---|---|
| Gerarchia N-livelli | `parent_agent_name` è l'unica fonte di verità; il registro valida padri, cicli, livelli e target condivisi (422/409). Un agente junior chiede al senior, che se non sa risponde al proprio superiore fino al Brain: solo le situazioni scomode arrivano in cima. |
| Priorità | Un blocco prevale solo se imposto da un attore con priorità strettamente maggiore; il Brain vince sempre; un attore sconosciuto blocca per prudenza (`app/core/priorita.py`). |
| Tool e guasti | Ogni lettura/attuazione restituisce un esito strutturato (`APPLIED`, `ALREADY_SET`, `REJECTED_PRIORITY`, `COMMAND_NOT_ALLOWED`, `TOOL_MISSING`, `TOOL_ERROR`, ...) con il motivo: il padre con priorità sufficiente diagnostica e ritenta una volta, poi il guasto sale al Brain e infine all'operatore. |
| Comandi ammessi | Nessun agente, override o scrittura API può comandare un dispositivo o un valore non elencato in `configurazione.toml`. |
| Errori del modello | Codici classificati (`CREDITI_ESAURITI`, `CHIAVE_NON_VALIDA`, `LIMITE_RICHIESTE`, `RISPOSTA_TRONCATA`, ...) con suggerimento; nessun ripiego silenzioso: il grafo si ferma e l'operatore risolve (cambia chiave, ricarica crediti) e riprende. `.env` e `configurazione.toml` si ricaricano a caldo. |
| Modelli per agente | Provider e modello per singolo agente via API, ereditati dal padre (`app/core/modelli_agenti.py`). Provider: OpenRouter, Google AI Studio, Mistral, locale. |
| HITL | Due flussi separati (wrapper sui nodi, approvazione del Brain sulle escalation) scelti con `[hitl] livello`; pause di emergenza sempre attive; timer con azione alla scadenza (`sistema`, `respingi`, `umano`); decisioni `APPROVA`, `RESPINGI`, `OVERRIDE` (quest'ultimo con arbitrato semantico dell'LLM). |
| Persistenza | Un solo punto d'accesso al database (`app/core/archivio.py`, SQLAlchemy Core): SQLite nel file `DB_PATH` (predefinito) oppure Postgres con `LANGBRAIN_DB_BACKEND=postgres` e `LANGBRAIN_DB_URL`. Tornare a SQLite: [`ROLLBACK.md`](ROLLBACK.md). Checkpointer `AsyncSqliteSaver` sullo stesso file, o `AsyncPostgresSaver` su pool con Postgres: thread, interrupt pendenti e scadenze HITL sopravvivono a riavvii e ricompilazioni del grafo. |
| Sicurezza API | Tre ruoli con chiavi separate (`tirocinante`, `medico_di_guardia`, `primario`), matrice dei permessi con rifiuto per impostazione predefinita, un test verifica che ogni rotta sia classificata. Vedi `SECURITY.md`. |
| Lingue | Italiano e inglese: nomi della configurazione, ruoli e variabili d'ambiente accettati in entrambe le lingue; `[generale] lingua` sceglie i prompt predefiniti, i messaggi dell'API e i testi delle pagine; risposte del modello e decisioni dell'operatore sono lette in entrambe le lingue. |
| Streaming | `POST /graph/run/stream` e `/graph/resume/stream` (Server-Sent Events, un evento per nodo) e la pagina `GET /demo` che mostra il grafo in azione. |
| Statistiche per i grafici | `GET /events/stats`, `GET /graph/stats`, `GET /graph/runs`, `GET /signals`, `GET /signals/stats`: indicatori del sistema nervoso, delle run del grafo e dei segnali, con parametri generici. |
| Pagina impostazioni | `/config`: modifica `.env` e `configurazione.toml` (e i file dei nodi installati); l'amministratore può riavviare l'app. |
| Estensioni | `app/nodes/`: un nodo si aggancia all'API (rotte, pagine, impostazioni, prompt) senza che il core lo conosca. |
| Dipendenze | Versioni esatte in `requirements.txt`, elenco completo in `requirements.lock`; `pip-audit` non trova vulnerabilità note. |
| Test | `python -m pytest tests`: database temporanei, nessun LLM reale, i database veri non vengono mai toccati. `python -m pytest tests_windows` (2026-10-16): 649 passati, 10 saltati, 0 falliti. Prove per mutazione sui punti critici; prove reali con Mistral/OpenRouter/Gemini eseguite a mano durante lo sviluppo. |
| CI e sicurezza | `.github/workflows/ci.yml`, verde su GitHub su `main` (commit `a9cccc2`, tag `poc-contratto-1.0.4-rc2`): test SQLite su Python 3.12 e 3.14, intera suite su Postgres 16 (`tests/pg_isolamento.py`), **build dell'immagine Docker con avvio** e rifiuto dell'avvio senza chiave, gitleaks e pip-audit (`scripts/scansione_sicurezza.sh`). Il job `conformita` (suite del contratto) è disabilitato: vive nel fork di HQ. Più `SECURITY.md`, `CONTRIBUTING.md`. |
| Docker | `Dockerfile` e `docker-compose.yml` (volume SQLite o `LANGBRAIN_DB_BACKEND`/`LANGBRAIN_DB_URL` per Postgres, health check, `python -m app.serve` con `LANGBRAIN_WORKERS` worker). L'immagine è stata costruita e provata (2026-10-15) con 3 worker contro Postgres: avvio, agenti, modelli, eventi, statistiche, HITL, checkpoint, riavvio e reset funzionano. |

## Limiti noti

- **Più worker condividono lo stato nel database (SQLite o Postgres)** (`python -m app.serve`, `app/core/multiprocesso.py`): compiti in background in un processo alla volta, configurazione HITL a runtime, valori degli effettori simulati, interruttori dei tool e grafo compilato condivisi. Non condivisi: i guasti simulati e l'unico thread `api_session` del grafo dimostrativo (le esecuzioni si mettono in fila). `python -m uvicorn` resta un processo solo.
- **Stato fisico dei tool in memoria**, non riconciliato con il DB al riavvio; i tool sono simulati (nessun adapter hardware, MQTT o webhook).
- **Segnali**: i valori numerici dei segnali finiscono nella tabella `signals` (`NervousSystem.record_signals`) e `reduce_readings` scarta quelli oltre la finestra (`[signals] window_hours`, 4 ore). La tabella `readings` è un residuo del nome vecchio: resta nello schema ma non viene né scritta né letta (la svuota solo `DELETE /system/reset`), e la chiave `readings` dello stato del grafo mantiene il nome storico.
- **Registro eventi**: timestamp al millisecondo generati dall'applicazione (campo `ts` in ISO 8601 UTC) e ordine garantito dall'`event_id` crescente; ogni decisione porta `correlation_id`, `outcome`, `command_id`, `approval_id`. Gli errori DB in `mark_resolved`, `unblock_target` e TTL vengono rilanciati: `/events/unblock` e `/events/reset-conflicts` rispondono 503 senza toccare il dispositivo, il Brain completa il ciclo e avvisa l'operatore che l'escalation è rimasta aperta.
- **Provider gratuiti**: i modelli gratuiti hanno limiti di richieste e crediti; quando finiscono il grafo si ferma per l'operatore (comportamento voluto), oppure si abilita il ripiego in `configurazione.toml`.
- **Observability**: correlation id e attore per richiesta (`X-Correlation-Id`, `X-Actor`, `app/core/contesto.py`) e log JSON opzionale (`LANGBRAIN_LOG_FORMAT=json`, senza segreti). Nessuna metrica o trace. Nessun WebSocket (lo streaming è a Server-Sent Events).
- **Lingue**: gli errori di validazione della configurazione e i log sono in italiano.
- **Test ancora assenti**: carico; timer HITL con scadenza reale su Postgres.
- **Timeout a intermittenza della suite su Postgres, solo in locale con Docker Desktop (Windows)**: se il container dei test raggiunge Postgres
  passando dalla porta pubblicata sull'host (`host.docker.internal:55433`), qualche test (visti: `test_modelli_agenti`, `test_timer_hitl`,
  `test_validazione_gerarchia`) resta fermo 60 s e fallisce con `TimeoutError`: è il timeout di connessione predefinito di asyncpg, e la
  suite dura 130-290 s invece di 80. Non è codice del nucleo: con il container sulla **stessa rete Docker** di Postgres (nome del
  servizio, nessun port-forward) la suite passa intera (530 passati, 0 falliti in 79 s, anche con `--cpus 1`), e la CI su GitHub non l'ha
  mai visto. Per riprodurlo: `docker run --add-host host.docker.internal:host-gateway -e TEST_PG_URL=postgresql://postgres:prova@host.docker.internal:55433/langbrain_test <immagine> python -m pytest tests`
  con Docker Desktop già carico; per evitarlo: `docker network create n`, Postgres e test sulla stessa rete e URL con il nome del container.
- **Suite Windows**: nessun fallimento noto. Gli 11 `WinError 32` di prima erano dei test (`with sqlite3.connect(...)` non chiude il file) e sono corretti con `connetti` di `tests/sqlite_chiusura.py`; dettagli in `tests_windows/README.md`.
- **Override**: la traduzione della direttiva in comandi è affidata all'LLM; ogni comando passa comunque dall'elenco dei dispositivi ammessi e viene tracciato nell'audit log con `actor: "Brain_Override"`.

## Dipendenze e configurazione

### Dipendenze

Le dipendenze sono fissate (2026-09-20):

| File | Contenuto |
|---|---|
| `requirements.txt` | le dipendenze dirette con versione esatta (`==`): `langgraph`, `langgraph-checkpoint-sqlite`, `langgraph-checkpoint-postgres`, `langchain-core`, `fastapi`, `uvicorn[standard]`, `pydantic`, `aiosqlite`, `sqlalchemy[asyncio]`, `alembic`, `asyncpg`, `psycopg[binary,pool]`, `python-dotenv`, `httpx`, `openai` |
| `requirements.lock` | elenco completo di 52 pacchetti (transitive incluse), generato da un ambiente pulito; lo usa il Dockerfile (`pip install --no-deps` seguito da `pip check`) |
| `requirements-dev.txt` | `requirements.txt` più `pytest` |

`langchain-mistralai` è stata rimossa perché nessun file la importa (Mistral usa il client OpenAI-compatibile del MAO), e `pytest-asyncio` non serve (i test asincroni sono `unittest.IsolatedAsyncioTestCase`). Verifiche: in un ambiente pulito con le sole versioni fissate la suite passa; il lock si installa identico e `pip check` non trova conflitti; ogni pacchetto del lock ha un pacchetto installabile per Python 3.12. Non è stato possibile eseguire la build Docker né provare l'esecuzione su Python 3.12 (Docker e Python 3.12 non disponibili sulla macchina di sviluppo, dove la suite gira con Python 3.14). Non esiste `pyproject.toml`; la CI (`.github/workflows/ci.yml`) esegue la suite su Python 3.12 e 3.14.

### Variabili d'ambiente attese dal codice

- Scelte HITL e demo: `[hitl]` e `[demo]` in `configurazione.toml`.
- Database/runtime: `LANGBRAIN_DB_BACKEND` (`sqlite` predefinito o `postgres`), `LANGBRAIN_DB_URL` (con `postgres`), `DB_PATH`, `LANGBRAIN_REQUIRE_API_KEY` (1: avvio rifiutato senza chiave API), `LANGBRAIN_LOG_FORMAT=json`, `LANGBRAIN_LOG_LEVEL`, `LANGBRAIN_APARTMENT_ID`, `LANGBRAIN_RETENTION_EVENTI_GIORNI`, `LANGBRAIN_RETENTION_COMANDI_GIORNI`, `LANGBRAIN_RETENTION_INTERVALLO_ORE`, `FLAG_TTL_MINUTES`, `MAO_TIMEOUT_SECONDS` (default `40`).
- LLM: `MAO_MAX_TOKENS_MINIMO` (8192), `MAO_MAX_TOKENS_LIMITE` (32768), `MAO_FALLBACK` (vuoto: decide `configurazione.toml`; 0/1 lo sostituiscono), `LANGBRAIN_ENV_FILE` (percorso alternativo del `.env`).
- Sicurezza: `API_KEY_TIROCINANTE`, `API_KEY_MEDICO_DI_GUARDIA`, `API_KEY_PRIMARIO` e la chiave unica storica `API_KEY` (= primario). Se nessuna è impostata l'API resta aperta, con avviso all'avvio.
- Provider: `DEFAULT_PROVIDER` (`google_studio`, `openrouter`, `mistral`, `local`, `auto`).
- Mistral: `MISTRAL_API_KEY`, `MISTRAL_BASE_URL`, `MISTRAL_MODEL` (default `ministral-8b-latest`).
- Scelte dell'utente: file `configurazione.toml` (dispositivi ammessi, politica LLM), percorso alternativo `LANGBRAIN_CONFIG`.
- Google: `GOOGLE_BASE_URL`, `GEMINI_API_KEY` o `GOOGLE_API_KEY`, `GEMINI_MODEL`.
- Locale: `LOCAL_MODEL_BASE_URL`, `LOCAL_MODEL_DOCKER_BASE_URL`, `LOCAL_API_KEY`, `LOCAL_MODEL`.
- OpenRouter: `OPENROUTER_BASE_URL`, `OPENROUTER_API_KEY`, `OPENROUTER_REFERER`, `OPENROUTER_APP_TITLE`, `OPENROUTER_MODEL`.
- Prompt del Brain, del troubleshooting, dell'override e del controllo macro: sezione `[prompt]` di `configurazione.toml` o variabili `PROMPT_*` del `.env` (il formato di risposta resta un contratto del codice).

`.env.example` documenta queste variabili. Priorità, target, valori fisici dei device, frequenze e intervalli del loop restano in parte hardcoded o demandati all'implementazione del dominio.

### Coerenza Docker Compose

`Dockerfile` e `docker-compose.yml` sono operativi: espongono la porta configurabile, usano un volume SQLite (o `LANGBRAIN_DB_BACKEND`/`LANGBRAIN_DB_URL` per Postgres), health check, numero di worker configurabile e pass-through esplicito delle variabili. `LOCAL_MODEL_DOCKER_BASE_URL` separa l'endpoint visto dal container da quello nativo; per un modello su un'altra macchina LAN i due URL possono coincidere.

## Roadmap

- [ ] WebSocket (oltre allo streaming SSE già presente).
- [x] Backend Postgres (`LANGBRAIN_DB_BACKEND=postgres` + `LANGBRAIN_DB_URL`, SQLAlchemy Core, interruttore e rollback in [`ROLLBACK.md`](ROLLBACK.md), suite eseguita anche su Postgres in CI).
- [ ] Migrazioni versionate (Alembic è nelle dipendenze ma non ancora usato: oggi le colonne mancanti si aggiungono in automatico).
- [ ] MQTT/webhook reali al posto del produttore di eventi simulato.
- [ ] Budget, timeout e circuit breaker per modello.
- [x] Correlation id e log JSON; [ ] tracing distribuito e metriche.
- [ ] Persistenza dello stato fisico dei tool e adapter hardware.
- [ ] Scheduler robusto per health check e TTL.
- [x] Funzionamento multi-worker (stato condiviso nel database, SQLite o Postgres; un backend di dispositivi reali sostituirebbe gli effettori simulati).
- [ ] Dashboard operatore completa nel core (la pagina `/demo` è solo dimostrativa; un nodo può aggiungere le sue pagine come estensione).
- [ ] Test di concorrenza e di carico.

