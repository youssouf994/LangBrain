"""
Più processi (worker) che servono la stessa API.

Con `python -m app.serve --workers N` (o `LANGBRAIN_WORKERS=N`) l'API gira in N processi che condividono porta e
database SQLite. Ciò che per natura è unico (i compiti in background, un'azione su una stessa risorsa) e ciò che prima
viveva solo in memoria (la configurazione HITL a runtime, il valore di un effettore simulato, l'interruttore di un nodo, il
grafo compilato) passa da qui:

  - `compito_leader`: un compito in background gira in un solo processo alla volta (un contratto di affitto su SQLite:
    se il processo muore, un altro lo rileva entro pochi secondi);
  - `blocco`: mutua esclusione tra processi su una risorsa (es. la decisione su una pratica);
  - `leggi` / `scrivi` / `versione`: piccolo stato condiviso (tabella `runtime_state`) con un numero di versione, così
    ogni processo si accorge che un altro ha cambiato qualcosa.

Con un solo processo (il caso predefinito) nulla di tutto ciò tocca il database: `condiviso()` è falso, i compiti partono
direttamente e i blocchi non fanno nulla. Perciò i test e il comportamento di sempre non cambiano.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

import aiosqlite
from sqlalchemy import delete, select

from app.core import archivio
from app.db import schema
from app.db.database import DB_PATH

logger = logging.getLogger(__name__)

ASSENTE: Any = object()
DURATA_CONTRATTO_SECONDI = 15.0
_PROCESSO = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
_pronte: set[str] = set()


def lavoratori() -> int:
    """Quanti processi servono l'API (`LANGBRAIN_WORKERS`, impostato da `app.serve`); 1 se non indicato."""
    try:
        return max(1, int(os.getenv("LANGBRAIN_WORKERS", "1")))
    except ValueError:
        return 1


def condiviso() -> bool:
    """True se più processi condividono lo stato (o se `LANGBRAIN_SHARED_STATE=1`, per provarlo con un processo solo)."""
    return lavoratori() > 1 or os.getenv("LANGBRAIN_SHARED_STATE") == "1"


async def _motore(percorso: str | None = None):
    """Il motore dell'archivio, con le tabelle di stato e contratti create alla prima volta (una per database)."""
    percorso = percorso or DB_PATH
    motore = archivio.motore(percorso)
    chiave = archivio.url_database(percorso)
    if chiave not in _pronte:
        dirname = "" if "://" in (percorso or "") else os.path.dirname(percorso or "")
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        async with motore.begin() as db:
            await db.run_sync(archivio.crea_tabelle, schema.metadata, [schema.runtime_state, schema.leases])
        _pronte.add(chiave)
    return motore


async def abilita_wal(db: aiosqlite.Connection) -> None:
    """Con più processi SQLite va in modalità WAL (i lettori non bloccano chi scrive); con uno solo resta com'è."""
    if condiviso():
        await db.execute("PRAGMA journal_mode=WAL")


# ------------------------------------------------------------------------------------------ stato condiviso

async def leggi(chiave: str, default: Any = None, percorso: str | None = None) -> Any:
    motore = await _motore(percorso)
    async with motore.connect() as db:
        riga = (await db.execute(select(schema.runtime_state.c.value).where(schema.runtime_state.c.key == chiave))).first()
    return default if riga is None else json.loads(riga[0])


async def scrivi(chiave: str, valore: Any, percorso: str | None = None) -> int:
    """Scrive il valore e restituisce la nuova versione (cresce a ogni scrittura)."""
    motore = await _motore(percorso)
    tabella = schema.runtime_state
    inserimento = archivio.inserimento(motore, tabella).values(
        key=chiave, value=json.dumps(valore, default=str), version=1, updated_at=time.time())
    inserimento = inserimento.on_conflict_do_update(
        index_elements=[tabella.c.key],
        set_={"value": inserimento.excluded.value, "version": tabella.c.version + 1, "updated_at": inserimento.excluded.updated_at},
    ).returning(tabella.c.version)
    async with motore.begin() as db:
        return (await db.execute(inserimento)).scalar_one()


async def versioni(*chiavi: str, percorso: str | None = None) -> dict[str, int]:
    """La versione di ciascuna chiave (0 se non è mai stata scritta)."""
    motore = await _motore(percorso)
    tabella = schema.runtime_state
    async with motore.connect() as db:
        trovate = dict((await db.execute(select(tabella.c.key, tabella.c.version).where(tabella.c.key.in_(chiavi)))).all())
    return {c: trovate.get(c, 0) for c in chiavi}


# ------------------------------------------------------------------------------------------ contratti (leader e blocchi)

async def _acquisisci(nome: str, proprietario: str, durata: float, percorso: str | None = None) -> bool:
    """
    Prende (o rinnova) il contratto `nome` se è libero, scaduto o già nostro. Un'unica istruzione atomica: inserisce il
    contratto, oppure lo riscrive solo se è scaduto o già nostro (e in tal caso restituisce la riga); se è di un altro e
    ancora valido non cambia nulla e non restituisce nulla. Vale uguale su SQLite e su Postgres, senza transazioni esplicite.
    """
    motore = await _motore(percorso)
    tabella = schema.leases
    adesso = time.time()
    inserimento = archivio.inserimento(motore, tabella).values(name=nome, owner=proprietario, expires=adesso + durata)
    inserimento = inserimento.on_conflict_do_update(
        index_elements=[tabella.c.name],
        set_={"owner": inserimento.excluded.owner, "expires": inserimento.excluded.expires},
        where=(tabella.c.expires <= adesso) | (tabella.c.owner == proprietario),
    ).returning(tabella.c.name)
    async with motore.begin() as db:
        return (await db.execute(inserimento)).first() is not None


async def _rilascia(nome: str, proprietario: str, percorso: str | None = None) -> None:
    motore = await _motore(percorso)
    async with motore.begin() as db:
        await db.execute(delete(schema.leases).where(schema.leases.c.name == nome, schema.leases.c.owner == proprietario))


async def compito_leader(nome: str, fabbrica: Callable[[], Awaitable[None]], durata: float = DURATA_CONTRATTO_SECONDI,
                         percorso: str | None = None, proprietario: str | None = None) -> None:
    """
    Esegue `fabbrica()` in un solo processo alla volta. Con un processo solo la esegue e basta. Con più processi chi
    ottiene il contratto `nome` la lancia e lo rinnova; gli altri riprovano ogni `durata / 3` secondi e subentrano se il
    leader sparisce. Se il contratto si perde il compito viene fermato.
    """
    if not condiviso():
        await fabbrica()
        return
    contratto = f"leader:{nome}"
    proprietario = proprietario or _PROCESSO
    compito: asyncio.Task | None = None
    try:
        while True:
            try:
                avuto = await _acquisisci(contratto, proprietario, durata, percorso)
            except Exception as errore:  # il database non risponde: non si può essere leader
                logger.warning("Contratto %s non rinnovato: %s", nome, errore)
                avuto = False
            if avuto and compito is None:
                logger.info("Questo processo (%s) esegue il compito %s.", os.getpid(), nome)
                compito = asyncio.create_task(fabbrica())
            elif avuto and compito.done() and (compito.cancelled() or compito.exception()):
                logger.error("Il compito %s si è fermato (%s): riparte.", nome, compito.exception() or "annullato")
                compito = asyncio.create_task(fabbrica())
            elif not avuto and compito is not None:
                logger.warning("Contratto %s perso: il compito si ferma in questo processo.", nome)
                compito.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await compito
                compito = None
            await asyncio.sleep(durata / 3)
    finally:
        if compito is not None:
            compito.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await compito
            with contextlib.suppress(Exception):
                await _rilascia(contratto, proprietario, percorso)


@contextlib.asynccontextmanager
async def blocco(nome: str, attesa: float = 60.0, durata: float = 120.0, percorso: str | None = None) -> AsyncIterator[None]:
    """
    Mutua esclusione tra processi su `nome` (es. `pratica:p_1`). Con un processo solo non fa nulla. Se non si ottiene entro
    `attesa` secondi solleva TimeoutError; il contratto scade da solo dopo `durata` secondi se il processo muore.
    """
    if not condiviso():
        yield
        return
    contratto = f"blocco:{nome}"
    mio = f"{_PROCESSO}:{uuid.uuid4().hex[:6]}"
    limite = time.monotonic() + attesa
    pausa = 0.02
    while not await _acquisisci(contratto, mio, durata, percorso):
        if time.monotonic() >= limite:
            raise TimeoutError(f"Risorsa '{nome}' occupata da un altro processo.")
        await asyncio.sleep(pausa)
        pausa = min(pausa * 1.5, 0.25)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await _rilascia(contratto, mio, percorso)
