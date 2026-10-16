"""
Ciò che il nucleo sa dire di sé per `GET /health` e `GET /version` del contratto 1.0.4: l'implementazione, da quanto è
acceso, se il database risponde e se il modello linguistico è utilizzabile. Lo stato del connector e dei dispositivi è di chi
li ha (l'adattatore): qui non c'è.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import text

from app.core import archivio
from app.db import schema

logger = logging.getLogger(__name__)

NOME_IMPLEMENTAZIONE = "langbrain"
VERSIONE_NUCLEO = "2.1.0"
ATTESA_DATABASE_SECONDI = 5.0

_avvio = time.monotonic()


def implementazione() -> dict[str, str]:
    """`{name, version}` come nello schema `Implementation`."""
    return {"name": NOME_IMPLEMENTAZIONE, "version": VERSIONE_NUCLEO}


def uptime_s() -> int:
    return int(time.monotonic() - _avvio)


async def stato_database(db_path: str | None = None) -> dict[str, str]:
    """`{status: ok|error}`: il database risponde a una query semplice entro pochi secondi. Non solleva."""
    async def prova() -> None:
        async with archivio.motore(db_path).connect() as db:
            await db.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(prova(), timeout=ATTESA_DATABASE_SECONDI)
        return {"status": "ok"}
    except Exception as errore:
        logger.warning("[Salute] Database non raggiungibile: %s", errore)
        return {"status": "error"}


def stato_provider() -> dict[str, str]:
    """`{status: ok|degraded|unknown, provider}`: il provider predefinito ha una chiave utilizzabile. Non solleva."""
    try:
        from app.MAO.model_access_object import Mao

        descrizione = Mao().descrivi_provider(None, None)
        provider = str(descrizione.get("provider") or "unknown")
        return {"status": "ok" if descrizione.get("abilitato") else "degraded", "provider": provider}
    except Exception as errore:
        logger.warning("[Salute] Stato del provider non disponibile: %s", errore)
        return {"status": "unknown", "provider": "unknown"}


async def salute_nucleo(db_path: str | None = None) -> dict:
    """I campi di `Health` che il nucleo conosce; l'adattatore aggiunge `status`, `apartment_id`, `connector` e `devices`."""
    return {
        "implementation": implementazione(),
        "uptime_s": uptime_s(),
        "database": await stato_database(db_path),
        "model_provider": stato_provider(),
        "checked_at": schema.iso_ms(datetime.now(timezone.utc)),
    }
