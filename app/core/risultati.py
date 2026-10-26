"""
Contratto dei risultati di lettura e attuazione dei dispositivi.

Un tool o un agente che non riesce a completare un'operazione non solleva eccezioni verso l'alto e non
restituisce un semplice False: restituisce un dizionario con il nome del dispositivo, l'esito e il motivo
in chiaro, così l'agente padre sa perché l'operazione è fallita e può fare troubleshooting o escalation.

Formato (le chiavi opzionali compaiono solo quando servono):
    {
        "device_name": "living_lights",
        "success": False,
        "status": "TOOL_ERROR",
        "response": "controller Zigbee non risponde",   # motivo o esito in chiaro
        "error_type": "SIMULATED_FAULT",
        "actor": "component_lights", "action": "TURN_ON",
        "old_value": "OFF", "requested_value": "ON",
        "audit_logged": True,
        "attempts": [{"agent": "component_lights", "phase": "actuation", "status": "TOOL_ERROR", "response": "..."}],
    }
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Esiti dell'attuazione
APPLICATO = "APPLIED"
GIA_IMPOSTATO = "ALREADY_SET"
SOLO_LOG = "LOGGED_ONLY"
RESPINTO_PRIORITA = "REJECTED_PRIORITY"
COMANDO_NON_AMMESSO = "COMMAND_NOT_ALLOWED"
TOOL_ASSENTE = "TOOL_MISSING"
TOOL_ERRORE = "TOOL_ERROR"

_ESITI_RIUSCITI = {APPLICATO, GIA_IMPOSTATO, SOLO_LOG}
_ESITI_DI_GUASTO = {TOOL_ASSENTE, TOOL_ERRORE}

# Attesa massima su una singola operazione hardware prima di considerarla fallita.
TIMEOUT_TOOL_SECONDI = 10.0


class ErroreTool(Exception):
    """
    Guasto di un dispositivo con dettagli strutturati. I tool possono sollevarla dai propri
    `get_tool_value`/`set_tool_value` per indicare codice e dettagli dell'errore (es. "TIMEOUT", {"retry_dopo": 30}).
    """

    def __init__(self, messaggio: str, codice: str = "TOOL_ERROR", dettagli: dict[str, Any] | None = None):
        super().__init__(messaggio)
        self.messaggio = messaggio
        self.codice = codice
        self.dettagli = dettagli or {}


def e_comando_non_ammesso(risultato: dict[str, Any] | None) -> bool:
    """True se il comando è stato rifiutato perché il valore o il dispositivo non sono ammessi da configurazione.toml."""
    return bool(risultato) and risultato.get("status") == COMANDO_NON_AMMESSO


def e_guasto_tool(risultato: dict[str, Any] | None) -> bool:
    """True se il risultato descrive un guasto del dispositivo (tool assente o in errore)."""
    return bool(risultato) and risultato.get("status") in _ESITI_DI_GUASTO


def nuovo_risultato(
    device_name: str,
    status: str,
    response: str,
    *,
    actor: str | None = None,
    action: str | None = None,
    old_value: Any = None,
    requested_value: Any = None,
    error_type: str | None = None,
    details: dict[str, Any] | None = None,
    audit_logged: bool = True,
) -> dict[str, Any]:
    """Costruisce un risultato di attuazione nel formato standard."""
    risultato: dict[str, Any] = {
        "device_name": device_name,
        "success": status in _ESITI_RIUSCITI,
        "status": status,
        "response": response,
        "audit_logged": audit_logged,
        "attempts": [],
    }
    opzionali = {
        "actor": actor, "action": action, "old_value": old_value,
        "requested_value": requested_value, "error_type": error_type, "details": details or None,
    }
    risultato.update({k: v for k, v in opzionali.items() if v is not None})
    if actor:
        risultato["attempts"].append({"agent": actor, "phase": "actuation", "status": status, "response": response})
    return risultato


def _errore(device_name: str, response: str, error_type: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "device_name": device_name, "success": False, "status": TOOL_ERRORE,
        "response": response, "error_type": error_type, **({"details": details} if details else {}),
    }


async def leggi_tool(tool_obj: Any, device_name: str, timeout: float = TIMEOUT_TOOL_SECONDI) -> dict[str, Any]:
    """Legge il valore di un tool senza mai sollevare: restituisce {device_name, success, response, value, ...}."""
    try:
        valore = await asyncio.wait_for(tool_obj.get_tool_value(), timeout=timeout)
    except ErroreTool as e:
        return _errore(device_name, e.messaggio, e.codice, e.dettagli)
    except asyncio.TimeoutError:
        return _errore(device_name, f"Nessuna risposta dal dispositivo entro {timeout}s.", "TIMEOUT")
    except Exception as e:
        return _errore(device_name, str(e) or type(e).__name__, type(e).__name__)
    return {"device_name": device_name, "success": True, "response": f"Valore letto: {valore}", "value": valore}


async def comanda_tool(
    tool_obj: Any, device_name: str, valore: Any, timeout: float = TIMEOUT_TOOL_SECONDI
) -> dict[str, Any]:
    """Invia un comando a un tool senza mai sollevare: restituisce {device_name, success, response, ...}."""
    try:
        esito = await asyncio.wait_for(tool_obj.set_tool_value(valore), timeout=timeout)
    except ErroreTool as e:
        return _errore(device_name, e.messaggio, e.codice, e.dettagli)
    except asyncio.TimeoutError:
        return _errore(device_name, f"Nessuna risposta dal dispositivo entro {timeout}s.", "TIMEOUT")
    except Exception as e:
        return _errore(device_name, str(e) or type(e).__name__, type(e).__name__)
    if esito is False:
        return _errore(device_name, "Il dispositivo ha rifiutato il comando.", "COMMAND_REJECTED")
    return {
        "device_name": device_name, "success": True,
        "response": f"Comando '{valore}' eseguito su '{device_name}'.", "value": valore,
    }
