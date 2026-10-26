"""
Wrapper Decoratore e Helper per l'Esecuzione Sicura dei Tool IoT.
Fornisce logging strutturato e convalida delle policy di priorità prima dell'azionamento hardware (nessun retry:
un azionamento fallito è riportato a chi chiama, che decide se ripetere).
"""

import logging
from typing import Any
from app.core.configurazione import get_configurazione
from app.core.priorita import NOME_BRAIN, trova_blocco_prevalente
from app.tools.nervous_system import NervousSystem

logger = logging.getLogger(__name__)


async def execute_tool_safely(
    actor_name: str,
    actor_priority: float,
    target: str,
    tool_obj: Any,
    new_value: Any,
    reasoning: str = "",
    event_log: NervousSystem | None = None,
    ttl_minuti: int = 30,
) -> tuple[bool, str]:
    """
    Esegue l'azionamento di un tool verificando prima che non ci siano blocchi attivi imposti da attori con
    priorità strettamente maggiore di `actor_priority` (es. agent_security 500.0 vs agent_climate 1.0).
    """
    log = event_log or NervousSystem(target=[target])

    # 0. Il comando deve essere ammesso dall'elenco dei dispositivi di configurazione.toml
    validazione = get_configurazione().valida_comando(target, new_value)
    if not validazione.ammesso:
        logger.warning(f"[{actor_name}] Comando RIFIUTATO su '{target}': {validazione.motivo}")
        return False, validazione.motivo
    new_value = validazione.valore

    # 1. Il Brain ha priorità massima; per tutti gli altri si cercano blocchi prevalenti nel DB
    if actor_name != NOME_BRAIN:
        try:
            events = await log.get_recent_events()
            motivo = trova_blocco_prevalente(events, target, actor_name, actor_priority, ttl_minuti)
            if motivo:
                msg = f"Azione RESPINTA su '{target}': {motivo}."
                logger.warning(f"[{actor_name}] {msg}")
                return False, msg
        except Exception as e:
            logger.error(f"[{actor_name}] Errore durante la verifica dei blocchi di priorità: {e}")

    # 2. Azionamento del tool reale
    try:
        if tool_obj:
            await tool_obj.set_tool_value(new_value)
            logger.info(f"[{actor_name}] Tool '{target}' aggiornato a '{new_value}'" + (f" ({reasoning})" if reasoning else ""))
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
    event_log: NervousSystem | None = None,
) -> tuple[bool, str]:
    """
    Esecuzione FORZATA di un tool da parte del Brain_Override.
    Bypassa deliberatamente check_priority_lock: annulla i blocchi attivi sul target
    e sovrascrive l'attuatore fisico con il valore specificato.
    Usato esclusivamente per le decisioni di OVERRIDE umane validate dal Brain.
    """
    log = event_log or NervousSystem(target=[target])

    # 0. Anche un OVERRIDE deve rispettare l'elenco dei dispositivi e dei valori ammessi: bypassa le priorità, non i limiti fisici
    validazione = get_configurazione().valida_comando(target, new_value)
    if not validazione.ammesso:
        logger.warning(f"[Brain_Override] Comando RIFIUTATO su '{target}': {validazione.motivo}")
        return False, validazione.motivo
    new_value = validazione.valore

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

