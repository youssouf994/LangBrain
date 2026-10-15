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
- `DATABASE_URL=postgresql://...` (Scaleway, Supabase, RDS...) moves the whole database to Postgres; without it nothing
  changes: SQLite in the file `DB_PATH`. `sslmode=` in the URL is understood.
- Everything goes through `app/core/archivio.py` (SQLAlchemy Core, schema in `app/db/schema.py`): values are always bound
  parameters, never pasted into SQL, and upserts/types are translated by the dialect. This covers events, signals, node
  runs, the agent registry, per-agent models, HITL timers, the shared state and leases of the workers, the scenarios and
  `DELETE /system/reset`. A node asks the archivio for the engine and never writes a connection string.
- The LangGraph checkpointer uses `AsyncPostgresSaver` on a connection pool when the URL is Postgres.
- Database errors are now `sqlalchemy.exc.SQLAlchemyError` (no longer `aiosqlite.Error`). Old SQLite files are migrated
  additively (missing columns are added) as before.
- Tests on a real Postgres run only with `TEST_DATABASE_URL` set (`tests_windows/test_win_postgres.py`, a throw-away
  database: its tables are dropped).
- `requirements.txt` / `requirements.lock`: SQLAlchemy, Alembic, asyncpg, psycopg, `langgraph-checkpoint-postgres`.

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
