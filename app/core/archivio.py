"""
Archivio: l'unico punto da cui il core (e i nodi) aprono il database.

SQLite resta il predefinito (un file, `DB_PATH`); con `LANGBRAIN_DB_BACKEND=postgres` e `LANGBRAIN_DB_URL=postgresql://...` lo stesso codice gira su Postgres
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


class ErroreConfigurazioneDb(RuntimeError):
    """La scelta del database è incompleta o incoerente: l'avvio si ferma con un messaggio chiaro."""


BACKEND_SQLITE = "sqlite"
BACKEND_POSTGRES = "postgres"
_ALIAS_BACKEND = {"postgresql": BACKEND_POSTGRES, "pg": BACKEND_POSTGRES}


def _e_url(valore: str) -> bool:
    return "://" in valore


def backend() -> str:
    """Il database scelto con `LANGBRAIN_DB_BACKEND`: `sqlite` (predefinito) o `postgres`."""
    grezzo = (os.getenv("LANGBRAIN_DB_BACKEND") or BACKEND_SQLITE).strip().lower()
    scelto = _ALIAS_BACKEND.get(grezzo, grezzo)
    if scelto not in (BACKEND_SQLITE, BACKEND_POSTGRES):
        raise ErroreConfigurazioneDb(
            f"LANGBRAIN_DB_BACKEND='{grezzo}' non è valido: usa 'sqlite' (predefinito) oppure 'postgres'.")
    return scelto


def url_configurato() -> str | None:
    """
    L'URL di Postgres (`LANGBRAIN_DB_URL`) se il backend è `postgres`, altrimenti None: il database è il file SQLite di
    `DB_PATH`. Con `postgres` e senza URL solleva `ErroreConfigurazioneDb`.
    """
    if backend() != BACKEND_POSTGRES:
        return None
    valore = (os.getenv("LANGBRAIN_DB_URL") or "").strip()
    if not valore:
        raise ErroreConfigurazioneDb(
            "LANGBRAIN_DB_BACKEND=postgres richiede LANGBRAIN_DB_URL (es. postgresql://utente:password@host:5432/database): "
            "impostalo, oppure torna a SQLite togliendo LANGBRAIN_DB_BACKEND.")
    return valore


def verifica_configurazione() -> None:
    """
    Da chiamare all'avvio: rifiuta una scelta di database incompleta (backend sconosciuto, `postgres` senza URL o con un
    URL che non è Postgres). Con SQLite non c'è nulla da verificare.
    """
    url = url_configurato()
    if url is not None:
        url_database(_db_path_predefinito())  # solleva se l'URL non è di un database supportato
        if not e_postgres():
            raise ErroreConfigurazioneDb("LANGBRAIN_DB_URL deve essere un URL postgresql://...")
    elif (os.getenv("LANGBRAIN_DB_URL") or "").strip():
        logger.warning("LANGBRAIN_DB_URL è impostato ma LANGBRAIN_DB_BACKEND non è 'postgres': si usa SQLite (%s).",
                       _db_path_predefinito())


def _db_path_predefinito() -> str:
    from app.db.database import DB_PATH  # import tardivo: database.py importa questo modulo
    return DB_PATH


def url_database(percorso: str | None = None) -> str:
    """
    L'URL SQLAlchemy asincrono del database.
      - `percorso` è un URL (`postgresql://...`, `sqlite:///...`): è quello;
      - `percorso` è il file predefinito (`DB_PATH`) o manca, e il backend è `postgres`: è `LANGBRAIN_DB_URL`;
      - altrimenti `percorso` è un file SQLite.
    """
    percorso = percorso or _db_path_predefinito()
    if _e_url(percorso):
        grezzo = percorso
    elif percorso == _db_path_predefinito() and url_configurato():
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


# Parametro dell'URL di Postgres che isola il database in uno schema (`?lb_schema=nome`): serve ai test, che danno a ogni
# database temporaneo il suo schema nello stesso database Postgres, e a chi vuole più istanze nello stesso database.
PARAMETRO_SCHEMA = "lb_schema"


def schema_dell_url(url: str) -> str | None:
    return make_url(url).query.get(PARAMETRO_SCHEMA)


def e_postgres(percorso: str | None = None) -> bool:
    return url_database(percorso).startswith("postgresql")


def url_per_psycopg(percorso: str | None = None) -> str:
    """URL `postgresql://...` per psycopg (il checkpointer LangGraph usa quello), con `sslmode` di libpq."""
    url = make_url(url_database(percorso))
    if not url.drivername.startswith("postgresql"):
        raise ValueError("L'URL di psycopg esiste solo per Postgres.")
    query = dict(url.query)
    schema = query.pop(PARAMETRO_SCHEMA, None)
    if schema:
        query["options"] = f"-csearch_path={schema}"
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
        schema = schema_dell_url(url)
        url_driver = make_url(url).difference_update_query([PARAMETRO_SCHEMA]) if schema else url
        argomenti = {"server_settings": {"search_path": schema}} if schema else {}
        nuovo = create_async_engine(url_driver, pool_pre_ping=True, pool_size=5, max_overflow=5, connect_args=argomenti)
        if schema:
            _crea_schema_al_collegamento(nuovo, schema)
    _motori[url] = nuovo
    logger.info("[Archivio] Motore creato: %s", make_url(url).render_as_string(hide_password=True))
    return nuovo


def _crea_schema_al_collegamento(motore_pg: AsyncEngine, schema: str) -> None:
    @event.listens_for(motore_pg.sync_engine, "connect")
    def _al_collegamento(connessione_dbapi, _record):
        cursore = connessione_dbapi.cursor()
        try:
            # Con più processi che si collegano insieme `CREATE SCHEMA IF NOT EXISTS` può comunque collidere: un blocco lo serializza.
            cursore.execute(f"SELECT pg_advisory_lock({_CHIAVE_BLOCCO_SCHEMA})")
            try:
                cursore.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')  # `schema` è un identificatore nostro (esadecimale)
            finally:
                cursore.execute(f"SELECT pg_advisory_unlock({_CHIAVE_BLOCCO_SCHEMA})")
        finally:
            cursore.close()


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
    aggiunte.extend(_crea_indici_mancanti(connessione_sincrona, metadata, ispettore))
    return aggiunte


def _crea_indici_mancanti(connessione_sincrona, metadata, ispettore) -> list[str]:
    """Gli indici dello schema che una tabella già esistente non ha ancora (un database creato da una versione precedente)."""
    creati: list[str] = []
    for tabella in metadata.sorted_tables:
        if not ispettore.has_table(tabella.name):
            continue
        presenti = {i["name"] for i in ispettore.get_indexes(tabella.name)}
        for indice in tabella.indexes:
            if indice.name not in presenti:
                indice.create(connessione_sincrona, checkfirst=True)
                creati.append(f"indice {indice.name}")
                logger.info("Migrazione DB: creato l'indice '%s' su '%s'.", indice.name, tabella.name)
    return creati


async def chiudi_motori() -> None:
    """Chiude tutti i motori aperti. Sicura da chiamare più volte."""
    motori = list(_motori.values())
    _motori.clear()
    for m in motori:
        await m.dispose()
