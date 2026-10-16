# Changelog

## Unreleased

The framework is renamed after the human body so that the same core fits any domain, and a use case no longer touches the
core: it plugs into the API as an extension. The old names keep working as aliases.

### Vocabulary of the body (migration table)

| Old | New |
|---|---|
| `BaseTool` (`app/tools/baseTool.py`) | `Reflex` (`app/tools/reflex.py`); `Receptor` is a `Reflex` that turns a stimulus into a signal |
| `IoTDeviceTool`, `get_default_iot_tools` (`app/tools/sensor_tools.py`) | `SimulatedEffector`, `default_effectors` (`app/tools/effectors.py`) |
| `EventLog` (`app/tools/event_log.py`) | `NervousSystem` (`app/tools/nervous_system.py`) |
| `execute_tool_safely`, `force_execute_tool` (`app/tools/tool_wrapper.py`) | the same functions in `app/tools/motor_control.py` |
| readings, `SensorReading`, `sensor_readings` in `POST /graph/run`, table `readings` | signals, `signals` in `POST /graph/run`, table `signals` (the state key `readings` is unchanged) |
| `GET/POST /tools`, `/tools/{id}`, `/tools/{id}/fault` | `/effectors`, `/effectors/{id}`, `/effectors/{id}/fault` |
| `[devices.*]` / `[dispositivi.*]`, `unlisted_devices` | `[effectors.*]`, `unlisted_effectors` |
| levels Brain → Organ → Component → … | Brain → Organ → Tissue → Cell (`nome_livello`) |
| `app/agents/agent_climate.py`, `medical_agents.py`, `app/tools/medical_tools.py` | `examples/smart_home/`, `examples/medical_homeostasis/`: domain examples live outside the core |

The old Python modules are shims that re-export the new ones with a `DeprecationWarning`; the old configuration names, API
routes and the old table are accepted as aliases. `tests/` still imports the old names and passes.

### Extensions
- A module in `app/nodes/` that exposes `ESTENSIONE` (`app/core/estensioni.py`) plugs a use case into the API without the
  core knowing it: routes and pages, the minimum role of each route, entries of the top bar, background tasks and their
  shutdown, prompts, page texts (merged with the core's) and the files and fields it adds to the settings page. With
  nothing in `app/nodes/` the API runs as the plain framework. See section 10 of `docs/HOW_TO_CUSTOMIZE.en.md`.

### Settings page and restart
- `/config` edits `.env`, `configurazione.toml` / `config.toml` (and the files of installed nodes) with a form, over the
  same files: comments, order and Italian or English names are kept, the previous version of each file is saved as `.bak`
  (`POST /config/api/restore`), nothing is written unless the real loaders accept it, secrets are write-only.
- **Restart app** button for the administrator: `POST /config/api/restart` stops the process like Ctrl+C and starts the
  same command again (Docker: the process exits and the restart policy brings it back). `GET /config/api/fields` says
  `can_restart` for the key that asks.

### Postgres (or SQLite) behind one layer
- `LANGBRAIN_DB_BACKEND=sqlite|postgres` (default `sqlite`) chooses the database. With `postgres`, `LANGBRAIN_DB_URL=postgresql://...`
  (Scaleway, Supabase, RDS...) is required: without it the start is refused with a clear message. `sslmode=` in the URL is
  understood. SQLite stays the default, in the file `DB_PATH`. How to go back: `docs/ROLLBACK.md`.
- Everything goes through `app/core/archivio.py` (SQLAlchemy Core, schema in `app/db/schema.py`): values are always bound
  parameters, never pasted into SQL, and upserts/types are translated by the dialect. This covers events, signals, node
  runs, the agent registry, per-agent models, HITL timers, the shared state and leases of the workers, the scenarios and
  `DELETE /system/reset`. A node asks the archivio for the engine and never writes a connection string.
- Migrations are additive and idempotent on both: missing columns and indexes are added, nothing is removed or renamed. A
  SQLite file migrated by this code opens fine with the previous code.
- Concurrent `CREATE TABLE` from several workers no longer fails (advisory lock on Postgres, retry on SQLite).
- The LangGraph checkpointer uses `AsyncPostgresSaver` on a connection pool when the backend is Postgres.
- Database errors are now `sqlalchemy.exc.SQLAlchemyError` (no longer `aiosqlite.Error`).
- Tests: the whole suite also runs on Postgres with `TEST_PG_URL` (each temporary database becomes a schema, see
  `tests/pg_isolamento.py`; `docker compose -f docker-compose.test.yml up -d` gives a throw-away Postgres on port 55433);
  `tests/test_postgres.py` runs with `TEST_DATABASE_URL`.
- `requirements.txt` / `requirements.lock`: SQLAlchemy, Alembic, asyncpg, psycopg, `langgraph-checkpoint-postgres`.

### Alignment with contract 1.0.4 (core side)
- **Correlation id and actor** (`app/core/contesto.py`): an ASGI middleware reads `X-Correlation-Id` (used as it comes, a UUID4
  is generated if missing) and `X-Actor` (email -> `user:<email>`, `system:<name>` -> `system`; internal actors stay
  `rule:<id>`, `organ:<name>`, `brain`, `assistant`) and returns the id in the response. Internal facts (HITL timer expiry)
  get their own id.
- **Decision log**: `events` gets `correlation_id`, `outcome` (`applied`, `rejected`, `pending_approval`, `failed`,
  `informational`), `command_id`, `approval_id`; `log_event` returns the `decision_id`. Timestamps are generated by the
  application to the millisecond and exposed as `ts` (ISO 8601 UTC); the old `timestamp` field keeps its format; order is
  guaranteed by `event_id`.
- **`tenta_comando`** (`app/core/comandi.py`): runs a command from outside the graph with the same checks as the agents
  (allowed devices and values, higher-priority blocks, registered rules) and returns an `EsitoComando` that maps one to one
  to the contract's failure reasons. It never raises; a block is a `rejected` decision, not an error.
- **JSON logs** (`LANGBRAIN_LOG_FORMAT=json`): one line per event with `ts, level, apartment_id, correlation_id, actor, msg`;
  secrets (API keys, tokens, the database password) are masked. Default format unchanged.
- **`LANGBRAIN_REQUIRE_API_KEY=1`**: the server refuses to start without an API key (default `0`).
- **Data retention** (`app/core/conservazione.py`): `LANGBRAIN_RETENTION_EVENTI_GIORNI` and
  `LANGBRAIN_RETENTION_COMANDI_GIORNI` (0 = off, the default; commands are cleaned by whoever holds them through
  `registra_pulizia_comandi`).
- **Approvals** (`app/core/approvazioni.py`): a pending HITL interrupt as an object with a stable `approval_id` (the
  LangGraph interrupt id), `description` and `reason` in Italian, expiry and the action at expiry. `decidi` accepts
  `approve`/`reject` only (no OVERRIDE), returns `already_closed` for a second decision instead of raising, serializes
  concurrent decisions, and records the decision with `approval_id` and the requester as actor.
- **Event bus** (`app/core/bus_eventi.py`, table `stream_events`): `pubblica`, `leggi_da(Last-Event-ID)`, `flusso` (SSE with
  `: ping`). The id is monotonic, persisted and never reused (not even after a restart or a buffer cleanup); the buffer keeps
  at least one hour (`LANGBRAIN_STREAM_BUFFER_ORE`, default 24); only the contract's seven event types are accepted and
  nothing from the graph is published unless a caller does it explicitly.
- **Checked against the real contract file** (1.0.4): `Decision` mapping and `GET /decisions` search with an opaque cursor
  (`app/core/decisioni.py`); the stream envelope `id` is a numeric string; the `Approval` shape (`come_contratto`,
  `leggi_chiusa`) and an `APPROVAL_EXPIRED` event so a late decision is `already_closed`; device description with limits,
  `online`, `last_seen` (`app/core/dispositivi.py`); health pieces (`app/core/salute.py`); missing `old_value`/`new_value` are
  stored as NULL.
- **CI on GitHub**: SQLite on Python 3.12 and 3.14, the whole suite on Postgres 16, Docker image build with startup, gitleaks
  and pip-audit; the `conformita` job is disabled (it lives in HQ's fork).

### Several people at once
- `python -m app.serve` starts the API with several worker processes (`--workers N` or `LANGBRAIN_WORKERS`, default `auto`:
  one per CPU, at most 4; the Docker image uses it). `app/core/multiprocesso.py` shares what lived in the memory of one
  process: the runtime HITL configuration, the value of the simulated effectors and the compiled graph (recompiled when
  another process changed the agents). Background tasks (the HITL timer, the tasks of installed nodes) run in one process at
  a time (a lease on SQLite, with takeover if it dies), HITL operations on one thread exclude each other across processes,
  SQLite runs in WAL mode with a 30 s wait and the startup migrations run one process at a time. **Restart app** works with
  workers. With one process (`python -m uvicorn`) nothing changes and the database is not touched.
- The uvicorn access log no longer writes a line for every successful request (the pages poll the API): errors and
  refusals are still written; `LANGBRAIN_ACCESS_LOG=1` brings back the full log.

### Statistics for charts (generic parameters, any domain)
- `GET /graph/stats` and `GET /graph/runs`: every execution of a graph node is recorded (`node_runs`: duration, outcome
  `ok`, `escalated`, `waiting`, `error`); the API returns runs, outcomes and median/p90 per node, transitions, waits for a
  person and the compiled topology. A run is `config.run_id`, assigned by `POST /graph/run` and kept across a resume.
- `GET /signals` and `GET /signals/stats`: numeric signals per receptor (count, min, max, mean, last value, series over
  time; empty intervals stay empty), with `window_hours`, `receptor_id`, `agent_owner` and `bucket_minutes`.
- `[signals] window_hours` (`[segnali] finestra_ore`, default 4): how many hours of signals the graph keeps in the state of a
  run; numeric signals are also stored in the nervous system.

### Earlier changes of the core
- `DynamicAgent` manages every target of `managed_targets` with one model call per cycle (one `TARGET:` block per target);
  the Brain decides all the escalations of a cycle with one call (numbered `ESCALATION:` blocks) after collecting the
  operator's decisions, so a resume after a pause no longer repeats calls or commands.
- Event log: database errors of `mark_resolved`, `unblock_target` and the TTL expiry are raised; `POST /events/unblock`
  and `DELETE /events/reset-conflicts/{target}` answer 503 and change nothing.
- Prompts of the Brain, troubleshooting, override and routine check configurable in `[prompt]` / `PROMPT_*`; the answer
  format stays a contract of the code; a template with unknown placeholders is refused.
- External organs (`[organismo.attori_esterni]`): actors that write events without being graph nodes get a level and a priority.
- Italian and English: `[general] language`, configuration names in either language (`config.example.toml`), API roles
  with English names (`API_KEY_VIEWER`, `API_KEY_OPERATOR`, `API_KEY_ADMIN`), model answers and operator decisions read in
  both languages, pages in both languages with one strings file per language (`app/static/i18n/`).
- `GET /tools` lists the tools of the process registry too, and `POST /tools` uses a registered tool.
- `tests_windows/`: the tests of `tests/` rerun with Windows-friendly paths, plus the tests written on Windows. The
  known Windows-only failures are SQLite files still open at clean-up (`PermissionError WinError 32`).
