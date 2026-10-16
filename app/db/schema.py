"""
Schema del database del core (SQLAlchemy Core): lo stesso su SQLite e su Postgres.

Le colonne mantengono i tipi di sempre (`escalated` resta 0/1, i testi restano testi) così le risposte dell'API non cambiano.
Il timestamp degli eventi e dei segnali è UTC; `come_testo` lo riporta al formato storico `YYYY-MM-DD HH:MM:SS`.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Index, Integer, MetaData, String, Table, Text, func, text

metadata = MetaData()


def adesso_utc() -> datetime:
    """Ora UTC senza fuso, al millisecondo (le colonne sono `timestamp without time zone`). La genera l'applicazione."""
    adesso = datetime.now(timezone.utc)
    return adesso.replace(tzinfo=None, microsecond=adesso.microsecond // 1000 * 1000)


def iso_ms(valore):
    """Un timestamp in ISO 8601 UTC con millisecondi (`2026-10-14T13:02:11.420Z`); i testi dell'altro formato si convertono."""
    if isinstance(valore, str):
        try:
            valore = datetime.fromisoformat(valore)
        except ValueError:
            return valore
    if isinstance(valore, datetime):
        if valore.tzinfo is not None:
            valore = valore.astimezone(timezone.utc).replace(tzinfo=None)
        return valore.strftime("%Y-%m-%dT%H:%M:%S.") + f"{valore.microsecond // 1000:03d}Z"
    return valore


def come_testo(valore) -> str | None:
    """Un timestamp nel formato storico (`2026-10-15 10:30:00`); i testi già in quel formato restano come sono."""
    if isinstance(valore, datetime):
        return valore.strftime("%Y-%m-%d %H:%M:%S")
    return valore


# Esiti ammessi di una decisione (colonna `events.outcome`).
ESITI_DECISIONE = ("applied", "rejected", "pending_approval", "failed", "informational")

events = Table(
    "events", metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column("actor", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("target", Text, nullable=False),
    Column("old_value", Text),
    Column("new_value", Text),
    Column("reasoning", Text),
    Column("timestamp", DateTime, default=adesso_utc, server_default=func.current_timestamp()),
    Column("escalated", Integer, default=0, server_default=text("0")),
    # Campi del registro delle decisioni (contratto 1.0.4): facoltativi, le righe vecchie li hanno vuoti.
    Column("correlation_id", Text),
    Column("outcome", Text),
    Column("command_id", Text),
    Column("approval_id", Text),
    Index("idx_events_target_ts", "target", text("timestamp DESC")),
    Index("idx_events_correlation", "correlation_id"),
    sqlite_autoincrement=True,
)

signals = Table(
    "signals", metadata,
    Column("signal_id", Integer, primary_key=True, autoincrement=True),
    Column("receptor_id", Text, nullable=False),
    Column("agent_owner", Text, nullable=False),
    Column("value", Float, nullable=False),
    Column("unit", Text, nullable=False),
    Column("timestamp", DateTime, default=adesso_utc, server_default=func.current_timestamp()),
    sqlite_autoincrement=True,
)

# Nome storico della tabella dei segnali: accettato come alias (vuoto e non usato) finché esistono i vecchi test.
readings = Table(
    "readings", metadata,
    Column("reading_id", Integer, primary_key=True, autoincrement=True),
    Column("sensor_id", Text, nullable=False),
    Column("agent_owner", Text, nullable=False),
    Column("value", Float, nullable=False),
    Column("unit", Text, nullable=False),
    Column("timestamp", DateTime, default=adesso_utc, server_default=func.current_timestamp()),
    sqlite_autoincrement=True,
)

# Esecuzioni dei nodi del grafo (traccia per le statistiche di `GET /graph/stats`)
node_runs = Table(
    "node_runs", metadata,
    Column("run_no", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Text, nullable=False),
    Column("thread_id", Text, nullable=False, server_default=text("''")),
    Column("node", Text, nullable=False),
    Column("started_at", Text, nullable=False),
    Column("duration_ms", Float, nullable=False, server_default=text("0")),
    Column("outcome", Text, nullable=False),
    Column("detail", Text, nullable=False, server_default=text("''")),
    Index("idx_node_runs_run", "run_id", "run_no"),
    Index("idx_node_runs_started", "started_at"),
    sqlite_autoincrement=True,
)

# Bus degli eventi in tempo reale (vedi `app/core/bus_eventi.py`): l'id cresce sempre e non viene mai riusato.
stream_events = Table(
    "stream_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event", Text, nullable=False),
    Column("ts", Text, nullable=False),
    Column("apartment_id", Text, nullable=False, server_default=text("''")),
    Column("correlation_id", Text),
    Column("data", Text, nullable=False, server_default=text("'{}'")),
    sqlite_autoincrement=True,
)

# Stato condiviso tra processi e contratti di affitto (vedi `app/core/multiprocesso.py`)
runtime_state = Table(
    "runtime_state", metadata,
    Column("key", String(255), primary_key=True),
    Column("value", Text, nullable=False),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("updated_at", Float, nullable=False),
)

leases = Table(
    "leases", metadata,
    Column("name", String(255), primary_key=True),
    Column("owner", Text, nullable=False),
    Column("expires", Float, nullable=False),
)

# Registro degli agenti creati a runtime (vedi `app/agents/agent_registry.py`); `managed_targets` e `sub_agent_names` sono JSON in testo.
agents_registry = Table(
    "agents_registry", metadata,
    Column("name", String(255), primary_key=True),
    Column("level", Integer, nullable=False, server_default=text("1")),
    Column("parent_agent_name", Text),
    Column("managed_targets", Text, nullable=False),
    Column("sub_agent_names", Text),
    Column("system_prompt_template", Text),
    Column("conflict_window_minutes", Integer, server_default=text("30")),
    Column("priority_weight", Float, server_default=text("1.0")),
    Column("created_at", DateTime, default=adesso_utc, server_default=func.current_timestamp()),
)

# Modello LLM proprio di un agente (vedi `app/core/modelli_agenti.py`); i nomi si confrontano senza distinguere le maiuscole.
agent_models = Table(
    "agent_models", metadata,
    Column("agent_name", String(255), primary_key=True),
    Column("provider", Text, nullable=False),
    Column("model", Text),
    Column("updated_at", DateTime, default=adesso_utc, server_default=func.current_timestamp()),
)

# Scadenze dei timer HITL (vedi `app/graph/timer_hitl.py`); gli istanti sono epoche in secondi.
hitl_scadenze = Table(
    "hitl_scadenze", metadata,
    Column("thread_id", String(255), primary_key=True),
    Column("interrupt_id", Text, nullable=False),
    Column("tipo", Text),
    Column("creato_il", Float, nullable=False),
    Column("scade_il", Float, nullable=False),
    Column("durata_secondi", Integer, nullable=False),
    Column("stato", Text, nullable=False, server_default=text("'attesa'")),
)
