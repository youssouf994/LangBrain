import logging
from typing import Any
from app.core import multiprocesso
from app.core.risultati import ErroreTool
from app.tools.reflex import Reflex

logger = logging.getLogger(__name__)

# Registry globale: garantisce un'unica istanza per dispositivo (Singleton per processo)
_TOOL_REGISTRY: dict[str, "SimulatedEffector"] = {}


class SimulatedEffector(Reflex):
    """
    Effettore simulato per sensori e attuatori IoT (reali o simulati).
    Condiviso sia dai sotto-agenti che dal Cervello per azionare direttamente i dispositivi.
    """
    def __init__(self, target_device: str, initial_value: Any = "OFF", unit: str = ""):
        super().__init__(target_device=target_device)
        self._current_value = initial_value
        self.unit = unit
        # Simulazione di guasti: messaggio dell'errore e numero di operazioni che falliranno (None = finché non viene rimosso).
        self.guasto: str | None = None
        self.guasti_residui: int | None = None
        self.guasto_solo_comandi = False
        self.ultimo_contatto = None  # datetime UTC dell'ultima lettura o comando riusciti

    @property
    def online(self) -> bool:
        """False con un guasto che blocca anche le letture (dispositivo che non risponde); i comandi soli non lo rendono offline."""
        return not self.guasto or self.guasto_solo_comandi

    def imposta_guasto(self, messaggio: str | None, operazioni: int | None = None, solo_comandi: bool = False) -> None:
        """
        Simula un dispositivo guasto: le prossime `operazioni` operazioni falliscono (None = finché non viene rimosso).
        Con `solo_comandi` le letture funzionano e falliscono solo i comandi (sensore attivo, attuatore bloccato).
        Con messaggio None il guasto viene rimosso.
        """
        self.guasto = messaggio
        self.guasti_residui = operazioni if messaggio else None
        self.guasto_solo_comandi = bool(messaggio) and solo_comandi

    def _verifica_guasto(self, operazione: str) -> None:
        if not self.guasto or (self.guasto_solo_comandi and operazione == "lettura"):
            return
        messaggio = self.guasto
        if self.guasti_residui is not None:
            self.guasti_residui -= 1
            if self.guasti_residui <= 0:
                self.guasto, self.guasti_residui, self.guasto_solo_comandi = None, None, False
        raise ErroreTool(messaggio, codice="SIMULATED_FAULT")

    async def get_tool_value(self) -> Any:
        self._verifica_guasto("lettura")
        self._segna_contatto()
        if multiprocesso.condiviso():  # più processi: il valore sta nel database, non nella memoria di uno solo
            valore = await multiprocesso.leggi(f"effettore:{self.target_device}", multiprocesso.ASSENTE)
            if valore is not multiprocesso.ASSENTE:
                self._current_value = valore
        return self._current_value

    def _segna_contatto(self) -> None:
        from app.db.schema import adesso_utc
        self.ultimo_contatto = adesso_utc()

    async def set_tool_value(self, value: Any) -> bool:
        self._verifica_guasto("comando")
        self._segna_contatto()
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
        if multiprocesso.condiviso():
            await multiprocesso.scrivi(f"effettore:{self.target_device}", value)
        return True


def get_tool(target_device: str, initial_value: Any = "OFF", unit: str = "") -> SimulatedEffector:
    """
    Restituisce l'istanza singleton del tool per il dispositivo dato.
    Se non esiste ancora, la crea e la registra nel registry globale.
    """
    if target_device not in _TOOL_REGISTRY:
        _TOOL_REGISTRY[target_device] = SimulatedEffector(
            target_device=target_device,
            initial_value=initial_value,
            unit=unit,
        )
    return _TOOL_REGISTRY[target_device]


def registra_tool(target_device: str, tool: Any) -> None:
    """
    Registra un tool personalizzato (es. uno che eredita da `Reflex`) nel registry condiviso del processo: da quel
    momento il Brain e gli altri agenti lo trovano anche se non sta nella mappa che hanno ricevuto alla costruzione.
    """
    _TOOL_REGISTRY[target_device] = tool


def tool_registrati() -> dict[str, Any]:
    """Copia della mappa dei tool registrati nel processo, compresi quelli creati on-demand."""
    return dict(_TOOL_REGISTRY)


def trova_tool(target_device: str, tools_map: dict[str, Any] | None = None) -> Any | None:
    """
    Cerca il tool di un dispositivo prima nella mappa dell'agente e poi nel registry globale: un tool creato
    on-demand (es. via API) dopo la costruzione dell'agente non compare nella copia della mappa che l'agente possiede.
    """
    if tools_map and target_device in tools_map:
        return tools_map[target_device]
    return _TOOL_REGISTRY.get(target_device)


def default_effectors() -> dict[str, Reflex]:
    """Crea (o recupera) la mappa singleton di tool condivisi per tutte le periferiche note."""
    specs = [
        ("ac_living_room",     "OFF",      "°C"),
        ("heater_bedroom",     "OFF",      "°C"),
        ("front_door_lock",    "LOCKED",   "status"),
        ("alarm_system",       "DISARMED", "status"),
        ("living_room_lights", "OFF",      "%"),
    ]
    return {name: get_tool(name, init, unit) for name, init, unit in specs}


# Alias dei vecchi nomi (retrocompatibilità)
IoTDeviceTool = SimulatedEffector
get_default_iot_tools = default_effectors
