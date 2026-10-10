"""
La simulazione energetica (rete elettrica e gas con accumuli) come estensione del core: rotte `/energia/...`, la pagina
`/energia`, i ruoli delle rotte, la sorveglianza del sistema nervoso e il suo spegnimento. Vedi `docs/SIMULAZIONE_ENERGIA.md`.

Il motore della simulazione tiene lo stato in memoria del processo che lo esegue: con `python -m app.serve` a più processi
le richieste della pagina possono arrivare a processi diversi. Per la simulazione usare un processo solo
(`python -m uvicorn app.api.main:app` o `python -m app.serve --workers 1`).
"""

import asyncio
import logging
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api.energia import registra_rotte
from app.core import multiprocesso
from app.core.configurazione import get_configurazione
from app.core.estensioni import Estensione, VoceNav
from app.core.lingua import scegli
from app.core.pagine_web import INTESTAZIONI_PAGINA
from app.core.ruoli import Ruolo
from app.simulazione.esecutore import esecutore
from app.simulazione.sistema_nervoso import sistema_nervoso

logger = logging.getLogger(__name__)

_PAGINA = Path(__file__).resolve().parent.parent / "static" / "energia.html"
_TESTI = Path(__file__).resolve().parent.parent / "simulazione" / "i18n"

TIROCINANTE, MEDICO, PRIMARIO = Ruolo.TIROCINANTE, Ruolo.MEDICO_DI_GUARDIA, Ruolo.PRIMARIO
PERMESSI = {
    ("GET", "/energia/rete"): TIROCINANTE,
    ("GET", "/energia/stato"): TIROCINANTE,
    ("GET", "/energia/stream"): TIROCINANTE,
    ("GET", "/energia/storico"): TIROCINANTE,
    ("GET", "/energia/entita/{entita_id}"): TIROCINANTE,
    ("POST", "/energia/controllo"): MEDICO,
    ("POST", "/energia/leva"): MEDICO,
    ("POST", "/energia/sistema-nervoso/ciclo"): MEDICO,
    ("POST", "/energia/sistema-nervoso/decisione"): MEDICO,
    ("POST", "/energia/scenario"): PRIMARIO,
    ("POST", "/energia/evento"): PRIMARIO,
    ("POST", "/energia/sistema-nervoso/configura"): PRIMARIO,
}


async def pagina_energia(request: Request):
    """Pagina della simulazione energetica (si disattiva con `[demo] pagina_web = 0`, come /demo)."""
    if not get_configurazione().demo_pagina_web or not _PAGINA.is_file():
        raise HTTPException(status_code=404, detail=scegli("Pagina della simulazione energetica non disponibile.",
                                                           "Energy simulation page not available."))
    return HTMLResponse(_PAGINA.read_text(encoding="utf-8"), headers=INTESTAZIONI_PAGINA)


def registra(app) -> None:
    if multiprocesso.condiviso():
        logger.warning("La simulazione energetica tiene lo stato in memoria di ogni processo: usa un solo worker per /energia.")
    registra_rotte(app)
    app.add_api_route("/energia", pagina_energia, methods=["GET"], include_in_schema=False)


async def sorveglianza() -> None:
    """Avvia la sorveglianza del sistema nervoso della rete e resta in attesa fino allo spegnimento."""
    sistema_nervoso.avvia_sorveglianza()
    await asyncio.Event().wait()


async def chiudi() -> None:
    await sistema_nervoso.chiudi()
    await esecutore.chiudi()


ESTENSIONE = Estensione(
    nome="energia",
    registra_rotte=registra,
    permessi=PERMESSI,
    rotte_pubbliche=frozenset({("GET", "/energia")}),
    nav=(VoceNav("energia", "/energia", "nav_energia", 15),),
    compiti=(sorveglianza,),
    chiusure=(chiudi,),
    testi=_TESTI,
)
