import json
import logging
from typing import Any
from langchain_core.messages import AIMessage

from app.agents.base_agent import BaseAgent
from app.graph.state import GraphState
from app.tools.baseTool import BaseTool
from app.tools.sensor_tools import get_default_iot_tools, get_tool
from app.tools.tool_wrapper import force_execute_tool

logger = logging.getLogger(__name__)


def _tool_value_catalog(tools: dict[str, Any] | None = None) -> str:
    """Genera una checklist dei valori validi per gli attuatori in modo da guidare l'LLM."""
    tools = tools or {}
    catalog: list[str] = []
    for name, tool_obj in tools.items():
        device = str(name).lower()
        if "valve" in device or "air" in device:
            valid = ["OPEN", "CLOSED", "100%"]
        elif "breaker" in device:
            valid = ["ON", "OFF"]
        elif "light" in device or "lamp" in device:
            valid = ["ON", "OFF"]
        elif "lock" in device or "door" in device:
            valid = ["LOCKED", "UNLOCKED"]
        elif "alarm" in device:
            valid = ["ARMED", "DISARMED"]
        else:
            current = getattr(tool_obj, "_current_value", None)
            valid = [str(v) for v in [current, "OFF", "ON", "OPEN", "CLOSED", "100%", "22.5°C"] if v not in (None, "")]
        deduped: list[str] = []
        for value in valid:
            if value not in deduped:
                deduped.append(value)
        catalog.append(f"- {name}: {deduped}")
    return "\n".join(catalog) if catalog else "- Nessun tool registrato."


def _normalize_action_value(target: str, action: str, explicit_value: Any = None, fallback_value: Any = None) -> Any:
    """Converte un comando in un valore di dispositivo reale; ignora flag/nomi di azione che non sono stati fisici."""
    action_name = str(action or "").upper()
    if action_name in {"TURN_ON", "TURN_OFF"}:
        return "ON" if action_name == "TURN_ON" else "OFF"

    candidate = explicit_value if explicit_value not in (None, "", "NULL", "null", "NONE", "none") else fallback_value
    candidate_str = str(candidate).strip() if candidate is not None else ""
    invalid_tokens = {
        "TURN_ON", "TURN_OFF", "FORCE_SHUTDOWN", "SECURITY_LOCK", "UNBLOCK_AND_SET",
        "REJECTED", "RECONCILED", "RESOLVED", "BLOCKED", "ESCALATION_PROPOSED", "NULL", "NONE"
    }
    if candidate_str and candidate_str.upper() not in invalid_tokens:
        return candidate

    device_name = str(target).upper()
    if "VALVE" in device_name or "AIR" in device_name:
        return "100%"
    if "BREAKER" in device_name or "LIGHT" in device_name or "LOCK" in device_name or "ALARM" in device_name:
        return "ON"
    return "ON"


# System prompt dedicato all'Arbitrato Semantico (Brain Override)
_OVERRIDE_SYSTEM_PROMPT = (
    "Sei il modulo di Arbitrato Semantico (Brain Override) di un sistema IoT N-Tier.\n"
    "Il tuo compito è tradurre una direttiva umana complessa in un array JSON eseguibile.\n"
    "L'utente può richiedere azioni multiple, come ignorare blocchi di sicurezza, accendere o spegnere dispositivi.\n\n"
    "Checklist obbligatoria prima di generare ogni comando:\n"
    "1. Identifica target e azione corretta.\n"
    "2. Verifica i valori validi del device e usa solo quelli del catalogo.\n"
    "3. Se l'azione è TURN_ON -> il valore fisico deve essere ON.\n"
    "4. Se l'azione è TURN_OFF -> il valore fisico deve essere OFF.\n"
    "5. Non usare nomi di azione come valori (es. FORCE_SHUTDOWN, TURN_ON, TURN_OFF).\n\n"
    "REGOLE TASSATIVE:\n"
    '1. Restituisci ESCLUSIVAMENTE un array JSON valido. Niente markdown, niente backticks (```json), nessuna parola di saluto.\n'
    "2. Il formato di ogni oggetto deve essere esattamente:\n"
    '   {"target": "nome_del_dispositivo", "action": "UNBLOCK_AND_SET"|"TURN_OFF"|"TURN_ON", "value": "stringa o null"}\n'
    "3. Se l'utente chiede esplicitamente di ignorare un vincolo, sbloccare, o forzare un'accensione con un valore specifico, usa l'azione UNBLOCK_AND_SET.\n"
    "4. Usa il catalogo dei valori validi del dispositivo e non inventare stati arbitrari.\n"
    "Catalogo valori validi per i tool:\n"
    "{tool_catalog}\n"
)

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
        self.tool_value_catalog = _tool_value_catalog(self.tools)
        self.system_prompt = (
            os.getenv("BRAIN_SYSTEM_PROMPT")
            or (
                "Sei l'Orchestratore Supremo della Smart Home. "
                "Hai ricevuto un'escalation da un sotto-agente per un conflitto o un'anomalia. "
                "Hai visibilità su tutte le letture dei sensori delle ultime ore e sugli eventi recenti. "
                "Compila una checklist: target, azione proposta, valore valido per device, decisione finale.\n"
                "Catalogo valori validi per i device:\n"
                f"{self.tool_value_catalog}\n"
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

        pending_escalations = list(state.get("pending_escalations", []))

        # Se non ci sono escalation pendenti nello stato del grafo, verifica conflitti non ancora risolti nel DB
        if not pending_escalations:
            unresolved_events = [
                e for e in recent_events
                if not str(e.get("action", "")).startswith("RECONCILED_")
                and not str(e.get("action", "")).startswith("RESOLVED_")
                and not str(e.get("action", "")).startswith("UNBLOCKED")
                and (str(e.get("action", "")).startswith("FORCE_") or e.get("escalated", False))
            ]
            for ev in unresolved_events:
                pending_escalations.append({
                    "source_agent": ev.get("actor", "agent_security"),
                    "target_device": ev.get("target"),
                    "proposed_action": ev.get("action"),
                    "reason": ev.get("reasoning", "Conflitto non riconciliato nel DB audit log"),
                })

        # --- CASO 1: Gestione Escalation Pendenti / Conflitti DB (Reconciliation) ---
        if pending_escalations:
            logger.info(f"[{self.name}] Inizio Reconciliation per {len(pending_escalations)} escalation/conflitti...")
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

                    # --- PERCORSO OVERRIDE: Arbitrato Semantico MAO → JSON → force_execute_tool ---
                    if "OVERRIDE" in decision_val:
                        logger.warning(f"[{self.name}] OVERRIDE ricevuto. Avvio Arbitrato Semantico per: '{decision_reason}'")
                        override_msgs = await self._execute_semantic_override(
                            human_directive=decision_reason,
                            fallback_target=target,
                            fallback_action=action,
                        )
                        resolved_messages.extend(override_msgs)
                        # non serve continuare con la logica standard
                        continue

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

    async def _execute_semantic_override(
        self,
        human_directive: str,
        fallback_target: str,
        fallback_action: str,
    ) -> list[str]:
        """
        Arbitrato Semantico: traduce la direttiva umana in linguaggio naturale
        in un array JSON di comandi eseguibili via MAO, poi li esegue con force_execute_tool.
        """
        override_prompt = (
            f"Direttiva umana da tradurre:\n\"{human_directive}\"\n\n"
            f"Contesto: i dispositivi disponibili nel sistema includono {list(self.tools.keys())}.\n"
            f"Catalogo valori validi:\n{_tool_value_catalog(self.tools)}\n\n"
            "Checklist: target corretto, azione corretta, valore valido per il device, e mai usare il nome dell'azione come stato.\n"
            "Se la direttiva non specifica un dispositivo riconoscibile, usa il dispositivo di fallback: "
            f"\"{fallback_target}\" con azione \"{fallback_action}\"."
        )

        raw_response = ""
        try:
            raw_response = self.ask_brain(
                _OVERRIDE_SYSTEM_PROMPT.format(tool_catalog=_tool_value_catalog(self.tools)),
                override_prompt,
                temperature=0.0,
                max_tokens=1024,
            )
            logger.info(f"[{self.name}] Risposta MAO Semantic Override: {raw_response}")
        except Exception as e:
            logger.error(f"[{self.name}] MAO fallito nell'Arbitrato Semantico: {e}")

        # Pulizia difensiva: rimuove backticks e markdown
        cleaned = raw_response.strip()
        for strip_token in ["```json", "```JSON", "```"]:
            cleaned = cleaned.replace(strip_token, "")
        cleaned = cleaned.strip()

        # Parsing JSON
        commands: list[dict] = []
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                commands = parsed
            elif isinstance(parsed, dict):
                commands = [parsed]
        except (json.JSONDecodeError, ValueError) as je:
            logger.warning(f"[{self.name}] Impossibile parsare JSON dall'Override MAO: {je}. Risposta raw: {cleaned!r}")
            # Fallback: usa la semantica del target, non il nome dell'azione di sicurezza.
            commands = [{"target": fallback_target, "action": "UNBLOCK_AND_SET", "value": None}]

        messages_out = []
        for cmd in commands:
            cmd_target = str(cmd.get("target", fallback_target))
            cmd_action = str(cmd.get("action", "UNBLOCK_AND_SET")).upper()
            cmd_value = cmd.get("value")

            # Risolve il tool: usa il registro condiviso oppure ne crea uno on-demand
            tool_obj = self.tools.get(cmd_target)
            if tool_obj is None:
                tool_obj = get_tool(cmd_target, initial_value="OFF", unit="")
                self.tools[cmd_target] = tool_obj
                logger.info(f"[Brain_Override] Tool '{cmd_target}' creato on-demand.")

            final_value = _normalize_action_value(cmd_target, cmd_action, cmd_value, fallback_action)

            ok, msg = await force_execute_tool(
                target=cmd_target,
                tool_obj=tool_obj,
                action=cmd_action,
                new_value=final_value,
                reasoning=f"Brain Override: {human_directive}",
                event_log=self.event_log,
            )

            status = "✓ ESEGUITO" if ok else "✗ FALLITO"
            log_msg = f"[Brain_Override] {status} — {cmd_action} su '{cmd_target}' → '{final_value}'"
            logger.info(log_msg)
            messages_out.append(log_msg)

        return messages_out

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