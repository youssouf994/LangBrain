"""
Modulo DynamicAgent — Agente di Dominio Parametrico Gerarchico.
Rappresenta un nodo in una gerarchia ad albero a N livelli:
  Cervello (Livello 0) -> Organi (Livello 1) -> Componenti (Livello 2) -> Sotto-Componenti (Livello N).
"""

import logging
from typing import Any
from langchain_core.messages import AIMessage

from app.agents.base_agent import BaseAgent
from app.core.constants import is_control_flag
from app.graph.state import GraphState
from app.tools.sensor_tools import get_default_iot_tools

logger = logging.getLogger(__name__)


class DynamicAgent(BaseAgent):
    """
    Agente dinamico configurabile a runtime.
    Può fungere da Organo o Componente dell'Organo, delegando verso i propri sotto-agenti
    o escalando verso il proprio agente Padre (parent_agent_name).
    """

    def __init__(
        self,
        name: str,
        managed_targets: list[str],
        parent_agent_name: str | None = "Brain",
        sub_agent_names: list[str] | None = None,
        level: int = 1,
        system_prompt_template: str | None = None,
        user_prompt_template: str | None = None,
        conflict_window_minutes: int = 30,
        priority_weight: float = 1.0,
        tools: dict[str, Any] | None = None,
    ):
        super().__init__(
            name=name,
            managed_targets=managed_targets,
            conflict_window_minutes=conflict_window_minutes,
            priority_weight=priority_weight,
        )
        self.parent_agent_name = parent_agent_name
        self.sub_agent_names = sub_agent_names or []
        self.level = level
        self.system_prompt_template = system_prompt_template or (
            f"Sei l'agente gerarchico '{name}' (Livello {level}). "
            f"Gestisci i target {managed_targets}. "
            "Devi decidere se eseguire un'azione (ACTION), fare escalation al Padre (ESCALATE), o fare nulla (NONE).\n"
            "Rispondi ESATTAMENTE nel formato:\n"
            "DECISIONE: [ACTION|ESCALATE|NONE]\n"
            "MOTIVAZIONE: [spiegazione]"
        )
        self.user_prompt_template = user_prompt_template or (
            "Target primario: {target}\n"
            "Stato attuale letto dal tool: {current_status}\n"
            "Conflitto non riconciliato nel DB: {has_conflict}\n"
            "Riconciliazione recente: {recently_reconciled}\n"
            "Letture sensori rilevanti: {relevant_readings}\n"
            "Storico eventi recenti: {recent_events}\n"
            "Qual è la decisione corretta?"
        )
        self.tools = tools or get_default_iot_tools()

    async def process(
        self,
        state: GraphState,
        recent_events: list[dict],
        relevant_readings: list[dict],
        agent_escalations: list[dict],
    ) -> dict[str, Any]:
        logger.info(f"[{self.name}] [Livello {self.level}] Esecuzione analisi dinamica...")

        # Se non ha target diretti, delega al primo sotto-agente se presente
        if not self.managed_targets:
            next_target = self.sub_agent_names[0] if self.sub_agent_names else (self.parent_agent_name or "END")
            return {"next_agent": next_target}

        primary_target = self.managed_targets[0]
        tool_obj = self.tools.get(primary_target)

        # 1. Readout stato reale dal tool
        current_status = "OFF"
        if tool_obj:
            try:
                current_status = str(await tool_obj.get_tool_value())
            except Exception as e:
                logger.error(f"[{self.name}] Errore lettura tool {primary_target}: {e}")

        # 2. Cortocircuito se il dispositivo è in uno stato di blocco
        if is_control_flag(current_status):
            logger.info(f"[{self.name}] Target '{primary_target}' in stato di blocco '{current_status}'. In attesa sblocco.")
            return {
                "next_agent": self.parent_agent_name or "END",
                "messages": [AIMessage(content=f"[{self.name}] {primary_target} in blocco ({current_status}). In attesa.")],
            }

        # 3. Controllo conflitti nel DB
        reconciled_events = [e for e in recent_events if str(e.get("action", "")).startswith("RECONCILED_")]
        recently_reconciled = any(e.get("target") == primary_target for e in reconciled_events)
        unresolved_events = [e for e in recent_events if not str(e.get("action", "")).startswith("RECONCILED_")]

        has_conflict, conflict_event = self.check_for_recent_conflict(primary_target, unresolved_events)

        # 4. Prompting MAO
        user_prompt = self.user_prompt_template.format(
            target=primary_target,
            current_status=current_status,
            has_conflict=has_conflict,
            recently_reconciled=recently_reconciled,
            relevant_readings=relevant_readings,
            recent_events=recent_events,
        )

        ai_response = self.ask_brain(self.system_prompt_template, user_prompt, temperature=0.0, max_tokens=2048)
        ai_response_str = ai_response.strip().upper()
        logger.info(f"[{self.name}] Risposta AI: {ai_response_str}")

        update: dict[str, Any] = {}

        # 5. Gestione decisione ed Escalation verso il Padre
        if "DECISIONE: ESCALATE" in ai_response_str and has_conflict and not recently_reconciled:
            reason = f"Conflitto/anomalia rilevata da {self.name}: {ai_response}"
            logger.warning(f"[{self.name}] Escalation inviata a '{self.parent_agent_name}' per {primary_target}")

            escalation = self.create_escalation(
                target_device=primary_target,
                proposed_action="DESIRED_ACTION",
                reason=reason,
                conflict_detected=has_conflict,
                context_events=[conflict_event] if conflict_event else [],
            )

            await self.event_log.log_event(
                actor=self.name,
                action="ESCALATION_PROPOSED",
                target=primary_target,
                old_value=current_status,
                new_value="ESCALATION_PENDING",
                reasoning=reason,
                escalated=True,
            )

            update["pending_escalations"] = [escalation]
            update["next_agent"] = self.parent_agent_name or "brain"
            update["messages"] = [AIMessage(content=f"[{self.name}] Escalation inviata a {self.parent_agent_name} per {primary_target}.")]

        elif "DECISIONE: ACTION" in ai_response_str:
            reasoning = f"Azione consigliata da AI in {self.name}: {ai_response_str}"
            applied = await self.apply_status(
                target=primary_target,
                action="DYNAMIC_ACTION",
                new_value="ON",
                reasoning=reasoning,
                escalated=False,
                tools_map=self.tools,
            )
            update["messages"] = [AIMessage(content=f"[{self.name}] Azione eseguita su {primary_target} (applied={applied}).")]
            update["next_agent"] = self.parent_agent_name or "END"
        else:
            logger.info(f"[{self.name}] Nessuna azione richiesta.")
            update["next_agent"] = self.parent_agent_name or "END"
            update["messages"] = [AIMessage(content=f"[{self.name}] Nessuna azione necessaria.")]

        return update
