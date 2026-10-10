# LangBrain project status

🇬🇧 English | 🇮🇹 [Italiano](PROJECT_STATUS.md)

Updated on 2026-10-03. The previous version of this document was a static audit listing the defects found; those
defects have been fixed (the Git history keeps the original audit) and what remains here is the current state, the
known limits and the roadmap.

## Summary

LangBrain is a working boilerplate for hierarchical agent systems on LangGraph, with a demonstration domain (smart
home), a test domain (medical homeostasis) and nodes that plug into the API as extensions (`app/nodes`). The full cycle Brain →
organs → components, escalations, operator approval, the API, persistence on SQLite, basic security and the
documentation are in order and covered by tests. **It is not production-ready**: it runs with a single worker by default (several with `python -m app.serve`), the
physical state of the (simulated) devices is in memory, observability is missing. The limits are listed below.

## What is there and verified

| Area | State |
|---|---|
| N-level hierarchy | `parent_agent_name` is the only source of truth; the registry validates parents, cycles, levels and shared targets (422/409). A junior agent asks its senior, who, if it cannot answer, asks its own superior up to the Brain: only the awkward situations reach the top. |
| Priority | A lock prevails only if set by an actor with strictly higher priority; the Brain always wins; an unknown actor locks to be safe (`app/core/priorita.py`). |
| Tools and faults | Every read/actuation returns a structured outcome (`APPLIED`, `ALREADY_SET`, `REJECTED_PRIORITY`, `COMMAND_NOT_ALLOWED`, `TOOL_MISSING`, `TOOL_ERROR`, ...) with the reason: the parent with enough priority diagnoses and retries once, then the fault goes up to the Brain and finally to the operator. |
| Allowed commands | No agent, override or API write can command a device or a value not listed in the configuration file. |
| Model errors | Classified codes (`CREDITI_ESAURITI`, `CHIAVE_NON_VALIDA`, `LIMITE_RICHIESTE`, `RISPOSTA_TRONCATA`, ...) with a hint; no silent fallback: the graph stops and the operator solves the problem (changes key, tops up credits) and resumes. `.env` and the configuration file are reloaded live. |
| Models per agent | Provider and model per agent through the API, inherited from the parent (`app/core/modelli_agenti.py`). Providers: OpenRouter, Google AI Studio, Mistral, local. |
| HITL | Two separate flows (wrapper on nodes, Brain approval on escalations) chosen with `[hitl] level`; emergency pauses always active; timer with action on expiry (`system`, `reject`, `human`); decisions `APPROVE`, `REJECT`, `OVERRIDE` (the latter with semantic arbitration by the LLM). |
| Persistence | `AsyncSqliteSaver` checkpointer on the same file as `DB_PATH`: threads, pending interrupts and HITL deadlines survive restarts and recompilations of the graph. |
| API security | Three roles with separate keys (viewer, operator, admin; Italian names `tirocinante`, `medico_di_guardia`, `primario`), permission matrix that denies by default, a test checks that every route is classified. See `SECURITY.md`. |
| Languages | Italian and English: configuration names, roles and environment variables are accepted in both languages; `[general] language` chooses the default prompts, API messages and page texts; the answers of the model and the operator decisions are read in both languages. |
| Streaming | `POST /graph/run/stream` and `/graph/resume/stream` (Server-Sent Events, one event per node) and the `GET /demo` page that shows the graph at work. |
| Statistics for charts | `GET /events/stats`, `GET /graph/stats`, `GET /graph/runs`, `GET /signals`, `GET /signals/stats`: indicators of the nervous system, of the graph runs and of the signals, with generic parameters. |
| Settings page | `/config`: edits `.env` and `configurazione.toml` (and the files of installed nodes); the admin can restart the app. |
| Dependencies | Exact versions in `requirements.txt`, complete list in `requirements.lock`; `pip-audit` finds no known vulnerabilities. |
| Tests | `python -m pytest tests`: temporary databases, no real LLM, the real databases are never touched. Mutation checks on the critical points; real runs with Mistral/OpenRouter/Gemini done by hand during development. |
| CI and security | `.github/workflows/ci.yml` (tests on Python 3.12 and 3.14, gitleaks, pip-audit), `scripts/scansione_sicurezza.sh`, `SECURITY.md`, `CONTRIBUTING.md`. The CI is written but not yet run on GitHub: the first push will try it. |
| Energy simulation | Electricity and gas network with storage on plausible data of Italy or on generated networks up to thousands of entities, deterministic for a given seed, lasting up to years at an adjustable speed; page `/energia`, routes `/energia/...`, load test `scripts/stress_energia.py` (10 years of Italy in 24 s with no balance violation). An extension of the core (`app/nodes/energia.py`), tied to its own hierarchy of agents (Brain, two organs, four components) by the nervous system: anomaly → stimulus → graph cycle → levers, with operator approval for the levers that stop industry. See `docs/SIMULAZIONE_ENERGIA.md` (Italian). |
| Docker | `Dockerfile` and `docker-compose.yml` (SQLite volume, health check, `python -m app.serve` with `LANGBRAIN_WORKERS` workers, tesseract for local OCR redaction). The image could not be built on the development machine (no Docker): try it at first use. |

## Known limits

- **Several workers share state through SQLite** (`python -m app.serve`, `app/core/multiprocesso.py`): background tasks
  in one process at a time, runtime HITL configuration, simulated effector values, tool switches and the compiled graph
  shared. Not shared: simulated faults, and the single `api_session` thread of the demo graph (runs take turns).
  Plain `python -m uvicorn` is still one process.
- **The energy simulation is in memory**: its state is lost at restart and lives in the process that runs it (with several workers use only one for `/energia`); network by market zones, without power flows or ramp limits (details in `docs/SIMULAZIONE_ENERGIA.md`).
- **Physical state of the tools in memory**, not reconciled with the DB at restart; the tools are simulated (no
  hardware, MQTT or webhook adapter).
- **Unused or unbounded data**: the `readings` table is not populated; `GraphState.reduce_readings` accumulates
  without a time window.
- **Event log**: SQLite timestamps to the second (simultaneous events without guaranteed order); DB errors in
  `mark_resolved`, `unblock_target` and TTL are raised: `/events/unblock` and `/events/reset-conflicts` answer 503 without
  touching the device, the Brain completes its cycle and tells the operator that the escalation stayed open.
- **Free providers**: free models have request and credit limits; when they run out the graph stops for the operator
  (intended behaviour), or the fallback is enabled in the configuration.
- **Observability**: standard logs only, no metrics, traces or correlation IDs. No WebSocket (streaming uses
  Server-Sent Events).
- **Languages**: configuration validation errors and logs are in Italian.
- **Tests still missing**: SQLite concurrency across processes, Docker build, load.
- **Override**: turning the directive into commands is left to the LLM; every command still goes through the list of
  allowed devices and is traced in the audit log with `actor: "Brain_Override"`.

## Dependencies and configuration

### Dependencies

The dependencies are pinned:

| File | Content |
|---|---|
| `requirements.txt` | the 14 direct dependencies with exact version (`==`): `langgraph`, `langgraph-checkpoint-sqlite`, `langchain-core`, `fastapi`, `uvicorn[standard]`, `pydantic`, `aiosqlite`, `python-dotenv`, `httpx`, `openai`, `pypdf`, `asn1crypto`, `pillow`, `cryptography` |
| `requirements.lock` | complete list of the packages (transitive included), generated from a clean environment; used by the Dockerfile (`pip install --no-deps` followed by `pip check`) |
| `requirements-dev.txt` | `requirements.txt` plus `pytest` |

`langchain-mistralai` was removed because no file imports it (Mistral uses the OpenAI-compatible client of the MAO),
and `pytest-asyncio` is not needed (async tests are `unittest.IsolatedAsyncioTestCase`). There is no `pyproject.toml`;
the CI (`.github/workflows/ci.yml`) runs the suite on Python 3.12 and 3.14.

### Environment variables expected by the code

- HITL and demo choices: `[hitl]` and `[demo]` in the configuration file.
- Database/runtime: `DB_PATH`, `FLAG_TTL_MINUTES`, `MAO_TIMEOUT_SECONDS` (default `40`).
- LLM: `MAO_MAX_TOKENS_MINIMO` (8192), `MAO_MAX_TOKENS_LIMITE` (32768), `MAO_FALLBACK` (empty: the configuration
  decides; 0/1 replace it), `LANGBRAIN_ENV_FILE` (alternative path of `.env`).
- Security: `API_KEY_VIEWER`, `API_KEY_OPERATOR`, `API_KEY_ADMIN` (or the Italian `API_KEY_TIROCINANTE`,
  `API_KEY_MEDICO_DI_GUARDIA`, `API_KEY_PRIMARIO`) and the historic single key `API_KEY` (= admin). If none is set the
  API stays open, with a warning at start.
- Provider: `DEFAULT_PROVIDER` (`google_studio`, `openrouter`, `mistral`, `local`, `auto`).
- Mistral: `MISTRAL_API_KEY`, `MISTRAL_BASE_URL`, `MISTRAL_MODEL` (default `ministral-8b-latest`).
- User choices: `configurazione.toml` or `config.toml` (allowed devices, LLM policy, language), alternative path
  `LANGBRAIN_CONFIG`.
- Google: `GOOGLE_BASE_URL`, `GEMINI_API_KEY` or `GOOGLE_API_KEY`, `GEMINI_MODEL`.
- Local: `LOCAL_MODEL_BASE_URL`, `LOCAL_MODEL_DOCKER_BASE_URL`, `LOCAL_API_KEY`, `LOCAL_MODEL`.
- OpenRouter: `OPENROUTER_BASE_URL`, `OPENROUTER_API_KEY`, `OPENROUTER_REFERER`, `OPENROUTER_APP_TITLE`,
  `OPENROUTER_MODEL`.
- Prompts of the Brain, troubleshooting, override and routine check: section `[prompts]` of the
  configuration or `PROMPT_*` variables of `.env` (the answer format stays a contract of the code).

`.env.example` documents these variables. Priorities, targets, physical values of the devices, frequencies and loop
intervals are partly hardcoded or left to the domain implementation.

### Docker Compose consistency

`Dockerfile` and `docker-compose.yml` work: they expose the configurable port, use a SQLite volume, health check,
a configurable number of workers and explicit pass-through of the variables. `LOCAL_MODEL_DOCKER_BASE_URL` separates the endpoint seen
by the container from the native one; for a model on another LAN machine the two URLs can be the same.

## Roadmap

- [ ] WebSocket (besides the SSE streaming already present).
- [ ] Postgres backend and versioned migrations.
- [ ] Real MQTT/webhooks instead of the simulated event producer.
- [ ] Budget, timeout and circuit breaker per model.
- [ ] Distributed tracing, metrics and correlation IDs.
- [ ] Persistence of the physical state of the tools and hardware adapters.
- [ ] Robust scheduler for health checks and TTL.
- [x] Multi-worker operation (shared state on SQLite; a real device backend would replace the simulated effectors).
- [ ] Test of concurrency and load.
