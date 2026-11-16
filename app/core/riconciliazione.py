"""
Riconciliazione dello stato fisico: ciò che il sistema crede dei dispositivi contro ciò che i dispositivi dichiarano.

Si esegue all'avvio e poi a intervalli regolari (`[stato_fisico] intervallo_secondi`, 0 = solo all'avvio; con più processi gira
in uno solo). Per ogni dispositivo, confrontando con l'ultimo stato noto salvato in `device_state` (`app/core/stato_fisico.py`):

  dispositivo simulato   (solo all'avvio) rimette in memoria l'ultimo valore comandato prima del riavvio: l'effettore simulato non
                         ha un hardware che lo ricordi. Esito `ripristinato`.
  senza `simulato`       un tool che non dichiara l'attributo `simulato` (True o False) non viene riconciliato.
  dispositivo vero       legge il valore dal dispositivo (mai un comando: un riavvio non deve aprire una serratura).
      allineato          il valore letto coincide con l'ultimo comando (o non c'era niente da confrontare).
      deriva             l'ultimo stato noto era un COMANDO e il dispositivo ora dichiara un valore diverso (qualcuno è intervenuto a
                         mano, il comando non ha avuto effetto, il dispositivo si è riavviato). Si registra l'evento
                         `RECONCILED_DRIFT` nel sistema nervoso e il valore letto diventa lo stato noto: il Brain lo vede nella
                         finestra degli eventi recenti e decide, il codice non corregge da solo.
      irraggiungibile    la lettura è fallita: si registra `RECONCILED_UNREACHABLE` (una volta per interruzione, non a ogni giro).
                         Lo stato noto resta quello di prima.

Gli eventi usano il prefisso `RECONCILED_`: per le regole di priorità sono eventi chiusi, non blocchi (`app/core/priorita.py`).
Una differenza tra un valore OSSERVATO e la lettura successiva non è una deriva (un sensore cambia da solo): lo è solo la differenza
dall'ultimo COMANDO.
"""

import asyncio
import logging
from typing import Any, Callable

from app.core import contesto, metriche, multiprocesso, stato_fisico
from app.core.configurazione import get_configurazione
from app.core.risultati import leggi_tool

logger = logging.getLogger(__name__)

ALLINEATO, DERIVA, IRRAGGIUNGIBILE, RIPRISTINATO = "allineato", "deriva", "irraggiungibile", "ripristinato"
_irraggiungibili: set[str] = set()   # dispositivi già segnalati come irraggiungibili: non si ripete l'evento a ogni giro


async def _registra_evento(azione: str, dispositivo: str, vecchio: Any, nuovo: Any, motivo: str, esito: str) -> None:
    from app.tools.nervous_system import NervousSystem
    try:
        await NervousSystem(target=["all"]).log_event(
            actor="system", action=azione, target=dispositivo, old_value=vecchio, new_value=nuovo, reasoning=motivo, outcome=esito)
    except Exception as errore:
        logger.warning("[Riconciliazione] Evento %s per '%s' non registrato: %s", azione, dispositivo, errore)


async def _ripristina_simulato(dispositivo: str, tool: Any, noto: dict | None) -> dict[str, Any]:
    if multiprocesso.condiviso() and await multiprocesso.leggi(f"effettore:{dispositivo}", multiprocesso.ASSENTE) is not multiprocesso.ASSENTE:
        return {"device": dispositivo, "outcome": ALLINEATO, "detail": "il valore è già nello stato condiviso"}
    if noto is None or noto["value"] is None or not hasattr(tool, "ripristina_valore"):
        return {"device": dispositivo, "outcome": ALLINEATO, "detail": "nessuno stato precedente"}
    tool.ripristina_valore(noto["value"])
    logger.info("[Riconciliazione] '%s' ripristinato a %r (%s, %s).", dispositivo, noto["value"], noto["source"], noto["updated_at"])
    return {"device": dispositivo, "outcome": RIPRISTINATO, "value": noto["value"], "known_since": noto["updated_at"]}


async def _riconcilia_vero(dispositivo: str, tool: Any, noto: dict | None) -> dict[str, Any]:
    lettura = await leggi_tool(tool, dispositivo)
    unita = getattr(tool, "unit", "") or ""
    if not lettura["success"]:
        if dispositivo not in _irraggiungibili:
            _irraggiungibili.add(dispositivo)
            ultimo = None if noto is None else noto["value"]
            await _registra_evento(
                "RECONCILED_UNREACHABLE", dispositivo, ultimo, None,
                f"Il dispositivo non risponde alla riconciliazione: {lettura['response']}. Ultimo stato noto: {ultimo!r}.", "failed")
        return {"device": dispositivo, "outcome": IRRAGGIUNGIBILE, "error": lettura["response"], "error_type": lettura.get("error_type")}

    letto = lettura["value"]
    if dispositivo in _irraggiungibili:
        _irraggiungibili.discard(dispositivo)
        logger.info("[Riconciliazione] '%s' risponde di nuovo (valore %r).", dispositivo, letto)
    if noto is None or noto["value"] is None:
        await stato_fisico.salva(dispositivo, letto, unita, stato_fisico.OSSERVATO)
        return {"device": dispositivo, "outcome": ALLINEATO, "value": letto, "detail": "nessuno stato precedente"}
    if stato_fisico.valori_equivalenti(noto["value"], letto, unita):
        return {"device": dispositivo, "outcome": ALLINEATO, "value": letto}
    if noto["source"] == stato_fisico.COMANDATO:
        await _registra_evento(
            "RECONCILED_DRIFT", dispositivo, noto["value"], letto,
            f"Ultimo comando riuscito: {noto['value']!r} ({noto['updated_at']}); il dispositivo ora dichiara {letto!r}. "
            "Nessuna azione automatica: lo stato noto è aggiornato al valore letto.", "informational")
        await stato_fisico.salva(dispositivo, letto, unita, stato_fisico.OSSERVATO)
        return {"device": dispositivo, "outcome": DERIVA, "commanded": noto["value"], "value": letto}
    await stato_fisico.salva(dispositivo, letto, unita, stato_fisico.OSSERVATO)   # solo una nuova osservazione
    return {"device": dispositivo, "outcome": ALLINEATO, "value": letto, "detail": "nuova osservazione"}


async def riconcilia(strumenti: dict[str, Any], avvio: bool = False) -> list[dict[str, Any]]:
    """
    Riconcilia i dispositivi della mappa e restituisce un rapporto per dispositivo. Non solleva: un errore su un dispositivo non
    ferma gli altri. Con `[stato_fisico] persistenza = 0` non c'è nulla da confrontare e restituisce un elenco vuoto.
    """
    if not stato_fisico.attiva():
        return []
    noti = await stato_fisico.leggi_tutti()
    rapporto: list[dict[str, Any]] = []
    with contesto.fatto_interno():
        for dispositivo, tool in sorted(strumenti.items()):
            try:
                simulato = getattr(tool, "simulato", None)
                if simulato is None:
                    continue   # un tool che non dichiara se è simulato o vero non si tocca: nessuna sorpresa per i tool dei nodi
                if simulato:
                    if not avvio:
                        continue
                    esito = await _ripristina_simulato(dispositivo, tool, noti.get(dispositivo))
                else:
                    esito = await _riconcilia_vero(dispositivo, tool, noti.get(dispositivo))
            except Exception as errore:
                logger.exception("[Riconciliazione] '%s' non riconciliato.", dispositivo)
                esito = {"device": dispositivo, "outcome": "errore", "error": str(errore)}
            metriche.incrementa("langbrain_device_reconciliations_total", outcome=esito["outcome"])
            rapporto.append(esito)
    if rapporto:
        riassunto = {}
        for voce in rapporto:
            riassunto[voce["outcome"]] = riassunto.get(voce["outcome"], 0) + 1
        logger.info("[Riconciliazione] %s: %s.", "all'avvio" if avvio else "periodica", riassunto)
    return rapporto


async def sorveglia(strumenti: Callable[[], dict[str, Any]]) -> None:
    """
    Compito in background (un solo processo alla volta, vedi `multiprocesso.compito_leader`): riconcilia i dispositivi veri a
    intervalli regolari. `strumenti` è una funzione che restituisce la mappa corrente dei tool. Con intervallo 0 non fa nulla.
    """
    while True:
        intervallo = get_configurazione().stato_fisico_intervallo_secondi
        if intervallo <= 0:
            await asyncio.sleep(60)   # l'intervallo si può accendere a caldo editando configurazione.toml
            continue
        await asyncio.sleep(intervallo)
        await riconcilia(strumenti(), avvio=False)
