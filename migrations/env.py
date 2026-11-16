"""
Ambiente di Alembic per LangBrain.

Due modi di arrivare qui:
  - dall'applicazione (`app/db/migrazioni.py`): passa la connessione già aperta in `config.attributes["connection"]`;
  - dalla riga di comando (`alembic upgrade head`): apre il motore dell'archivio, lo stesso dell'applicazione.
"""

import asyncio
from logging.config import fileConfig

from alembic import context

from app.core import archivio
from app.db import schema

config = context.config
if config.config_file_name is not None and config.attributes.get("connection") is None:
    # Solo da riga di comando: dall'applicazione non si tocca la configurazione dei log.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = schema.metadata
INDICI_CON_ESPRESSIONE = {"idx_events_target_ts"}


def _includi(oggetto, nome, tipo, riflesso, confronto):
    """Le tabelle che il database ha ma lo schema no (es. i checkpoint di LangGraph) non sono nostre: non si toccano."""
    if tipo == "table" and riflesso and nome not in target_metadata.tables:
        return False
    if tipo == "index" and nome in INDICI_CON_ESPRESSIONE:
        return False   # Alembic non sa confrontare un indice con espressione (`timestamp DESC`) e lo segnala sempre come cambiato
    return True


def _esegui(connessione) -> None:
    context.configure(
        connection=connessione,
        target_metadata=target_metadata,
        include_object=_includi,
        compare_type=True,
        render_as_batch=connessione.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _esegui_con_motore() -> None:
    async with archivio.motore().begin() as connessione:
        await connessione.run_sync(_esegui)


def _offline() -> None:
    context.configure(url=archivio.url_database().replace("+aiosqlite", "").replace("+asyncpg", ""),
                      target_metadata=target_metadata, literal_binds=True, include_object=_includi)
    with context.begin_transaction():
        context.run_migrations()


connessione_esterna = config.attributes.get("connection")
if context.is_offline_mode():
    _offline()
elif connessione_esterna is not None:
    _esegui(connessione_esterna)
else:
    asyncio.run(_esegui_con_motore())
