"""
Checkpointer LangGraph persistente.

Con `LANGBRAIN_DB_BACKEND=postgres` (URL in `LANGBRAIN_DB_URL`) usa `AsyncPostgresSaver` su un pool di connessioni; altrimenti `AsyncSqliteSaver` sullo
stesso file del database applicativo (`DB_PATH`). In entrambi i casi le tabelle dei checkpoint conservano thread, stato e
interrupt HITL pendenti, che sopravvivono a riavvii e ricompilazioni del grafo.
La connessione (o il pool) è dedicata al checkpointer e va aperta nel lifespan dell'applicazione e chiusa allo spegnimento
(un thread aiosqlite non chiuso impedisce l'uscita del processo).

Con più processi (`python -m app.serve --workers N`) lo stato che prima stava solo in memoria (configurazione HITL,
valore degli effettori simulati, grafo compilato) è condiviso: vedi `app/core/multiprocesso.py`.

Nota per Windows: psycopg asincrono non funziona con il ProactorEventLoop; per Postgres in locale su Windows serve il
SelectorEventLoop (`asyncio.WindowsSelectorEventLoopPolicy`). Su Linux (Docker, server) non c'è nulla da fare.
"""

import logging
from typing import Any

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core import archivio, multiprocesso
from app.db.database import DB_PATH

logger = logging.getLogger(__name__)

# Attesa massima (secondi) su un DB bloccato da un'altra connessione, prima di sollevare "database is locked".
TIMEOUT_DB_SECONDI = 30.0

# Tabelle del checkpointer Postgres (quelle di SQLite sono `checkpoints` e `writes`).
_TABELLE_POSTGRES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")

_connessione: aiosqlite.Connection | None = None
_pool: Any | None = None  # psycopg_pool.AsyncConnectionPool, importato solo se serve
_saver: Any | None = None  # AsyncSqliteSaver | AsyncPostgresSaver
_percorso_aperto: str | None = None


async def apri_checkpointer(db_path: str = DB_PATH):
    """
    Apre il checkpointer sul database indicato e crea le tabelle se mancano.
    Chiamarla di nuovo con lo stesso database restituisce l'istanza già aperta.
    """
    global _connessione, _pool, _saver, _percorso_aperto
    if _saver is not None and _percorso_aperto == db_path:
        return _saver
    if _saver is not None:
        await chiudi_checkpointer()

    connessione = pool = None
    if archivio.e_postgres(db_path):
        saver, pool = await _apri_postgres(db_path)
    else:
        saver, connessione = await _apri_sqlite(db_path)

    _connessione, _pool, _saver, _percorso_aperto = connessione, pool, saver, db_path
    logger.info("[Checkpointer] Persistenza LangGraph attiva su %s", archivio.url_mascherato(db_path))
    return saver


async def _apri_sqlite(db_path: str):
    connessione = await aiosqlite.connect(db_path, timeout=TIMEOUT_DB_SECONDI)
    try:
        await multiprocesso.abilita_wal(connessione)
        saver = AsyncSqliteSaver(connessione)
        await saver.setup()
    except Exception:
        await connessione.close()
        raise
    return saver, connessione


async def _apri_postgres(db_path: str):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    # Le impostazioni richieste da AsyncPostgresSaver: autocommit, nessuna istruzione preparata, righe come dizionari.
    pool = AsyncConnectionPool(conninfo=archivio.url_per_psycopg(db_path), min_size=1, max_size=5, open=False,
                               kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    try:
        await pool.open(wait=True, timeout=TIMEOUT_DB_SECONDI)
        schema = archivio.schema_dell_url(archivio.url_database(db_path))
        if schema:  # database isolato in uno schema (test): lo schema esiste prima che il checkpointer crei le tabelle
            async with pool.connection() as conn:
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
    except Exception:
        await pool.close()
        raise
    return saver, pool


async def chiudi_checkpointer() -> None:
    """Chiude la connessione del checkpointer. Sicura da chiamare anche se non è stato aperto."""
    global _connessione, _pool, _saver, _percorso_aperto
    connessione, pool = _connessione, _pool
    _connessione = _pool = _saver = _percorso_aperto = None
    if connessione is not None:
        await connessione.close()
    if pool is not None:
        await pool.close()


def get_checkpointer():
    """Restituisce il checkpointer aperto, oppure None se `apri_checkpointer()` non è stata chiamata."""
    return _saver


async def svuota_checkpoint() -> None:
    """Elimina tutti i thread persistiti (usato dal reset di sistema)."""
    if _saver is None:
        return
    if _pool is not None:
        async with _pool.connection() as conn:
            for tabella in _TABELLE_POSTGRES:
                await conn.execute(f"DELETE FROM {tabella}")  # nomi fissi qui sopra, nessun valore esterno
        return
    async with _saver.lock:
        await _saver.conn.execute("DELETE FROM writes")
        await _saver.conn.execute("DELETE FROM checkpoints")
        await _saver.conn.commit()
