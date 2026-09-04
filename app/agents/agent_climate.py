import logging
from typing import Any
from langchain_core.messages import AIMessage

from app.agents.base_agent import BaseAgent
from app.core.constants import is_control_flag
from app.graph.state import GraphState
from app.tools.sensor_tools import get_default_iot_tools

logger = logging.getLogger(__name__)

# Valore canonico di setpoint per il climatizzatore (Fix 4 — schema comandi unico)
AC_TARGET_VALUE = "22.5°C"


class ClimateAgent(BaseAgent):
    """
    Sotto-agente specializzato nella gestione del clima con lettura tool reali e prevenzione ridondanze.
    """

    def __init__(self, tools: dict[str, Any] | None = None):
        super().__init__(
            name="agent_climate", 
            managed_targets=["ac_living_room", "heater_bedroom"], 
            conflict_window_minutes=30, 
            priority_weight=0.001
        )
        self.tools = tools or get_default_iot_tools()

    async def process(
        self, 
        state: GraphState, 
        recent_events: list[dict], 
        relevant_readings: list[dict], 
        agent_escalations: list[dict]
    ) -> dict[str, Any]:
        logger.info(f"[{self.name}] Avvio analisi Clima...")

        target_device = "ac_living_room"
        tool_obj = self.tools.get(target_device)
        
        # 1. Legge lo stato reale dal tool del dispositivo
        current_status = "OFF"
        if tool_obj:
            try:
                current_status = str(await tool_obj.get_tool_value())
            except Exception as e:
                logger.error(f"[{self.name}] Errore lettura tool {target_device}: {e}")

        # 2. Recupera l'ultima lettura sensore dallo stato (o usa default)
        current_temp = 28.5
        for reading in relevant_readings:
            if reading.get("sensor_id") in ["temp_living_room", "ac_living_room"]:
                try:
                    current_temp = float(reading.get("value", 28.5))
                except (ValueError, TypeError):
                    pass
                break

        # 3a. Guardia di cortocircuito: se il dispositivo è in uno stato di blocco
        #     impostato dal Brain (es. REJECTED), non rivalutare fino a un nuovo
        #     evento fisico esterno. Evita il loop di re-escalation.
        if is_control_flag(current_status):
            logger.info(
                f"[{self.name}] Dispositivo '{target_device}' in stato di blocco '{current_status}'. "
                "Nessuna azione finché non arriva un nuovo evento fisico."
            )
            return {
                "next_agent": "END",
                "messages": [AIMessage(content=f"[{self.name}] {target_device} bloccato ({current_status}). In attesa di sblocco esterno.")],
            }

        # 3b. Trova l'evento più recente in ordine cronologico per questo target
        target_events = [e for e in recent_events if e.get("target") == target_device]
        target_events.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)

        has_conflict = False
        recently_reconciled = False
        conflict_event = None

        if target_events:
            latest_ev = target_events[0]
            latest_act = str(latest_ev.get("action", ""))
            latest_actor = str(latest_ev.get("actor", ""))

            if latest_act.startswith("RECONCILED_") or latest_act.startswith("RESOLVED_") or latest_act.startswith("UNBLOCKED"):
                recently_reconciled = True
            elif (latest_act in ["FORCE_SHUTDOWN", "SECURITY_LOCK"] or latest_act.startswith("REJECTED_")) and latest_actor != self.name:
                has_conflict = True
                conflict_event = latest_ev

        # 4. Costruisce il prompt con le regole gerarchiche esplicite
        system_prompt = (
            "Sei un sotto-agente esperto di domotica per il controllo del Clima. "
            "Devi decidere se ATTIVARE l'aria condizionata (ACTION), FARE ESCALATION al Cervello (ESCALATE), o NON FARE NULLA (NONE). "
            "\n\nRegole importanti:\n"
            "- Scegli ACTION se la temperatura supera la soglia e il dispositivo NON e' gia' al valore desiderato.\n"
            "- Scegli NONE se il dispositivo e' gia' al valore desiderato o la temperatura e' sotto soglia.\n"
            "- Scegli ESCALATE SOLO se c'e' un conflitto reale non ancora risolto (azione recente di un agente con priorita' superiore NON riconciliata).\n"
            "- Se esiste un evento 'RECONCILED_*' recente per il target, il conflitto e' gia' stato risolto: NON escalare.\n"
            "Catalogo valori validi:\n"
            "- ac_living_room: ['OFF', '22.5°C']\n"
            "- heater_bedroom: ['OFF', '22.5°C']\n"
            "Rispondi ESATTAMENTE nel seguente formato:\n"
            "DECISIONE: [ACTION|ESCALATE|NONE]\n"
            "MOTIVAZIONE: [spiegazione sintetica]"
        )
        user_prompt = (
            f"Target: {target_device}\n"
            f"Stato Attuale del Tool (valore reale letto ora): {current_status}\n"
            f"Valore Desiderato: {AC_TARGET_VALUE}\n"
            f"Temperatura Rilevata: {current_temp}°C (Soglia: 25.0°C)\n"
            f"Conflitto Non Riconciliato nel DB: {has_conflict}\n"
            f"Riconciliazione Recente Trovata: {recently_reconciled}\n"
            f"Evento di Conflitto: {conflict_event}\n"
            f"Storico Completo Recente: {recent_events}\n"
            "Qual e' la decisione corretta?"
        )

        ai_response = await self.ask_brain(system_prompt, user_prompt, temperature=0.0, max_tokens=2048)
        ai_response_str = ai_response.strip().upper()
        logger.info(f"[{self.name}] Risposta AI: {ai_response_str}")

        update: dict[str, Any] = {}

        # 5. Gestione della decisione: se c'è un conflitto non riconciliato con un altro agente (es. Sicurezza), ESCALA SEMPRE al Cervello
        if has_conflict and not recently_reconciled:
            reason = f"Conflitto attivo rilevato con '{conflict_event.get('actor', 'Sicurezza')}': {conflict_event.get('action', 'FORCE_SHUTDOWN')}"
            logger.warning(f"[{self.name}] Escalation forzata al Cervello per {target_device}: {reason}")

            escalation = self.create_escalation(
                target_device=target_device,
                proposed_action=AC_TARGET_VALUE,
                reason=reason,
                conflict_detected=True,
                context_events=[conflict_event] if conflict_event else []
            )

            await self.event_log.log_event(
                actor=self.name,
                action="ESCALATION_PROPOSED",
                target=target_device,
                old_value=current_status,
                new_value=AC_TARGET_VALUE,
                reasoning=reason,
                escalated=True,
            )

            update["pending_escalations"] = [escalation]
            update["next_agent"] = "brain"
            update["messages"] = [AIMessage(content=f"[{self.name}] Conflitto rilevato. Escalation inviata al Cervello per {target_device}.")]

        elif "DECISIONE: ACTION" in ai_response_str and not has_conflict:
            reasoning = f"Attivazione consigliata da AI: {ai_response_str}"

            applied = await self.apply_status(
                target=target_device,
                action="TURN_ON_AC",
                new_value=AC_TARGET_VALUE,
                reasoning=reasoning,
                escalated=False,
                tools_map=self.tools
            )

            if applied:
                logger.info(f"[{self.name}] Azione eseguita: {current_status} -> {AC_TARGET_VALUE} su {target_device}")
                update["messages"] = [AIMessage(content=f"[{self.name}] AC attivata su {target_device}: {current_status} -> {AC_TARGET_VALUE}.")]
            else:
                logger.info(f"[{self.name}] Azione saltata: {target_device} gia' impostata a {AC_TARGET_VALUE} (In stabilizzazione).")
                update["messages"] = [AIMessage(content=f"[{self.name}] {target_device} gia' impostata a {AC_TARGET_VALUE}. In stabilizzazione.")]

            update["next_agent"] = "END"
        else:
            logger.info(f"[{self.name}] Nessuna azione richiesta.")
            update["next_agent"] = "END"
            update["messages"] = [AIMessage(content=f"[{self.name}] Sistema stabile. Nessuna azione necessaria.")]

        return update
