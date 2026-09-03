import logging
from typing import Any
from app.tools.baseTool import BaseTool

logger = logging.getLogger(__name__)

# Registry globale: garantisce un'unica istanza per dispositivo (Singleton per processo)
_TOOL_REGISTRY: dict[str, "IoTDeviceTool"] = {}


class IoTDeviceTool(BaseTool):
    """
    Tool generico per sensori e attuatori IoT (reali o simulati).
    Condiviso sia dai sotto-agenti che dal Cervello per azionare direttamente i dispositivi.
    """
    def __init__(self, target_device: str, initial_value: Any = "OFF", unit: str = ""):
        super().__init__(target_device=target_device)
        self._current_value = initial_value
        self.unit = unit

    async def get_tool_value(self) -> Any:
        return self._current_value

    async def set_tool_value(self, value: Any) -> bool:
        # Sanitizza il valore: rimuove l'unità se già presente per evitare duplicazioni nel log
        raw = str(value)
        if self.unit and raw.endswith(self.unit):
            display = raw
        elif self.unit:
            display = f"{raw}{self.unit}"
        else:
            display = raw
        logger.info(f"[Tool: {self.target_device}] Azionamento -> Nuovo valore: {display}")
        self._current_value = value
        return True


def get_tool(target_device: str, initial_value: Any = "OFF", unit: str = "") -> IoTDeviceTool:
    """
    Restituisce l'istanza singleton del tool per il dispositivo dato.
    Se non esiste ancora, la crea e la registra nel registry globale.
    """
    if target_device not in _TOOL_REGISTRY:
        _TOOL_REGISTRY[target_device] = IoTDeviceTool(
            target_device=target_device,
            initial_value=initial_value,
            unit=unit,
        )
    return _TOOL_REGISTRY[target_device]


def get_default_iot_tools() -> dict[str, BaseTool]:
    """Crea (o recupera) la mappa singleton di tool condivisi per tutte le periferiche note."""
    specs = [
        ("ac_living_room",     "OFF",      "°C"),
        ("heater_bedroom",     "OFF",      "°C"),
        ("front_door_lock",    "LOCKED",   "status"),
        ("alarm_system",       "DISARMED", "status"),
        ("living_room_lights", "OFF",      "%"),
    ]
    return {name: get_tool(name, init, unit) for name, init, unit in specs}
