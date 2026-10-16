"""
Log JSON strutturato: una riga per evento, con campi fissi.

    {"ts": "2026-10-14T13:02:11.420Z", "level": "INFO", "apartment_id": "ap01", "correlation_id": "...", "actor": "user:a@b.it", "msg": "..."}

Si attiva con `LANGBRAIN_LOG_FORMAT=json`; il formato predefinito resta quello di sempre, per non cambiare l'esperienza di
sviluppo. `apartment_id` viene da `LANGBRAIN_APARTMENT_ID` (stringa vuota se manca); `correlation_id` e `actor` dal contesto
della richiesta (`app/core/contesto.py`), `null` fuori da una richiesta.

Nessun segreto nei log: prima di scrivere la riga, ogni valore di una variabile d'ambiente che "sembra" un segreto (nome con
KEY, SECRET, TOKEN, PASSWORD, PASSWD, CREDENTIAL) e la password dell'URL del database vengono sostituiti con `***`. Anche il
testo di un'eccezione passa da qui.
"""

import json
import logging
import os
import re
import sys
from urllib.parse import urlsplit

from app.core import contesto
from app.db.schema import adesso_utc, iso_ms

VARIABILE_FORMATO = "LANGBRAIN_LOG_FORMAT"
VARIABILE_APPARTAMENTO = "LANGBRAIN_APARTMENT_ID"
VARIABILE_LIVELLO = "LANGBRAIN_LOG_LEVEL"
MASCHERA = "***"

_NOME_SEGRETO = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)
_LUNGHEZZA_MINIMA = 6  # valori più corti ("0", "no", "1") non sono segreti e sporcherebbero ogni riga
_MARCATORE = "_langbrain_json"


def formato_json() -> bool:
    return (os.getenv(VARIABILE_FORMATO) or "").strip().lower() == "json"


def segreti_correnti() -> list[str]:
    """I valori da non scrivere mai nei log, dal più lungo al più corto (così un valore non ne spezza un altro)."""
    valori: set[str] = set()
    for nome, valore in os.environ.items():
        if valore and len(valore) >= _LUNGHEZZA_MINIMA and _NOME_SEGRETO.search(nome):
            valori.add(valore)
    for nome in ("LANGBRAIN_DB_URL",):
        try:
            password = urlsplit(os.environ.get(nome, "")).password
        except ValueError:
            password = None
        if password and len(password) >= _LUNGHEZZA_MINIMA:
            valori.add(password)
    return sorted(valori, key=len, reverse=True)


def oscura(testo: str) -> str:
    for segreto in segreti_correnti():
        if segreto in testo:
            testo = testo.replace(segreto, MASCHERA)
    return testo


class FormattatoreJson(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        messaggio = record.getMessage()
        if record.exc_info:
            messaggio = f"{messaggio}\n{self.formatException(record.exc_info)}"
        elif record.stack_info:
            messaggio = f"{messaggio}\n{self.formatStack(record.stack_info)}"
        riga = {
            "ts": iso_ms(adesso_utc()),
            "level": record.levelname,
            "apartment_id": os.getenv(VARIABILE_APPARTAMENTO, ""),
            "correlation_id": contesto.correlation_id_corrente(),
            "actor": contesto.attore_corrente(),
            "msg": oscura(messaggio),
        }
        return json.dumps(riga, ensure_ascii=False)


def configura_log(stream=None, livello: str | int | None = None) -> bool:
    """
    Se `LANGBRAIN_LOG_FORMAT=json`, manda tutti i log (anche quelli di uvicorn) al formato JSON su `stream` (stdout).
    Sicura da chiamare più volte. Restituisce True se il formato JSON è attivo.
    """
    if not formato_json():
        return False
    radice = logging.getLogger()
    for gestore in list(radice.handlers):
        if getattr(gestore, _MARCATORE, False):
            radice.removeHandler(gestore)
    gestore = logging.StreamHandler(stream or sys.stdout)
    gestore.setFormatter(FormattatoreJson())
    setattr(gestore, _MARCATORE, True)
    radice.addHandler(gestore)
    nome_livello = livello if livello is not None else (os.getenv(VARIABILE_LIVELLO) or "INFO")
    radice.setLevel(nome_livello.upper() if isinstance(nome_livello, str) else nome_livello)
    for nome in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        registro = logging.getLogger(nome)
        registro.handlers = []   # niente gestori propri: passano dalla radice, quindi in JSON
        registro.propagate = True
    return True


def configurazione_uvicorn() -> dict | None:
    """
    Il `log_config` da dare a uvicorn: con il formato JSON uvicorn non installa i suoi gestori (ci pensa `configura_log`),
    altrimenti None, cioè la configurazione predefinita di uvicorn.
    """
    if not formato_json():
        return None
    return {"version": 1, "disable_existing_loggers": False}
