import logging
from typing import Any
from langchain_core.messages import AIMessage

from app.agents.base_agent import BaseAgent
from app.graph.state import GraphState
from app.tools.baseTool import BaseTool
from app.tools.sensor_tools import get_default_iot_tools

logger = logging.getLogger(__name__)

class BrainAgent(BaseAgent):
    """
    Orchestratore Supremo (Cervello / Padre).
    Possiede i privilegi massimi, esegue il readout iniziale dei tool e gestisce le escalation.
    """

    def __init__(self, tools: list[BaseTool] | None = None):
        super().__init__(
            name="Brain", 
            managed_targets=["all"], 
            conflict_window_minutes=240, 
            priority_weight=1000.0
        )
        if tools:
            self.tools: dict[str, BaseTool] = {tool.target_device: tool for tool in tools}
        else:
            self.tools = get_default_iot_tools()

        # Prompt configurabili da .env
        import os
        self.system_prompt = (
            os.getenv("BRAIN_SYSTEM_PROMPT")
            or (
                "Sei l'Orchestratore Supremo della Smart Home. "
                "Hai ricevuto un'escalation da un sotto-agente per un conflitto o un'anomalia. "
                "Hai visibilità su tutte le letture dei sensori delle ultime ore e sugli eventi recenti. "
                "Valuta il contesto e decidi se APPROVARE o RESPINGERE l'azione.\n"
                "Formato Risposta:\n"
                "DECISIONE: [APPROVA|RESPINGI]\n"
                "MOTIVAZIONE: [spiegazione]"
            )
        )
        self.user_prompt_template = (
            os.getenv("BRAIN_USER_PROMPT_TEMPLATE")
            or (
                "Agente Richiedente: {source}\n"
                "Dispositivo Target: {target}\n"
                "Azione Proposta: {action}\n"
                "Motivo Escalation: {reason}\n"
                "Letture Sensori Reali: {readings}\n"
                "Storico Eventi Recenti: {recent_events}\n"
                "Qual è la risoluzione corretta?"
            )
        )

    async def process(
        self, 
        state: GraphState, 
        recent_events: list[dict], 
        relevant_readings: list[dict], 
        agent_escalations: list[dict]
    ) -> dict[str, Any]:
        """
        Ciclo principale del Padre:
        1. Esegue il readout di tutti i tool per aggiornare le letture attuali nel GraphState.
        2. Riconcilia eventuali escalation aperte dei sotto-agenti.
        3. Smista al sotto-agente o termina se il sistema è stabile.
        """
        logger.info(f"[{self.name}] Avvio ciclo Orchestratore Padre (Escalation aperte: {len(agent_escalations)})")
        updates: dict[str, Any] = {}
        
        # --- FIX 1 & 2: Readout reale di tutti i tool registrati ---
        current_readings_map = {}
        for device_name, tool_obj in self.tools.items():
            try:
                val = await tool_obj.get_tool_value()
                current_readings_map[device_name] = {
                    "sensor_id": device_name,
                    "agent_owner": self.name,
                    "value": str(val),
                    "unit": getattr(tool_obj, 'unit', '')
                }
            except Exception as e:
                logger.error(f"[{self.name}] Errore durante il readout del tool '{device_name}': {e}")

        # Inserisce le letture fresche nello stato
        if current_readings_map:
            updates["readings"] = list(current_readings_map.values())

        pending_escalations = state.get("pending_escalations", [])

        # --- CASO 1: Gestione Escalation Pendenti (Reconciliation) ---
        if pending_escalations:
            logger.info(f"[{self.name}] Inizio Reconciliation per {len(pending_escalations)} escalation...")
            resolved_messages = []

            for esc in pending_escalations:
                source = esc.get("source_agent")
                target = esc.get("target_device")
                action = esc.get("proposed_action")
                reason = esc.get("reason")

                system_prompt = self.system_prompt
                user_prompt = self.user_prompt_template.format(
                    source=source,
                    target=target,
                    action=action,
                    reason=reason,
                    readings=current_readings_map.get(target, {}),
                    recent_events=recent_events,
                )

                hitl_targets = state.get("config", {}).get("hitl_targets", ["alarm_system", "front_door_lock"])
                use_hitl = state.get("hitl_required", False) or (target in hitl_targets) or state.get("config", {}).get("hitl_all", False)

                if use_hitl:
                    logger.warning(f"[{self.name}] HITL ATTIVO — Invocazione interrupt(). Grafo in PAUSA per target '{target}' ({action})...")
                    from langgraph.types import interrupt
                    human_payload = interrupt({
                        "type": "escalation_approval_request",
                        "target_device": target,
                        "proposed_action": action,
                        "source_agent": source,
                        "reason": reason,
                        "prompt": f"Approvazione Umana Richiesta per '{target}': l'agente '{source}' propone '{action}'. Motivo: {reason}."
                    })

                    if isinstance(human_payload, dict):
                        decision_val = str(human_payload.get("decision", "RESPINGI")).upper()
                        decision_reason = human_payload.get("reasoning", "Decisione fornita dall'utente via HITL API")
                    else:
                        decision_val = str(human_payload).upper()
                        decision_reason = "Decisione fornita dall'utente via HITL API"

                    if "APPROVA" in decision_val or "APPROVE" in decision_val or "YES" in decision_val:
                        decision_response = f"DECISIONE: APPROVA\nMOTIVAZIONE: {decision_reason}"
                    else:
                        decision_response = f"DECISIONE: RESPINGI\nMOTIVAZIONE: {decision_reason}"
                else:
                    decision_response = self.ask_brain(system_prompt, user_prompt, temperature=0.0, max_tokens=2048)

                logger.info(f"[{self.name}] Risoluzione (AI/HITL) per {target}: {decision_response}")

                if "DECISIONE: APPROVA" in decision_response.upper():
                    applied = await self.apply_status(
                        target=target,
                        action=f"RECONCILED_{action}",
                        new_value=action,   # action è già il valore canonico (es. "22.5°C")
                        reasoning=f"Approvato da Orchestratore Padre. Detail: {decision_response}",
                        escalated=False,
                        tools_map=self.tools
                    )
                    # Fix 1: invalida gli eventi ESCALATION_PROPOSED nel DB per bloccare il loop
                    await self.event_log.mark_resolved(target)
                    status_text = "APPLICATA" if applied else "SALTATA (già a regime)"
                    resolved_messages.append(f"[{self.name}] Escalation APPROVATA [{status_text}] per {target} ({action}).")
                else:
                    await self.apply_status(
                        target=target,
                        action=f"REJECTED_{action}",
                        new_value="REJECTED",
                        reasoning=f"Respinto da Orchestratore Padre. Detail: {decision_response}",
                        escalated=False,
                        tools_map=self.tools
                    )
                    # Fix 1: invalida gli eventi ESCALATION_PROPOSED anche se l'azione viene respinta
                    await self.event_log.mark_resolved(target)
                    resolved_messages.append(f"[{self.name}] Escalation RESPINTA per {target} su richiesta di {source}.")

            # Svuota le escalation e chiude il ciclo
            updates["pending_escalations"] = []
            updates["messages"] = [AIMessage(content="\n".join(resolved_messages))]
            updates["next_agent"] = "END"
            return updates

        # --- CASO 2: Avvio da START (Padre che smista verso il sotto-agente) ---
        target_agent = state.get("next_agent", "END")
        if target_agent != "END" and target_agent != "brain":
            logger.info(f"[{self.name}] Status check ok. Smistamento verso sotto-agente '{target_agent}'...")
            updates["next_agent"] = target_agent
        elif target_agent == "brain":
            # Se la richiesta parte da brain, delega al primo sotto-agente o ad agent_climate
            first_sub = next((k for k in self.tools.keys() if k != "all"), "agent_climate")
            updates["next_agent"] = state.get("config", {}).get("default_sub_agent", "agent_climate")
        else:
            updates["next_agent"] = "END"

        return updates

    async def check_body_status(
        self, 
        state: GraphState, 
        relevant_readings: list[dict], 
        recent_events: list[dict]
    ) -> dict[str, Any]:
        """
        Analisi Periodica Macro (Health Check / Trend della casa).
        """
        logger.info(f"[{self.name}] Esecuzione check_body_status (Analisi Macro Trend)...")

        system_prompt = (
            "Sei l'Orchestratore Supremo. Stai eseguendo il controllo di routine dello stato globale della casa. "
            "Esamina le letture reali e gli eventi recenti. "
            "Se trovi inefficienze macro, indica l'azione da intraprendere.\n"
            "Formato Risposta:\n"
            "STATUS: [OK|MACRO_ADJUSTMENT_REQUIRED]\n"
            "DETTAGLI: [spiegazione]"
        )

        user_prompt = (
            f"Letture reali: {relevant_readings}\n"
            f"Eventi recenti: {recent_events}\n"
            "Valuta la situazione macro dell'abitazione."
        )

        macro_response = self.ask_brain(system_prompt, user_prompt, temperature=0.1, max_tokens=2048)
        logger.info(f"[{self.name}] Risultato check_body_status: {macro_response}")

        if "MACRO_ADJUSTMENT_REQUIRED" in macro_response.upper():
            await self.apply_status(
                target="ac_living_room",
                action="MACRO_ECO_MODE",
                new_value="24°C",
                reasoning=f"Regolazione macro da check_body_status: {macro_response}",
                escalated=False,
                tools_map=self.tools
            )

        updates: dict[str, Any] = {
            "messages": [AIMessage(content=f"[{self.name}] Macro Check Completato: {macro_response}")]
        }
        return updates