"""
Idempotenza: la stessa richiesta, ripetuta, dà la stessa risposta e non si esegue due volte.

Il contratto 1.0.4 la chiede in due forme:
  - comandi (e le altre POST): stesso `X-Correlation-Id` sullo stesso endpoint entro 60 s -> stessa risposta, una sola
    esecuzione, una sola Decision (scenario C02);
  - prenotazioni: stesso `event_id`, SENZA limite di tempo.

`esegui_una_volta(ambito, chiave, funzione, ttl_secondi)` lo fa per qualunque operazione: `ambito` dice quale (per esempio
`POST /devices/luce/commands`), `chiave` identifica la richiesta (il correlation id, l'`event_id`), `funzione` è la
coroutine che esegue e restituisce un dizionario JSON. La prima chiamata esegue e memorizza il risultato; le successive con
la stessa coppia (ambito, chiave) lo riprendono, anche se arrivano mentre la prima è ancora in corso (aspettano). Vale tra
processi e su entrambi i database: la prenotazione della chiave è un inserimento atomico.

  - `ttl_secondi=None`: il risultato si conserva per sempre (prenotazioni). Un numero: scade dopo quei secondi e la stessa
    chiave può eseguire di nuovo.
  - Se `funzione` solleva, nulla viene memorizzato: chi riprova esegue di nuovo.
  - Se il processo che stava eseguendo muore, la prenotazione scade da sola (`DURATA_ESECUZIONE_SECONDI`) e un'altra
    richiesta subentra.
"""

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from sqlalchemy import delete, select, update

from app.core import archivio
from app.db import schema

logger = logging.getLogger(__name__)

# Quanto può durare un'esecuzione prima che la sua prenotazione si consideri abbandonata (e si aspetti al massimo altrettanto).
DURATA_ESECUZIONE_SECONDI = 60.0
INTERVALLO_ATTESA_SECONDI = 0.05
IN_CORSO = "in_corso"
FATTO = "fatto"

tabella = schema.idempotenza


def _ora() -> float:
    return time.time()


async def esegui_una_volta(
    ambito: str,
    chiave: str,
    funzione: Callable[[], Awaitable[dict[str, Any]]],
    ttl_secondi: float | None = 60.0,
    db_path: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """
    Esegue `funzione` una sola volta per (`ambito`, `chiave`). Restituisce `(risultato, riutilizzato)`: `riutilizzato` è
    True se il risultato viene da una richiesta precedente.
    """
    motore = archivio.motore(db_path)
    limite_attesa = _ora() + DURATA_ESECUZIONE_SECONDI + 5
    while True:
        ora = _ora()
        scadenza_esecuzione = ora + DURATA_ESECUZIONE_SECONDI
        async with motore.begin() as db:
            prenotata = (await db.execute(
                archivio.inserimento(motore, tabella).values(
                    ambito=ambito, chiave=chiave, stato=IN_CORSO, risposta=None, creata_il=ora, scade_il=scadenza_esecuzione)
                .on_conflict_do_nothing(index_elements=[tabella.c.ambito, tabella.c.chiave]))).rowcount == 1
            if not prenotata:
                # C'è già una riga: se è scaduta (risultato vecchio o esecuzione abbandonata) la si rileva atomicamente.
                prenotata = (await db.execute(
                    update(tabella).where(tabella.c.ambito == ambito, tabella.c.chiave == chiave,
                                          tabella.c.scade_il.is_not(None), tabella.c.scade_il < ora)
                    .values(stato=IN_CORSO, risposta=None, creata_il=ora, scade_il=scadenza_esecuzione))).rowcount == 1
        if prenotata:
            return await _esegui_e_memorizza(motore, ambito, chiave, funzione, ttl_secondi), False

        async with motore.connect() as db:
            riga = (await db.execute(select(tabella.c.stato, tabella.c.risposta).where(
                tabella.c.ambito == ambito, tabella.c.chiave == chiave))).first()
        if riga is not None and riga.stato == FATTO:
            return json.loads(riga.risposta), True
        if _ora() > limite_attesa:
            raise TimeoutError(f"L'esecuzione precedente di '{ambito}' ({chiave}) non è terminata.")
        await asyncio.sleep(INTERVALLO_ATTESA_SECONDI)  # un'altra richiesta sta eseguendo: si aspetta il suo risultato


async def _esegui_e_memorizza(motore, ambito: str, chiave: str, funzione, ttl_secondi: float | None) -> dict[str, Any]:
    try:
        risultato = await funzione()
        testo = json.dumps(risultato, ensure_ascii=False)
    except BaseException:
        # Nulla di memorizzato: chi riprova esegue di nuovo (anche se la richiesta è stata annullata).
        try:
            async with motore.begin() as db:
                await db.execute(delete(tabella).where(tabella.c.ambito == ambito, tabella.c.chiave == chiave,
                                                       tabella.c.stato == IN_CORSO))
        except Exception:
            logger.exception("[Idempotenza] Prenotazione di '%s' non rimossa: scadrà da sola.", ambito)
        raise
    async with motore.begin() as db:
        await db.execute(update(tabella).where(tabella.c.ambito == ambito, tabella.c.chiave == chiave).values(
            stato=FATTO, risposta=testo, scade_il=None if ttl_secondi is None else _ora() + ttl_secondi))
    return risultato


async def pulisci_scadute(db_path: str | None = None) -> int:
    """Toglie le righe scadute (risultati oltre il loro tempo, esecuzioni abbandonate). Quelle senza scadenza restano."""
    async with archivio.motore(db_path).begin() as db:
        risultato = await db.execute(delete(tabella).where(tabella.c.scade_il.is_not(None), tabella.c.scade_il < _ora()))
    return max(risultato.rowcount, 0)
