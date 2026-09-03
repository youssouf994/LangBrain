"""
Tool Biometrici e Funzioni Deterministiche di Normalizzazione Fisiologica.
"""

from typing import Any
from app.tools.baseTool import BaseTool


def deterministic_biometric_normalizer(raw_value: float, min_optimal: float, max_optimal: float) -> dict[str, Any]:
    r"""
    Funzione deterministica per la normalizzazione dei parametri vitali.
    Calcola l'indice di deviazione dallo stato omeostatico ideale.
    
    Formula di normalizzazione:
    $$N = \frac{V_{\text{raw}} - V_{\text{mid}}}{\text{half\_range}}$$
    """
    optimal_mid = (min_optimal + max_optimal) / 2.0
    half_range = (max_optimal - min_optimal) / 2.0
    
    normalized_score = (raw_value - optimal_mid) / half_range if half_range != 0 else 0.0
    is_normal = min_optimal <= raw_value <= max_optimal
    target_value = max(min_optimal, min(raw_value, max_optimal)) if not is_normal else raw_value
        
    return {
        "raw_value": raw_value,
        "optimal_range": f"{min_optimal}-{max_optimal}",
        "normalized_score": round(normalized_score, 3),
        "is_in_range": is_normal,
        "recommended_target": round(target_value, 2)
    }


class HeartRateRegulatorTool(BaseTool):
    """Tool per la regolazione del pacemaker cardiaco (BPM)."""

    def __init__(self, target_device: str = "cardiac_pacemaker"):
        super().__init__(target_device=target_device)
        self.bpm = 72.0

    async def get_tool_value(self) -> str:
        return f"{self.bpm} BPM"

    async def set_tool_value(self, value: Any) -> bool:
        clean_val = float(str(value).replace("BPM", "").strip())
        self.bpm = clean_val
        return True

    def normalize_current_state(self) -> dict[str, Any]:
        return deterministic_biometric_normalizer(self.bpm, min_optimal=60.0, max_optimal=100.0)


class LungVentilatorTool(BaseTool):
    """Tool per la regolazione dell'ossigenazione (SpO2)."""

    def __init__(self, target_device: str = "oxygen_regulator"):
        super().__init__(target_device=target_device)
        self.spo2 = 98.0

    async def get_tool_value(self) -> str:
        return f"{self.spo2}%"

    async def set_tool_value(self, value: Any) -> bool:
        clean_val = float(str(value).replace("%", "").strip())
        self.spo2 = clean_val
        return True

    def normalize_current_state(self) -> dict[str, Any]:
        return deterministic_biometric_normalizer(self.spo2, min_optimal=95.0, max_optimal=100.0)