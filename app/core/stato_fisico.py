"""
Stato fisico dei dispositivi che sopravvive ai riavvii.

Prima di questo modulo lo stato di un dispositivo viveva solo nella memoria del processo: dopo un riavvio il sistema non sapeva più
cosa aveva comandato e un dispositivo cambiato a mano nel frattempo (qualcuno ha spento la luce, la serratura si è bloccata) restava
invisibile. Ora ogni comando riuscito scrive l'ultimo valore comandato nella tabella `device_state`; all'avvio e a intervalli
regolari `app/core/riconciliazione.py` lo confronta con ciò che il dispositivo dichiara.

  - `salva(...)`: scrive (o aggiorna) la riga di un dispositivo. Non solleva mai: se il database non risponde il comando resta
    riuscito e il problema finisce nel log (un comando già eseguito sul dispositivo non si annulla per un errore di archivio).
  - `leggi(dispositivo)` / `leggi_tutti()`: l'ultimo stato noto, con `source` = `commanded` (l'ultimo comando riuscito) oppure
    `observed` (l'ultima lettura registrata dalla riconciliazione).

I valori sono salvati come JSON, così un numero resta un numero. `[stato_fisico] persistenza = 0` in `configurazione.toml` spegne
tutto: nulla viene scritto e la riconciliazione non ha cosa confrontare.
"""

import json
import logging
import re
from typing import Any

from sqlalchemy import delete, select

from app.core import archivio
from app.core.configurazione import get_configurazione
from app.db import schema

logger = logging.getLogger(__name__)

COMANDATO = "commanded"
OSSERVATO = "observed"

_pronte: set[str] = set()
_ultimo_scritto: dict[tuple[str, str], tuple[Any, str]] = {}   # (database, dispositivo) -> (valore, source) già scritti


def attiva() -> bool:
    return get_configurazione().stato_fisico_persistenza


async def _motore(percorso: str | None = None):
    """Il motore dell'archivio con la tabella `device_state` creata se manca (una volta per database)."""
    motore = archivio.motore(percorso)
    chiave = archivio.url_database(percorso)
    if chiave not in _pronte:
        async with motore.begin() as db:
            await db.run_sync(archivio.crea_tabelle, schema.metadata, [schema.device_state])
        _pronte.add(chiave)
    return motore


def dimentica_cache() -> None:
    """Svuota la memoria delle scritture e delle tabelle già create (test, reset del sistema)."""
    _ultimo_scritto.clear()
    _pronte.clear()


def _codifica(valore: Any) -> str:
    return json.dumps(valore, default=str, ensure_ascii=False)


def _decodifica(testo: str | None) -> Any:
    if testo is None:
        return None
    try:
        return json.loads(testo)
    except ValueError:
        return testo


def _numero(valore: Any, unita: str = "") -> float | None:
    testo = str(valore).strip()
    if unita and testo.endswith(unita):
        testo = testo[: -len(unita)].strip()
    if re.fullmatch(r"-?\d+(?:[.,]\d+)?", testo):
        return float(testo.replace(",", "."))
    return None


def valori_equivalenti(a: Any, b: Any, unita: str = "") -> bool:
    """`22`, `"22"`, `"22.0"` e `"22°C"` (con unità `°C`) sono lo stesso valore; i testi si confrontano senza maiuscole."""
    if a is None or b is None:
        return a is b
    numero_a, numero_b = _numero(a, unita), _numero(b, unita)
    if numero_a is not None and numero_b is not None:
        return numero_a == numero_b
    return str(a).strip().casefold() == str(b).strip().casefold()


async def salva(dispositivo: str, valore: Any, unita: str = "", source: str = COMANDATO, percorso: str | None = None) -> None:
    """Registra l'ultimo stato noto del dispositivo. Non solleva mai (vedi la descrizione del modulo)."""
    try:
        if not attiva():
            return
        motore = await _motore(percorso)
        chiave = (archivio.url_database(percorso), dispositivo)
        if source == OSSERVATO and _ultimo_scritto.get(chiave) == (valore, source):
            return   # un valore riletto cento volte è una riga sola. I comandi si scrivono sempre: con più processi
                     # un altro può aver cambiato la riga, e un comando è un fatto da non perdere.
        tabella = schema.device_state
        inserimento = archivio.inserimento(motore, tabella).values(
            device_id=dispositivo, value=_codifica(valore), unit=unita or "", source=source, updated_at=schema.adesso_utc())
        inserimento = inserimento.on_conflict_do_update(
            index_elements=[tabella.c.device_id],
            set_={"value": inserimento.excluded.value, "unit": inserimento.excluded.unit,
                  "source": inserimento.excluded.source, "updated_at": inserimento.excluded.updated_at})
        async with motore.begin() as db:
            await db.execute(inserimento)
        _ultimo_scritto[chiave] = (valore, source)
    except Exception as errore:
        logger.warning("[StatoFisico] Stato di '%s' non salvato: %s", dispositivo, errore)


async def assicura_e_svuota(percorso: str | None = None) -> None:
    """Cancella tutti gli stati noti (reset del sistema). Non solleva."""
    try:
        motore = await _motore(percorso)
        async with motore.begin() as db:
            await db.execute(delete(schema.device_state))
    except Exception as errore:
        logger.warning("[StatoFisico] Stati non cancellati: %s", errore)
    _ultimo_scritto.clear()


def _riga(riga) -> dict[str, Any]:
    return {"device_id": riga.device_id, "value": _decodifica(riga.value), "unit": riga.unit or "",
            "source": riga.source, "updated_at": schema.iso_ms(riga.updated_at)}


async def leggi(dispositivo: str, percorso: str | None = None) -> dict[str, Any] | None:
    """L'ultimo stato noto del dispositivo, oppure None (mai comandato né riconciliato, persistenza spenta o database in errore)."""
    try:
        if not attiva():
            return None
        motore = await _motore(percorso)
        async with motore.connect() as db:
            riga = (await db.execute(select(schema.device_state).where(schema.device_state.c.device_id == dispositivo))).first()
        return None if riga is None else _riga(riga)
    except Exception as errore:
        logger.warning("[StatoFisico] Stato di '%s' non letto: %s", dispositivo, errore)
        return None


async def leggi_tutti(percorso: str | None = None) -> dict[str, dict[str, Any]]:
    try:
        if not attiva():
            return {}
        motore = await _motore(percorso)
        async with motore.connect() as db:
            righe = (await db.execute(select(schema.device_state))).all()
        return {r.device_id: _riga(r) for r in righe}
    except Exception as errore:
        logger.warning("[StatoFisico] Stati non letti: %s", errore)
        return {}
