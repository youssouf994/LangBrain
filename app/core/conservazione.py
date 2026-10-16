"""
Conservazione dei dati: pulizia periodica di ciò che è più vecchio del tempo dichiarato.

  - `LANGBRAIN_RETENTION_EVENTI_GIORNI`: giorni di conservazione del registro delle decisioni (tabella `events`).
  - `LANGBRAIN_RETENTION_COMANDI_GIORNI`: giorni di conservazione dei comandi. Il nucleo non ha una tabella dei comandi:
    li tiene chi li crea (un nodo/adattatore), che registra la propria pulizia con `registra_pulizia`.
  - `LANGBRAIN_RETENTION_INTERVALLO_ORE`: ogni quante ore gira la pulizia (predefinito 6).

`0` o variabile assente = pulizia disattivata (predefinito): il nucleo non cancella mai dati che nessuno gli ha chiesto di
cancellare. Il compose del PoC imposta 90 e 7. La pulizia gira in un solo processo alla volta (`compito_leader`) e un
errore in una pulizia non ferma le altre né il ciclo.
"""

import asyncio
import logging
import os
from datetime import timedelta
from typing import Awaitable, Callable

from sqlalchemy import delete

from app.core import archivio, bus_eventi
from app.db import schema

logger = logging.getLogger(__name__)

VARIABILE_EVENTI = "LANGBRAIN_RETENTION_EVENTI_GIORNI"
VARIABILE_COMANDI = "LANGBRAIN_RETENTION_COMANDI_GIORNI"
VARIABILE_INTERVALLO = "LANGBRAIN_RETENTION_INTERVALLO_ORE"
INTERVALLO_PREDEFINITO_ORE = 6.0

# Una pulizia riceve i giorni di conservazione e restituisce quante righe ha tolto.
Pulizia = Callable[[int], Awaitable[int]]
_pulizie_comandi: dict[str, Pulizia] = {}


def _giorni(variabile: str) -> int:
    grezzo = (os.getenv(variabile) or "0").strip()
    try:
        return max(0, int(grezzo))
    except ValueError:
        logger.warning("%s='%s' non è un numero intero di giorni: pulizia disattivata.", variabile, grezzo)
        return 0


def giorni_eventi() -> int:
    return _giorni(VARIABILE_EVENTI)


def giorni_comandi() -> int:
    return _giorni(VARIABILE_COMANDI)


def intervallo_secondi() -> float:
    try:
        ore = float(os.getenv(VARIABILE_INTERVALLO) or INTERVALLO_PREDEFINITO_ORE)
    except ValueError:
        ore = INTERVALLO_PREDEFINITO_ORE
    return max(ore, 1 / 60) * 3600  # mai più spesso di una volta al minuto


def registra_pulizia_comandi(nome: str, pulizia: Pulizia) -> None:
    """Chi tiene i comandi registra qui come toglierli: viene chiamata con `LANGBRAIN_RETENTION_COMANDI_GIORNI`."""
    _pulizie_comandi[nome] = pulizia


def rimuovi_pulizia_comandi(nome: str) -> None:
    _pulizie_comandi.pop(nome, None)


async def pulisci_eventi(giorni: int, db_path: str | None = None) -> int:
    """Toglie dal registro le decisioni più vecchie di `giorni` giorni. `giorni <= 0`: non fa nulla. Restituisce quante."""
    if giorni <= 0:
        return 0
    limite = schema.adesso_utc() - timedelta(days=giorni)
    async with archivio.motore(db_path).begin() as db:
        risultato = await db.execute(delete(schema.events).where(schema.events.c.timestamp < limite))
    return max(risultato.rowcount, 0)


async def esegui_pulizie(db_path: str | None = None) -> dict[str, int]:
    """Un giro di pulizia. Restituisce quante righe ha tolto ciascuna (`eventi` e il nome di ogni pulizia dei comandi)."""
    tolte: dict[str, int] = {}
    try:  # il buffer del flusso di eventi è effimero: si pulisce sempre (almeno un'ora di eventi resta)
        tolte["stream"] = await bus_eventi.pulisci_buffer(db_path)
    except Exception:
        logger.exception("Pulizia del buffer del flusso di eventi non riuscita.")
    giorni = giorni_eventi()
    if giorni:
        try:
            tolte["eventi"] = await pulisci_eventi(giorni, db_path)
        except Exception:
            logger.exception("Pulizia degli eventi non riuscita.")
    giorni = giorni_comandi()
    if giorni:
        for nome, pulizia in list(_pulizie_comandi.items()):
            try:
                tolte[nome] = await pulizia(giorni)
            except Exception:
                logger.exception("Pulizia '%s' non riuscita.", nome)
    for nome, quante in tolte.items():
        if quante:
            logger.info("[Conservazione] %s: tolte %d righe più vecchie del limite.", nome, quante)
    return tolte


async def sorveglia(db_path: str | None = None) -> None:
    """Ciclo da eseguire come compito in background per tutta la vita dell'applicazione."""
    while True:
        try:
            await esegui_pulizie(db_path)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Errore nel ciclo di conservazione dei dati.")
        await asyncio.sleep(intervallo_secondi())
