"""
Bus degli eventi in tempo reale: la base per un flusso SSE con `Last-Event-ID`.

Il bus è una tabella (`stream_events`) in cui scrive SOLO chi chiama `pubblica`: nessun evento interno del grafo (le
esecuzioni dei nodi, i checkpoint...) ci finisce da solo. Chi serve il flusso (un adattatore) usa `leggi_da` per
riconsegnare ciò che un client ha perso e `flusso` per seguire i nuovi eventi.

  - `id`: intero crescente, per istanza, persistito nel database e MAI riusato — nemmeno dopo la pulizia del buffer o un
    riavvio (colonna autoincrementale: SQLite `AUTOINCREMENT`, Postgres sequenza).
  - Buffer: gli eventi restano almeno un'ora (`LANGBRAIN_STREAM_BUFFER_ORE`, predefinito 24, minimo 1); la pulizia gira con
    quella della conservazione dei dati (`app/core/conservazione.py`).
  - Tipi: solo i sette del contratto 1.0.4 (`TIPI_EVENTO`); un altro tipo è un errore di programmazione (`ValueError`).
  - Involucro: `id, event, ts, apartment_id, correlation_id, data` (`ts` in ISO 8601 UTC al millisecondo). L'`id` è una
    STRINGA numerica (così lo vuole lo schema `StreamEnvelope`); `pubblica` restituisce l'intero, `leggi_da` accetta stringa o intero.
  - Tempestività: lo stesso processo risveglia chi aspetta subito; tra processi si controlla il database ogni 0,25 s,
    quindi un evento arriva entro 1 s dal fatto.
"""

import asyncio
import json
import logging
import os
from datetime import timedelta
from typing import Any, AsyncIterator

from sqlalchemy import delete, func, insert, select

from app.core import archivio, contesto
from app.db import schema

logger = logging.getLogger(__name__)

TIPI_EVENTO = (
    "device.state", "device.online", "command.updated", "decision.logged",
    "approval.pending", "apartment.changed", "instance.health",
)
VARIABILE_BUFFER = "LANGBRAIN_STREAM_BUFFER_ORE"
BUFFER_PREDEFINITO_ORE = 24.0
BUFFER_MINIMO_ORE = 1.0
INTERVALLO_CONTROLLO_SECONDI = 0.25
INTERVALLO_PING_SECONDI = 25.0
LIMITE_PREDEFINITO = 1000

tabella = schema.stream_events
_nuovi: dict[int, asyncio.Event] = {}  # un risveglio per event loop (le attese di un loop non toccano quello di un altro)


def ore_buffer() -> float:
    try:
        ore = float(os.getenv(VARIABILE_BUFFER) or BUFFER_PREDEFINITO_ORE)
    except ValueError:
        ore = BUFFER_PREDEFINITO_ORE
    return max(ore, BUFFER_MINIMO_ORE)


def _involucro(riga) -> dict[str, Any]:
    try:
        dati = json.loads(riga.data) if riga.data else {}
    except ValueError:
        dati = {}
    return {"id": str(riga.id), "event": riga.event, "ts": riga.ts, "apartment_id": riga.apartment_id,
            "correlation_id": riga.correlation_id, "data": dati}


def _risveglia() -> None:
    for segnale in _nuovi.values():
        segnale.set()


async def pubblica(evento: str, data: dict[str, Any] | None = None, correlation_id: str | None = None,
                   db_path: str | None = None) -> int:
    """Pubblica un evento e restituisce il suo id. `correlation_id` mancante: quello della richiesta in corso (o nessuno)."""
    if evento not in TIPI_EVENTO:
        raise ValueError(f"Tipo di evento '{evento}' non ammesso: usa uno tra {list(TIPI_EVENTO)}.")
    async with archivio.motore(db_path).begin() as db:
        risultato = await db.execute(insert(tabella).values(
            event=evento, ts=schema.iso_ms(schema.adesso_utc()), apartment_id=os.getenv("LANGBRAIN_APARTMENT_ID", ""),
            correlation_id=correlation_id or contesto.correlation_id_corrente(),
            data=json.dumps(data or {}, ensure_ascii=False)))
        nuovo_id = risultato.inserted_primary_key[0]
    _risveglia()
    return nuovo_id


async def leggi_da(ultimo_id: int | None = 0, limite: int = LIMITE_PREDEFINITO, db_path: str | None = None) -> list[dict[str, Any]]:
    """Gli eventi con id MAGGIORE di `ultimo_id` (`Last-Event-ID`), in ordine, al massimo `limite`."""
    async with archivio.motore(db_path).connect() as db:
        righe = (await db.execute(
            select(tabella).where(tabella.c.id > int(ultimo_id or 0)).order_by(tabella.c.id).limit(limite))).all()
    return [_involucro(r) for r in righe]


async def ultimo_id(db_path: str | None = None) -> int:
    """L'id dell'ultimo evento ancora nel buffer, 0 se è vuoto (gli id nuovi continuano comunque dopo l'ultimo mai dato)."""
    async with archivio.motore(db_path).connect() as db:
        return (await db.execute(select(func.coalesce(func.max(tabella.c.id), 0)))).scalar_one()


async def attendi_nuovi(ultimo: int, attesa: float, db_path: str | None = None) -> list[dict[str, Any]]:
    """Aspetta fino a `attesa` secondi eventi con id maggiore di `ultimo`; restituisce quelli trovati (anche nessuno)."""
    chiave = id(asyncio.get_running_loop())
    segnale = _nuovi.setdefault(chiave, asyncio.Event())
    scadenza = asyncio.get_running_loop().time() + attesa
    while True:
        segnale.clear()
        eventi = await leggi_da(ultimo, db_path=db_path)
        if eventi:
            return eventi
        restante = scadenza - asyncio.get_running_loop().time()
        if restante <= 0:
            return []
        try:
            await asyncio.wait_for(segnale.wait(), timeout=min(INTERVALLO_CONTROLLO_SECONDI, restante))
        except asyncio.TimeoutError:
            pass


def formatta_sse(evento: dict[str, Any]) -> str:
    """Un evento nel formato Server-Sent Events: `id`, `event` e `data` (l'involucro in JSON)."""
    return f"id: {evento['id']}\nevent: {evento['event']}\ndata: {json.dumps(evento, ensure_ascii=False)}\n\n"


PING = ": ping\n\n"


async def flusso(ultimo_id_ricevuto: int | None = None, ping_secondi: float = INTERVALLO_PING_SECONDI,
                 db_path: str | None = None) -> AsyncIterator[str]:
    """
    Generatore SSE: prima riconsegna gli eventi persi (id > `ultimo_id_ricevuto`, il valore di `Last-Event-ID`; senza,
    parte dal prossimo evento), poi segue i nuovi e manda `: ping` ogni `ping_secondi` di silenzio.
    """
    ultimo = int(ultimo_id_ricevuto) if ultimo_id_ricevuto is not None else await ultimo_id(db_path)
    while True:
        eventi = await attendi_nuovi(ultimo, ping_secondi, db_path)
        if not eventi:
            yield PING
            continue
        for evento in eventi:
            yield formatta_sse(evento)
            ultimo = int(evento["id"])


async def pulisci_buffer(db_path: str | None = None) -> int:
    """Toglie gli eventi più vecchi del buffer (almeno un'ora). Gli id già dati non vengono mai riusati."""
    limite = schema.iso_ms(schema.adesso_utc() - timedelta(hours=ore_buffer()))
    async with archivio.motore(db_path).begin() as db:
        risultato = await db.execute(delete(tabella).where(tabella.c.ts < limite))
    return max(risultato.rowcount, 0)
