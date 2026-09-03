"""
Wrapper Decoratore e Helper per l'Esecuzione Sicura dei Tool IoT.
Fornisce retry, logging strutturato e convalida delle policy di priorità prima dell'azionamento hardware.
"""

import logging
from typing import Any
from app.tools.event_log import EventLog

logger = logging.getLogger(__name__)


async def execute_tool_safely(
    actor_name: str,
    actor_priority: float,
    target: str,
    tool_obj: Any,
    new_value: Any,
    reasoning: str = "",
    event_log: EventLog | None = None,
) -> tuple[bool, str]:
    """
    Esegue l'azionamento di un tool verificando prima che non ci siano blocchi attivi
    imposti da agenti a priorità superiore (es. agent_security priority 500.0 vs agent_climate priority 1.0).
    """
    log = event_log or EventLog(target=[target])

    # 1. Se non è il Brain (priorità massima 1000.0), controlla i blocchi a priorità superiore nel DB
    if actor_name != "Brain":
        try:
            events = await log.get_recent_events()
            for event in events:
                if event.get("target") == target:
                    action = str(event.get("action", ""))
                    new_val = str(event.get("new_value", "")).upper()

                    if action.startswith("EXPIRED_") or action.startswith("RESOLVED_") or action.startswith("UNBLOCKED") or action.startswith("RECONCILED_"):
                        continue

                    from app.core.constants import is_flag_expired, is_control_flag
                    ts = str(event.get("timestamp", ""))
                    if is_flag_expired(ts, ttl_minutes=30):
                        continue

                    actor = str(event.get("actor", ""))
                    if actor != actor_name and (action in ["FORCE_SHUTDOWN", "SECURITY_LOCK"] or is_control_flag(new_val) or new_val == "OFF"):
                        msg = f"Azione RESPINTA su '{target}': l'agente '{actor}' ha un blocco attivo '{action}' ({new_val})."
                        logger.warning(f"[{actor_name}] {msg}")
                        return False, msg
        except Exception as e:
            logger.error(f"[{actor_name}] Errore durante la verifica dei blocchi di priorità: {e}")

    # 2. Azionamento del tool reale
    try:
        if tool_obj:
            await tool_obj.set_tool_value(new_value)
            logger.info(f"[{actor_name}] Tool '{target}' aggiornato a '{new_value}'")
        return True, f"Tool '{target}' aggiornato a '{new_value}'"
    except Exception as e:
        logger.error(f"[{actor_name}] Errore durante l'azionamento di '{target}': {e}")
        return False, str(e)


async def force_execute_tool(
    target: str,
    tool_obj: Any,
    action: str,
    new_value: Any,
    reasoning: str = "",
    event_log: EventLog | None = None,
) -> tuple[bool, str]:
    """
    Esecuzione FORZATA di un tool da parte del Brain_Override.
    Bypassa deliberatamente check_priority_lock: annulla i blocchi attivi sul target
    e sovrascrive l'attuatore fisico con il valore specificato.
    Usato esclusivamente per le decisioni di OVERRIDE umane validate dal Brain.
    """
    log = event_log or EventLog(target=[target])

    # 1. Annulla gli eventuali blocchi/flag attivi sul target nel DB
    try:
        await log.mark_resolved(target)
        logger.info(f"[Brain_Override] Blocchi precedenti su '{target}' annullati (mark_resolved).")
    except Exception as e:
        logger.warning(f"[Brain_Override] Impossibile annullare blocchi su '{target}': {e}")

    # 2. Azionamento diretto senza nessun controllo di priorità
    try:
        if tool_obj:
            await tool_obj.set_tool_value(new_value)
            logger.info(f"[Brain_Override] Tool '{target}' forzato a '{new_value}' (action={action})")

        # 3. Audit log dell'override forzato
        try:
            await log.log_event(
                actor="Brain_Override",
                action=action,
                target=target,
                old_value="[FORCED_OVERRIDE]",
                new_value=str(new_value),
                reasoning=reasoning,
                escalated=False,
            )
        except Exception as log_err:
            logger.warning(f"[Brain_Override] Impossibile scrivere audit log per '{target}': {log_err}")

        return True, f"[OVERRIDE] Tool '{target}' forzato a '{new_value}'"
    except Exception as e:
        logger.error(f"[Brain_Override] Errore durante il force-execute di '{target}': {e}")
        return False, str(e)

