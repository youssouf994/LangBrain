from abc import ABC, abstractmethod
import logging
from typing import Any

from app.graph.state import GraphState, EscalationItem
from app.tools.event_log import EventLog
from app.MAO.model_access_object import Mao

logger = logging.getLogger(__name__)

class BaseAgent(ABC):
    """
    Classe base astratta per tutti gli agenti di dominio.
    Gestisce readout dei tool, tracciamento old_value -> new_value su DB e prevenzione comandi ridondanti.
    """

    def __init__(self, name: str, managed_targets: list[str], conflict_window_minutes: int = 30, priority_weight: float = 0.000):
        self.name = name
        self.managed_targets = managed_targets
        self.conflict_window_minutes = conflict_window_minutes
        self.priority_weight = priority_weight
        self.mao = Mao()
        self.event_log = EventLog(target=managed_targets, frequency=conflict_window_minutes)

    async def __call__(self, state: GraphState) -> dict[str, Any]:
        """
        Metodo invocato da LangGraph quando il nodo dell'agente viene eseguito.
        """
        logger.info(f"[{self.name}] Invocazione agente (Priorità: {self.priority_weight})")

        # 1. Recupera le escalation aperte relative ai target gestiti
        pending_escalations = state.get("pending_escalations", [])
        agent_escalations = [
            esc for esc in pending_escalations 
            if esc.get("target_device") in self.managed_targets
        ]

        # 2. Recupera gli eventi recenti dal DB per i target gestiti (ultimi N minuti)
        try:
            recent_events = await self.event_log.get_recent_events()
        except Exception as e:
            logger.error(f"[{self.name}] Errore nel recupero degli eventi recenti: {e}")
            recent_events = []

        # 3. Recupera le ultime letture dei sensori per i target gestiti
        all_readings = state.get("readings", [])
        relevant_readings = [
            r for r in all_readings 
            if r.get("sensor_id") in self.managed_targets or r.get("agent_owner") == self.name
        ]

        # 4. Esegue la logica specifica del sotto-agente (process)
        updates = await self.process(
            state=state, 
            recent_events=recent_events, 
            relevant_readings=relevant_readings,
            agent_escalations=agent_escalations
        )
        return updates

    @abstractmethod
    async def process(
        self, 
        state: GraphState, 
        recent_events: list[dict], 
        relevant_readings: list[dict], 
        agent_escalations: list[dict]
    ) -> dict[str, Any]:
        """
        Metodo astratto da implementare in ciascun agente concreto.
        """
        pass

    async def apply_status(
        self, 
        target: str, 
        action: str, 
        new_value: str, 
        reasoning: str, 
        escalated: bool = False,
        tools_map: dict[str, Any] | None = None
    ) -> bool:
        """
        Metodo unificato per azionare i tool reali e registrare la transizione old_value -> new_value su DB.
        Include prevenzione delle esecuzioni ridondanti (cooldown/stabilizzazione).
        Ritorna True se l'azione è stata effettivamente applicata, False se saltata perché ridondante.
        """
        old_value = "UNKNOWN"
        tool_obj = tools_map.get(target) if tools_map else None

        # 1. Legge il valore attuale reale dal tool
        if tool_obj:
            try:
                old_value = str(await tool_obj.get_tool_value())
            except Exception as e:
                logger.error(f"[{self.name}] Errore lettura stato dal tool {target}: {e}")

        # 2. Controllo Ridondanza: se il tool è GIÀ al valore desiderato, non rieseguire!
        if old_value.upper() == str(new_value).upper() and not escalated:
            logger.info(f"[{self.name}] Saltata azione su '{target}': valore attuale già a '{old_value}'. In fase di stabilizzazione.")
            return False

        # 3. Azionamento del Tool
        if tool_obj:
            try:
                await tool_obj.set_tool_value(new_value)
                logger.info(f"[{self.name}] Tool '{target}' azionato: {old_value} -> {new_value}")
            except Exception as e:
                logger.error(f"[{self.name}] Errore durante l'azionamento del tool '{target}': {e}")

        # 4. Registrazione dell'evento con old_value e new_value nel DB SQLite
        try:
            await self.event_log.log_event(
                actor=self.name,
                action=action,
                target=target,
                old_value=old_value,
                new_value=str(new_value),
                reasoning=reasoning,
                escalated=escalated
            )
        except Exception as e:
            logger.error(f"[{self.name}] Errore durante il salvataggio su DB: {e}")

        return True

    def check_for_recent_conflict(self, target: str, recent_events: list[dict], ignore_actor: str | None = None) -> tuple[bool, dict | None]:
        """
        Verifica se ci sono stati eventi recenti su 'target' da parte del Cervello o di agenti a priorità superiore.
        Esclude eventi la cui azione è già stata risolta/scaduta o che abbiano superato il TTL.
        """
        from app.core.constants import is_flag_expired

        for event in recent_events:
            if event.get("target") == target:
                action = str(event.get("action", ""))
                # Se l'azione è già marcata come EXPIRED_ o RESOLVED_, ignorala
                if action.startswith("EXPIRED_") or action.startswith("RESOLVED_"):
                    continue

                # Controlla il TTL del timestamp dell'evento
                ts = str(event.get("timestamp", ""))
                if is_flag_expired(ts, ttl_minutes=self.conflict_window_minutes):
                    continue

                actor = event.get("actor")
                if actor and actor != (ignore_actor or self.name):
                    return True, event
        return False, None

    def create_escalation(self, target_device: str, proposed_action: str, reason: str, conflict_detected: bool = False, context_events: list[dict] | None = None):
        """
        Helper per costruire un oggetto EscalationItem pronto da inserire nello stato.
        """
        item = EscalationItem(
            source_agent=self.name,
            target_device=target_device,
            proposed_action=proposed_action,
            reason=reason,
            conflict_detected=conflict_detected,
            context_events=context_events or []
        )
        return item.model_dump()

    def ask_brain(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        """
        Invocazione centralizzata del modello tramite MAO.
        """
        try:
            return self.mao.call_model(
                system_prompt, user_prompt, temperature, max_tokens, provider=provider, model=model
            )
        except Exception as e:
            logger.error(f"[{self.name}] Errore nell'invocazione del modello: {e}")
            return f"Errore nell'invocazione del modello: {str(e)}"