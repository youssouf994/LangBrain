"""
Costanti e utilità condivise tra gli agenti e il loop di esecuzione.
"""
from datetime import datetime, timedelta, timezone
import os
from typing import Any

# TTL di default in minuti per la scadenza automatica dei flag di controllo (es. REJECTED, BLOCKED)
DEFAULT_FLAG_TTL_MINUTES = int(os.getenv("FLAG_TTL_MINUTES", "60"))

# Prefissi che identificano flag di controllo interni del sistema.
# Questi valori vengono scritti dal Brain o dall'Orchestratore come stati di
# blocco/riconciliazione e NON rappresentano valori fisici reali dei sensori.
_CONTROL_FLAG_PREFIXES = (
    "REJECTED",
    "RECONCILED_",
    "RESOLVED_",
    "ESCALATION_",
    "BLOCKED",
    "EXPIRED_",
)


def is_control_flag(value: Any) -> bool:
    """
    Restituisce True se il valore è un flag di controllo interno, non un dato fisico.
    Usato sia dal SensorEventProducer (per non ri-triggerare il grafo) sia dagli
    agenti (per cortocircuitare la valutazione quando il dispositivo è bloccato).
    """
    s = str(value).upper()
    return any(s.startswith(prefix.upper()) for prefix in _CONTROL_FLAG_PREFIXES)


def is_flag_expired(timestamp_str: str, ttl_minutes: int = DEFAULT_FLAG_TTL_MINUTES) -> bool:
    """
    Verifica se un flag di controllo/evento ha superato la sua finestra di validità (TTL).
    Restituisce True se il timestamp + ttl_minutes è antecedente all'orario attuale.
    """
    if not timestamp_str:
        return False
    try:
        # Pulisce eventuale format ISO
        ts_clean = timestamp_str.replace("Z", "").replace("T", " ")
        if "." in ts_clean:
            ts_clean = ts_clean.split(".")[0]
        event_time = datetime.strptime(ts_clean, "%Y-%m-%d %H:%M:%S")
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=ttl_minutes)
        return event_time < cutoff
    except (ValueError, TypeError):
        return False
