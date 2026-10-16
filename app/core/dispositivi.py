"""
Il dispositivo come lo vede chi deve dichiararne le capacità (l'adattatore del contratto 1.0.4): nome, valore, unità,
limiti del setpoint, se risponde e quando è stato visto l'ultima volta.

Il nucleo non conosce marche né modelli né il nome delle capacità (`switch`, `climate`...): quello è il catalogo dell'adattatore.
Qui c'è ciò che il nucleo ha davvero:
  - il valore e l'unità, dal tool del dispositivo (`Reflex` / `SimulatedEffector`);
  - i limiti, da `[dispositivi.*] intervallo` di `configurazione.toml` (`setpoint_min`, `setpoint_max`);
  - `online` e `last_seen`, dal tool: un tool che li ha (`online`, `ultimo_contatto`) li dichiara, altrimenti `online` è l'esito
    della lettura e `last_seen` il momento della lettura riuscita.
`descrivi_dispositivo` non solleva mai.
"""

from datetime import datetime
from typing import Any

from app.core.configurazione import get_configurazione
from app.core.risultati import leggi_tool
from app.db import schema
from app.tools.effectors import tool_registrati, trova_tool


async def descrivi_dispositivo(device_id: str, tools_map: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """`{device_id, value, unit, setpoint_min, setpoint_max, online, last_seen}`, oppure None se il dispositivo non è noto."""
    tool = trova_tool(device_id, tools_map)
    if tool is None:
        return None
    lettura = await leggi_tool(tool, device_id)
    regola = get_configurazione().regola_per(device_id)
    intervallo = regola.intervallo if regola is not None else None
    online = bool(getattr(tool, "online", lettura["success"])) and lettura["success"]
    ultimo = getattr(tool, "ultimo_contatto", None)
    if ultimo is None and lettura["success"]:
        ultimo = schema.adesso_utc()
    return {
        "device_id": device_id,
        "value": lettura.get("value") if lettura["success"] else None,
        "unit": getattr(tool, "unit", "") or (regola.unita if regola is not None else ""),
        "setpoint_min": intervallo[0] if intervallo else None,
        "setpoint_max": intervallo[1] if intervallo else None,
        "online": online,
        "last_seen": schema.iso_ms(ultimo) if isinstance(ultimo, datetime) else ultimo,
    }


async def elenca_dispositivi(tools_map: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Tutti i dispositivi noti (quelli della mappa e i registrati nel processo), in ordine di nome."""
    nomi = sorted({*(tools_map or {}), *tool_registrati()})
    descritti = [await descrivi_dispositivo(n, tools_map) for n in nomi]
    return [d for d in descritti if d is not None]
