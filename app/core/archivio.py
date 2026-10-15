"""
Archivio: l'unico punto da cui il core (e i nodi) aprono il database.

SQLite resta il predefinito (un file, `DB_PATH`); con `DATABASE_URL=postgresql://...` lo stesso codice gira su Postgres
(Scaleway, Supabase, RDS...). Le query si scrivono con SQLAlchemy Core: i valori viaggiano sempre come parametri legati dal
driver, mai incollati nel testo SQL, e segnaposto, upsert e tipi sono tradotti dal dialetto, non da codice nostro.

  - `url_database(percorso)`: da un percorso di file (o da un URL) all'URL SQLAlchemy asincrono.
  - `motore(percorso)`: l'`AsyncEngine` per quel database (uno per URL, riusato).
  - `chiudi_motori()`: chiude tutti i motori (spegnimento dell'applicazione, fine dei test).
  - `e_postgres(percorso)`: per le poche scelte che dipendono dal DBMS.
  - `url_per_psycopg(percorso)`: URL nel formato di psycopg, per il checkpointer LangGraph.

Un nodo non scrive mai una stringa di connessione: chiede il motore all'archivio. Il segreto dell'URL non finisce nei log
(`url_mascherato`).
"""

import logging
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

# Attesa massima (secondi) su un database bloccato da un'altra connessione, prima di sollevare "database is locked".
TIMEOUT_DB_SECONDI = 30.0

_motori: dict[str, AsyncEngine] = {}


def _e_url(valore: str) -> bool:
    return "://" in valore


def url_configurato() -> str | None:
    """`DATABASE_URL` se impostato (e non vuoto), altrimenti None: il database è il file SQLite di `DB_PATH`."""
    valore = (os.getenv("DATABASE_URL") or "").strip()
    return valore or None


def url_database(percorso: str | None = None) -> str:
    """
    L'URL SQLAlchemy asincrono del database.
      - `percorso` è un URL (`postgresql://...`, `sqlite:///...`): è quello;
      - `percorso` è il file predefinito (`DB_PATH`) o manca, e `DATABASE_URL` è impostato: è `DATABASE_URL`;
      - altrimenti `percorso` è un file SQLite.
    """
    from app.db.database import DB_PATH  # import tardivo: database.py importa questo modulo

    percorso = percorso or DB_PATH
    if _e_url(percorso):
        grezzo = percorso
    elif percorso == DB_PATH and url_configurato():
        grezzo = url_configurato()
    else:
        # Su Windows i `\` rompono l'URL: si usa `/`, che va bene ovunque.
        return "sqlite+aiosqlite:///" + os.path.abspath(percorso).replace("\\", "/")
    return _normalizza(grezzo)


def _normalizza(url: str) -> str:
    """Porta l'URL al driver asincrono giusto (`postgresql://` -> `postgresql+asyncpg://`, `sqlite://` -> `sqlite+aiosqlite://`)."""
    schema, resto = url.split("://", 1)
    base = schema.split("+", 1)[0].lower()
    if base in ("postgres", "postgresql"):
        return "postgresql+asyncpg://" + _traduci_ssl(resto)
    if base == "sqlite":
        return "sqlite+aiosqlite://" + resto
    raise ValueError(f"Database non supportato: '{base}' (solo sqlite e postgresql).")


def _traduci_ssl(resto: str) -> str:
    """asyncpg non conosce `sslmode=` di libpq: lo traduce nel parametro `ssl=` che capisce."""
    parti = urlsplit("//" + resto)
    domanda = [(k, v) for k, v in parse_qsl(parti.query, keep_blank_values=True)]
    tradotta = []
    for chiave, valore in domanda:
        if chiave == "sslmode":
            tradotta.append(("ssl", "false" if valore == "disable" else valore))
        else:
            tradotta.append((chiave, valore))
    return urlunsplit(("", parti.netloc, parti.path, urlencode(tradotta), ""))[2:]


def e_postgres(percorso: str | None = None) -> bool:
    return url_database(percorso).startswith("postgresql")


def url_per_psycopg(percorso: str | None = None) -> str:
    """URL `postgresql://...` per psycopg (il checkpointer LangGraph usa quello), con `sslmode` di libpq."""
    url = make_url(url_database(percorso))
    if not url.drivername.startswith("postgresql"):
        raise ValueError("L'URL di psycopg esiste solo per Postgres.")
    query = dict(url.query)
    if "ssl" in query:
        valore = query.pop("ssl")
        query["sslmode"] = "disable" if valore == "false" else valore
    return url.set(drivername="postgresql", query=query).render_as_string(hide_password=False)


def url_mascherato(percorso: str | None = None) -> str:
    """L'URL senza la password, adatto ai log."""
    return make_url(url_database(percorso)).render_as_string(hide_password=True)


def motore(percorso: str | None = None) -> AsyncEngine:
    """
    Il motore asincrono del database indicato (uno per URL). SQLite: nessun pool, così ogni operazione chiude il file
    (su Windows un file aperto impedisce di cancellarlo) e, con più processi, `journal_mode=WAL`. Postgres: pool con `pre_ping`.
    """
    url = url_database(percorso)
    esistente = _motori.get(url)
    if esistente is not None:
        return esistente

    if url.startswith("sqlite"):
        nuovo = create_async_engine(url, poolclass=NullPool, connect_args={"timeout": TIMEOUT_DB_SECONDI})
        _attiva_pragma_sqlite(nuovo)
    else:
        nuovo = create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    _motori[url] = nuovo
    logger.info("[Archivio] Motore creato: %s", make_url(url).render_as_string(hide_password=True))
    return nuovo


def _attiva_pragma_sqlite(motore_sqlite: AsyncEngine) -> None:
    from app.core import multiprocesso  # import tardivo: multiprocesso usa l'archivio

    @event.listens_for(motore_sqlite.sync_engine, "connect")
    def _al_collegamento(connessione_dbapi, _record):
        cursore = connessione_dbapi.cursor()
        try:
            # Con più processi i lettori non devono bloccare chi scrive; con uno solo il file resta com'è.
            if multiprocesso.condiviso():
                cursore.execute("PRAGMA journal_mode=WAL")
            cursore.execute(f"PRAGMA busy_timeout={int(TIMEOUT_DB_SECONDI * 1000)}")
        finally:
            cursore.close()


def inserimento(motore_db: AsyncEngine, tabella):
    """
    Un `INSERT` che supporta `on_conflict_do_update` / `on_conflict_do_nothing` / `returning`, nella variante del DBMS del
    motore (SQLite e Postgres hanno la stessa interfaccia, ma classi diverse).
    """
    if motore_db.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(tabella)


# Chiave del blocco di Postgres che serializza le creazioni di tabelle tra processi (valore qualunque, fisso).
_CHIAVE_BLOCCO_SCHEMA = 7_361_204_001


def _blocca_schema(connessione_sincrona) -> None:
    """Su Postgres: blocco fino a fine transazione, così due processi non creano la stessa tabella insieme (altrimenti
    `CREATE TABLE` fallisce con `pg_type_typname_nsp_index`). SQLite serializza da sé."""
    if connessione_sincrona.dialect.name == "postgresql":
        connessione_sincrona.exec_driver_sql(f"SELECT pg_advisory_xact_lock({_CHIAVE_BLOCCO_SCHEMA})")


def crea_tabelle(connessione_sincrona, metadata, tabelle=None) -> None:
    """
    Crea le tabelle e gli indici di `metadata` (o solo `tabelle`) che mancano; da usare con
    `await conn.run_sync(crea_tabelle, metadata)`. Sicura con più processi che partono insieme.
    """
    _blocca_schema(connessione_sincrona)
    try:
        metadata.create_all(connessione_sincrona, tables=tabelle, checkfirst=True)
    except OperationalError as errore:
        # SQLite non ha un blocco di schema: un altro processo ha creato la tabella tra il controllo e la creazione.
        if "already exists" not in str(errore):
            raise
        metadata.create_all(connessione_sincrona, tables=tabelle, checkfirst=True)  # ora vede la tabella e crea il resto


def aggiungi_colonne_mancanti(connessione_sincrona, metadata) -> list[str]:
    """
    Migrazione additiva: aggiunge a ogni tabella esistente le colonne che lo schema prevede e il database non ha ancora
    (un database creato da una versione precedente). Non toglie né cambia mai nulla. Restituisce `tabella.colonna` aggiunte.
    """
    from sqlalchemy import inspect
    from sqlalchemy.schema import CreateColumn

    _blocca_schema(connessione_sincrona)
    ispettore = inspect(connessione_sincrona)
    aggiunte: list[str] = []
    for tabella in metadata.sorted_tables:
        if not ispettore.has_table(tabella.name):
            continue
        presenti = {c["name"] for c in ispettore.get_columns(tabella.name)}
        for colonna in tabella.columns:
            if colonna.name in presenti:
                continue
            definizione = CreateColumn(colonna).compile(dialect=connessione_sincrona.dialect)
            nome_tabella = connessione_sincrona.dialect.identifier_preparer.format_table(tabella)
            connessione_sincrona.exec_driver_sql(f"ALTER TABLE {nome_tabella} ADD COLUMN {definizione}")
            aggiunte.append(f"{tabella.name}.{colonna.name}")
            logger.info("Migrazione DB: aggiunta colonna '%s' a '%s'.", colonna.name, tabella.name)
    return aggiunte


async def chiudi_motori() -> None:
    """Chiude tutti i motori aperti. Sicura da chiamare più volte."""
    motori = list(_motori.values())
    _motori.clear()
    for m in motori:
        await m.dispose()
