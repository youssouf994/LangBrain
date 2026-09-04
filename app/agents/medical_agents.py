"""
Agenti Fisiologici: Organo Cardiovascolare ed Organo Respiratorio.
Simulano la risposta dell'omeostasi clinica ed intervengono in caso di patologia (Tachicardia, Ipossia).
"""

import logging
from typing import Any
from langchain_core.messages import AIMessage

from app.agents.base_agent import BaseAgent
from app.graph.state import GraphState
from app.tools.medical_tools import HeartRateRegulatorTool, LungVentilatorTool

logger = logging.getLogger(__name__)


class CardiovascularOrganAgent(BaseAgent):
    """Agente di Livello 1 per l'Organo Cardiovascolare (gestisce il pacemaker)."""

    def __init__(self, tools: dict[str, Any] | None = None):
        super().__init__(
            name="organ_cardiovascular",
            managed_targets=["cardiac_pacemaker"],
            conflict_window_minutes=15,
            priority_weight=700.0,
        )
        self.tools = tools or {}

    async def process(
        self,
        state: GraphState,
        recent_events: list[dict],
        relevant_readings: list[dict],
        agent_escalations: list[dict],
    ) -> dict[str, Any]:
        pacemaker_tool = self.tools.get("cardiac_pacemaker", HeartRateRegulatorTool())
        norm_result = pacemaker_tool.normalize_current_state()

        logger.info(f"[{self.name}] Analisi Omeostasi Cardiaca: {norm_result}")

        # Se la frequenza cardiaca è patologica (fuori norma)
        if not norm_result["is_in_range"]:
            target_bpm = f"{norm_result['recommended_target']} BPM"
            reasoning = f"Patologia Tachicardia/Aritmia rilevata. Score normalizzato: {norm_result['normalized_score']}. Ripristino target omeostatico a {target_bpm}."

            # Se la patologia è severa (|Z| > 1.5), invia un'escalation al Brain (SNC)
            if abs(norm_result["normalized_score"]) > 1.5:
                escalation = self.create_escalation(
                    target_device="cardiac_pacemaker",
                    proposed_action=target_bpm,
                    reason=reasoning,
                    conflict_detected=True,
                )
                await self.event_log.log_event(
                    actor=self.name,
                    action="CRITICAL_ARHYTHMIA_ESCALATION",
                    target="cardiac_pacemaker",
                    old_value=await pacemaker_tool.get_tool_value(),
                    new_value=target_bpm,
                    reasoning=reasoning,
                    escalated=True,
                )
                return {
                    "next_agent": "brain",
                    "pending_escalations": [escalation],
                    "messages": [AIMessage(content=f"[{self.name}] Escalation inviata al Cervello per Tachicardia Severa ({norm_result['raw_value']} BPM).")],
                }

            # Altrimenti applica direttamente il ripristino omeostatico
            applied = await self.apply_status(
                target="cardiac_pacemaker",
                action="HOMEOSTASIS_BPM_RESTORATION",
                new_value=target_bpm,
                reasoning=reasoning,
                escalated=False,
                tools_map=self.tools,
            )
            return {
                "next_agent": "END",
                "messages": [AIMessage(content=f"[{self.name}] Omeostasi Cardiaca Ripristinata: {norm_result['raw_value']} -> {target_bpm} (applied={applied}).")],
            }

        return {
            "next_agent": "END",
            "messages": [AIMessage(content=f"[{self.name}] Frequenza cardiaca fisiologicamente stabile ({norm_result['raw_value']} BPM).")],
        }


class RespiratoryOrganAgent(BaseAgent):
    """Agente di Livello 1 per l'Organo Respiratorio (gestisce il ventilatore/ossigeno)."""

    def __init__(self, tools: dict[str, Any] | None = None):
        super().__init__(
            name="organ_respiratory",
            managed_targets=["oxygen_regulator"],
            conflict_window_minutes=15,
            priority_weight=600.0,
        )
        self.tools = tools or {}

    async def process(
        self,
        state: GraphState,
        recent_events: list[dict],
        relevant_readings: list[dict],
        agent_escalations: list[dict],
    ) -> dict[str, Any]:
        ventilator_tool = self.tools.get("oxygen_regulator", LungVentilatorTool())
        norm_result = ventilator_tool.normalize_current_state()

        logger.info(f"[{self.name}] Analisi Omeostasi Respiratoria: {norm_result}")

        # Se l'ossigenazione è patologica (Ipossia)
        if not norm_result["is_in_range"]:
            target_spo2 = f"{norm_result['recommended_target']}%"
            reasoning = f"Patologia Ipossia acuta rilevata (SpO2={norm_result['raw_value']}%). Ripristino ventilazione target a {target_spo2}."

            applied = await self.apply_status(
                target="oxygen_regulator",
                action="HOMEOSTASIS_OXYGEN_RESTORATION",
                new_value=target_spo2,
                reasoning=reasoning,
                escalated=False,
                tools_map=self.tools,
            )
            return {
                "next_agent": "END",
                "messages": [AIMessage(content=f"[{self.name}] Omeostasi Respiratoria Ripristinata: {norm_result['raw_value']}% -> {target_spo2} (applied={applied}).")],
            }

        return {
            "next_agent": "END",
            "messages": [AIMessage(content=f"[{self.name}] Saturazione SpO2 fisiologicamente stabile ({norm_result['raw_value']}%).")],
        }
