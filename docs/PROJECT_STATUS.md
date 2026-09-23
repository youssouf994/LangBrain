# Stato del progetto LangBrain

Aggiornato il 2026-09-22. La versione precedente di questo documento era un audit statico con l'elenco dei difetti trovati; quei difetti sono stati corretti (la cronologia Git conserva l'audit originale) e qui resta lo stato attuale, i limiti noti e la roadmap.

## Sintesi

LangBrain è un boilerplate funzionante per sistemi di agenti gerarchici su LangGraph, con un dominio dimostrativo (smart home) e uno di prova (omeostasi medica). Il ciclo completo Brain → organi → componenti, le escalation, l'approvazione dell'operatore, l'API, la persistenza su SQLite, la sicurezza di base e la documentazione sono in ordine e coperti da test. **Non è pronto per la produzione**: gira con un solo worker, lo stato fisico dei dispositivi (simulati) è in memoria, manca l'observability. I limiti sono elencati sotto.

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
| Persistenza | Checkpointer `AsyncSqliteSaver` sullo stesso file di `DB_PATH`: thread, interrupt pendenti e scadenze HITL sopravvivono a riavvii e ricompilazioni del grafo. |
| Sicurezza API | Tre ruoli con chiavi separate (`tirocinante`, `medico_di_guardia`, `primario`), matrice dei permessi con rifiuto per impostazione predefinita, un test verifica che ogni rotta sia classificata. Vedi `SECURITY.md`. |
| Streaming | `POST /graph/run/stream` e `/graph/resume/stream` (Server-Sent Events, un evento per nodo) e la pagina `GET /demo` che mostra il grafo in azione. |
| Dipendenze | Versioni esatte in `requirements.txt`, elenco completo in `requirements.lock`; `pip-audit` non trova vulnerabilità note. |
| Test | `python -m pytest tests`: database temporanei, nessun LLM reale, i database veri non vengono mai toccati. Prove per mutazione sui punti critici; prove reali con Mistral/OpenRouter/Gemini eseguite a mano durante lo sviluppo. |
| CI e sicurezza | `.github/workflows/ci.yml` (test su Python 3.12 e 3.14, gitleaks, pip-audit), `scripts/scansione_sicurezza.sh`, `SECURITY.md`, `CONTRIBUTING.md`. La CI è scritta ma non ancora eseguita su GitHub: il primo push la proverà. |
| Simulazione energetica | Rete elettrica e gas con accumuli su dati plausibili dell'Italia o su reti generate fino a migliaia di entità, deterministica a parità di seed, durata fino ad anni e velocità regolabile; pagina `/energia`, rotte `/energia/...`, prova di carico `scripts/stress_energia.py` (10 anni di Italia in 24 s senza violazioni dei bilanci). Collegata a una gerarchia di agenti dedicata (Brain, due organi, quattro componenti) dal sistema nervoso: anomalie → stimolo → ciclo del grafo → leve, con approvazione dell'operatore per le leve che fermano l'industria. Vedi `docs/SIMULAZIONE_ENERGIA.md`. |
| Docker | `Dockerfile` e `docker-compose.yml` (volume SQLite, health check, un worker). Non è stato possibile costruire l'immagine sulla macchina di sviluppo (Docker assente): va provata al primo uso. |

## Limiti noti

- **Un solo worker.** Tool, configurazione HITL a runtime (`/hitl/config`) e grafo sono in memoria del processo; checkpoint e scadenze HITL stanno su SQLite. La ricompilazione del grafo è serializzata da un lock, ma più processi diverrebbero incoerenti.
- **Simulazione energetica in memoria**: lo stato si perde al riavvio; rete per zone di mercato, senza flussi di potenza né vincoli di rampa (dettagli in `docs/SIMULAZIONE_ENERGIA.md`).
- **Stato fisico dei tool in memoria**, non riconciliato con il DB al riavvio; i tool sono simulati (nessun adapter hardware, MQTT o webhook).
- **`DynamicAgent` gestisce solo il primo target** di `managed_targets` quando ha target diretti.
- **Dati non usati o non limitati**: la tabella `readings` non viene popolata; `GraphState.reduce_readings` accumula senza finestra temporale.
- **Registro eventi**: timestamp SQLite al secondo (eventi simultanei senza ordine garantito); gli errori DB in `mark_resolved`, `unblock_target` e TTL sono registrati nei log ma non propagati all'API.
- **Provider gratuiti**: i modelli gratuiti hanno limiti di richieste e crediti; quando finiscono il grafo si ferma per l'operatore (comportamento voluto), oppure si abilita il ripiego in `configurazione.toml`.
- **Observability**: solo log standard, nessuna metrica, trace o correlation ID. Nessun WebSocket (lo streaming è a Server-Sent Events).
- **Test ancora assenti**: concorrenza SQLite tra più processi, build Docker, carico.
- **Override**: la traduzione della direttiva in comandi è affidata all'LLM; ogni comando passa comunque dall'elenco dei dispositivi ammessi e viene tracciato nell'audit log con `actor: "Brain_Override"`.

## Dipendenze e configurazione

### Dipendenze

Le dipendenze sono fissate (2026-09-20):

| File | Contenuto |
|---|---|
| `requirements.txt` | le 10 dipendenze dirette con versione esatta (`==`): `langgraph`, `langgraph-checkpoint-sqlite`, `langchain-core`, `fastapi`, `uvicorn[standard]`, `pydantic`, `aiosqlite`, `python-dotenv`, `httpx`, `openai` |
| `requirements.lock` | elenco completo di 52 pacchetti (transitive incluse), generato da un ambiente pulito; lo usa il Dockerfile (`pip install --no-deps` seguito da `pip check`) |
| `requirements-dev.txt` | `requirements.txt` più `pytest` |

`langchain-mistralai` è stata rimossa perché nessun file la importa (Mistral usa il client OpenAI-compatibile del MAO), e `pytest-asyncio` non serve (i test asincroni sono `unittest.IsolatedAsyncioTestCase`). Verifiche: in un ambiente pulito con le sole versioni fissate la suite passa; il lock si installa identico e `pip check` non trova conflitti; ogni pacchetto del lock ha un pacchetto installabile per Python 3.12. Non è stato possibile eseguire la build Docker né provare l'esecuzione su Python 3.12 (Docker e Python 3.12 non disponibili sulla macchina di sviluppo, dove la suite gira con Python 3.14). Non esiste `pyproject.toml`; la CI (`.github/workflows/ci.yml`) esegue la suite su Python 3.12 e 3.14.

### Variabili d'ambiente attese dal codice

- Scelte HITL e demo: `[hitl]` e `[demo]` in `configurazione.toml`.
- Database/runtime: `DB_PATH`, `FLAG_TTL_MINUTES`, `MAO_TIMEOUT_SECONDS` (default `40`).
- LLM: `MAO_MAX_TOKENS_MINIMO` (8192), `MAO_MAX_TOKENS_LIMITE` (32768), `MAO_FALLBACK` (vuoto: decide `configurazione.toml`; 0/1 lo sostituiscono), `LANGBRAIN_ENV_FILE` (percorso alternativo del `.env`).
- Sicurezza: `API_KEY_TIROCINANTE`, `API_KEY_MEDICO_DI_GUARDIA`, `API_KEY_PRIMARIO` e la chiave unica storica `API_KEY` (= primario). Se nessuna è impostata l'API resta aperta, con avviso all'avvio.
- Provider: `DEFAULT_PROVIDER` (`google_studio`, `openrouter`, `mistral`, `local`, `auto`).
- Mistral: `MISTRAL_API_KEY`, `MISTRAL_BASE_URL`, `MISTRAL_MODEL` (default `ministral-8b-latest`).
- Scelte dell'utente: file `configurazione.toml` (dispositivi ammessi, politica LLM), percorso alternativo `LANGBRAIN_CONFIG`.
- Google: `GOOGLE_BASE_URL`, `GEMINI_API_KEY` o `GOOGLE_API_KEY`, `GEMINI_MODEL`.
- Locale: `LOCAL_MODEL_BASE_URL`, `LOCAL_MODEL_DOCKER_BASE_URL`, `LOCAL_API_KEY`, `LOCAL_MODEL`.
- OpenRouter: `OPENROUTER_BASE_URL`, `OPENROUTER_API_KEY`, `OPENROUTER_REFERER`, `OPENROUTER_APP_TITLE`, `OPENROUTER_MODEL`.
- Prompt Brain: `BRAIN_SYSTEM_PROMPT`, `BRAIN_USER_PROMPT_TEMPLATE`.

`.env.example` documenta queste variabili. Priorità, target, valori fisici dei device, frequenze e intervalli del loop restano in parte hardcoded o demandati all'implementazione del dominio.

### Coerenza Docker Compose

`Dockerfile` e `docker-compose.yml` sono operativi: espongono la porta configurabile, usano un volume SQLite, health check, singolo worker e pass-through esplicito delle variabili. `LOCAL_MODEL_DOCKER_BASE_URL` separa l'endpoint visto dal container da quello nativo; per un modello su un'altra macchina LAN i due URL possono coincidere.

## Roadmap

- [ ] WebSocket (oltre allo streaming SSE già presente).
- [ ] Backend Postgres e migrazioni versionate.
- [ ] MQTT/webhook reali al posto del produttore di eventi simulato.
- [ ] Budget, timeout e circuit breaker per modello.
- [ ] Tracing distribuito, metriche e correlation ID.
- [ ] Persistenza dello stato fisico dei tool e adapter hardware.
- [ ] Scheduler robusto per health check e TTL.
- [ ] Funzionamento multi-worker (stato condiviso fuori dal processo).
- [ ] Dashboard operatore completa (la pagina `/demo` è solo dimostrativa).
- [ ] Test di concorrenza e di carico.
