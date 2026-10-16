"""
Controlli all'avvio: se la configurazione è incompleta il processo si ferma subito con un messaggio chiaro, invece di
partire e fallire alla prima richiesta (o, peggio, di partire con l'API aperta a chiunque).

  - database: `LANGBRAIN_DB_BACKEND=postgres` richiede `LANGBRAIN_DB_URL` (vedi `app/core/archivio.py`);
  - chiave API: con `LANGBRAIN_REQUIRE_API_KEY=1` serve almeno una chiave configurata (vedi `app/core/ruoli.py`).
"""

import os

from app.core import archivio
from app.core.ruoli import autenticazione_attiva

VARIABILE_CHIAVE_RICHIESTA = "LANGBRAIN_REQUIRE_API_KEY"
_VERO = ("1", "true", "yes", "on", "si", "sì")


class ErroreAvvio(RuntimeError):
    """La configurazione impedisce l'avvio."""


def chiave_richiesta() -> bool:
    return (os.getenv(VARIABILE_CHIAVE_RICHIESTA) or "").strip().lower() in _VERO


def verifica_avvio() -> None:
    """Solleva `ErroreAvvio` se il database o la chiave API non sono configurati come richiesto."""
    from app.MAO import model_access_object  # noqa: F401  (all'import carica il .env: le chiavi possono stare lì)
    try:
        archivio.verifica_configurazione()
    except archivio.ErroreConfigurazioneDb as errore:
        raise ErroreAvvio(str(errore)) from errore
    if chiave_richiesta() and not autenticazione_attiva():
        raise ErroreAvvio(
            f"{VARIABILE_CHIAVE_RICHIESTA}=1 ma nessuna chiave API è configurata: imposta API_KEY (o API_KEY_PRIMARIO, "
            "API_KEY_ADMIN) nell'ambiente. Il server non parte con l'API aperta.")
