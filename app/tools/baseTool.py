from abc import ABC, abstractmethod
from typing import Any

class BaseTool(ABC):
    """
    Classe base astratta per tutti i tool hardware/mock IoT del sistema.
    """
    def __init__(self, target_device: str, name: str | None = None):
        self.target_device = target_device
        self.name = name or f"tool_{target_device}"

    @abstractmethod
    async def get_tool_value(self) -> Any:
        """Legge il valore attuale dal sensore o attuatore."""
        pass

    @abstractmethod
    async def set_tool_value(self, value: Any) -> bool:
        """Invia un comando di azionamento all'attuatore."""
        pass