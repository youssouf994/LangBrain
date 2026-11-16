"""Baseline: lo schema com'era prima dell'introduzione di Alembic.

Revisione: 0001
Precedente: nessuna

Questa revisione è congelata: descrive le tabelle di allora con una copia propria delle definizioni (non importa
`app.db.schema`, che cambia), così non muta quando lo schema evolve.

  - database nuovo: crea tutte le tabelle e gli indici;
  - database creato da una versione precedente a Alembic (le tabelle ci sono già, magari con meno colonne): aggiunge le
    colonne e gli indici che mancano, come faceva l'auto-migrazione di prima. Non toglie né cambia nulla.

Da qui in avanti ogni modifica allo schema è una nuova revisione (docs/MIGRAZIONI.md).
"""
from alembic import op
from sqlalchemy import Column, DateTime, Float, Index, Integer, MetaData, String, Table, Text, func, text

from app.core import archivio

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _schema_baseline() -> MetaData:
    metadata = MetaData()
    Table(
        "events", metadata,
        Column("event_id", Integer, primary_key=True, autoincrement=True),
        Column("actor", Text, nullable=False),
        Column("action", Text, nullable=False),
        Column("target", Text, nullable=False),
        Column("old_value", Text),
        Column("new_value", Text),
        Column("reasoning", Text),
        Column("timestamp", DateTime, server_default=func.current_timestamp()),
        Column("escalated", Integer, server_default=text("0")),
        Column("correlation_id", Text),
        Column("outcome", Text),
        Column("command_id", Text),
        Column("approval_id", Text),
        Index("idx_events_target_ts", "target", text("timestamp DESC")),
        Index("idx_events_correlation", "correlation_id"),
        sqlite_autoincrement=True,
    )
    for nome, chiave, origine in (("signals", "signal_id", "receptor_id"), ("readings", "reading_id", "sensor_id")):
        Table(
            nome, metadata,
            Column(chiave, Integer, primary_key=True, autoincrement=True),
            Column(origine, Text, nullable=False),
            Column("agent_owner", Text, nullable=False),
            Column("value", Float, nullable=False),
            Column("unit", Text, nullable=False),
            Column("timestamp", DateTime, server_default=func.current_timestamp()),
            sqlite_autoincrement=True,
        )
    Table(
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
    Table(
        "stream_events", metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("event", Text, nullable=False),
        Column("ts", Text, nullable=False),
        Column("apartment_id", Text, nullable=False, server_default=text("''")),
        Column("correlation_id", Text),
        Column("data", Text, nullable=False, server_default=text("'{}'")),
        sqlite_autoincrement=True,
    )
    Table(
        "idempotenza", metadata,
        Column("ambito", String(255), primary_key=True),
        Column("chiave", String(255), primary_key=True),
        Column("stato", Text, nullable=False),
        Column("risposta", Text),
        Column("creata_il", Float, nullable=False),
        Column("scade_il", Float),
    )
    Table(
        "runtime_state", metadata,
        Column("key", String(255), primary_key=True),
        Column("value", Text, nullable=False),
        Column("version", Integer, nullable=False, server_default=text("1")),
        Column("updated_at", Float, nullable=False),
    )
    Table(
        "leases", metadata,
        Column("name", String(255), primary_key=True),
        Column("owner", Text, nullable=False),
        Column("expires", Float, nullable=False),
    )
    Table(
        "agents_registry", metadata,
        Column("name", String(255), primary_key=True),
        Column("level", Integer, nullable=False, server_default=text("1")),
        Column("parent_agent_name", Text),
        Column("managed_targets", Text, nullable=False),
        Column("sub_agent_names", Text),
        Column("system_prompt_template", Text),
        Column("conflict_window_minutes", Integer, server_default=text("30")),
        Column("priority_weight", Float, server_default=text("1.0")),
        Column("created_at", DateTime, server_default=func.current_timestamp()),
    )
    Table(
        "agent_models", metadata,
        Column("agent_name", String(255), primary_key=True),
        Column("provider", Text, nullable=False),
        Column("model", Text),
        Column("updated_at", DateTime, server_default=func.current_timestamp()),
    )
    Table(
        "hitl_scadenze", metadata,
        Column("thread_id", String(255), primary_key=True),
        Column("interrupt_id", Text, nullable=False),
        Column("tipo", Text),
        Column("creato_il", Float, nullable=False),
        Column("scade_il", Float, nullable=False),
        Column("durata_secondi", Integer, nullable=False),
        Column("stato", Text, nullable=False, server_default=text("'attesa'")),
    )
    return metadata


def upgrade() -> None:
    connessione = op.get_bind()
    metadata = _schema_baseline()
    archivio.crea_tabelle(connessione, metadata)
    archivio.aggiungi_colonne_mancanti(connessione, metadata)


def downgrade() -> None:
    raise NotImplementedError("La baseline non si annulla: cancellerebbe tutti i dati. Ripristina da un backup.")
