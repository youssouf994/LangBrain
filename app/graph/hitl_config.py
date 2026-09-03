"""
Gestione Centrale e Dinamica della Configurazione Human-in-the-Loop (HITL).
Permette di inserire punti di interrupt ovunque nel flusso del grafo LangGraph (su nodi, target sensori o azioni specifiche)
e definire il tempo massimo di attesa prima del fallback.
"""

import logging
from typing import Any
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class HitlConfigSchema(BaseModel):
    """Schema di configurazione dei punti di interrupt HITL e dei tempi di attesa."""
    hitl_all: bool = False
    """Se True, applica l'interrupt HITL prima dell'esecuzione di qualsiasi nodo nel grafo."""
    hitl_nodes: list[str] = Field(default_factory=list)
    """Lista dei nodi agenti su cui attivare l'interrupt HITL (es. ['brain', 'organ_security'])."""
    hitl_targets: list[str] = Field(default_factory=list)
    """Lista dei target sensore su cui attivare l'interrupt HITL (es. ['front_door_lock', 'cardiac_pacemaker'])."""
    hitl_actions: list[str] = Field(default_factory=list)
    """Lista delle azioni specifiche che richiedono approvazione umana (es. ['FORCE_SHUTDOWN', 'UNLOCK'])."""
    max_wait_seconds: int | None = Field(default=None)
    """Attesa massima in secondi prima dell'eventuale fallback automatico."""


class HitlConfigManager:
    """Manager singleton per la gestione dinamica delle policy HITL a runtime."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.config = HitlConfigSchema()
        return cls._instance

    def get_config(self) -> HitlConfigSchema:
        return self.config

    def update_config(
        self,
        hitl_all: bool | None = None,
        hitl_nodes: list[str] | None = None,
        hitl_targets: list[str] | None = None,
        hitl_actions: list[str] | None = None,
        max_wait_seconds: int | None = None,
    ) -> HitlConfigSchema:
        if hitl_all is not None:
            self.config.hitl_all = hitl_all
        if hitl_nodes is not None:
            self.config.hitl_nodes = hitl_nodes
        if hitl_targets is not None:
            self.config.hitl_targets = hitl_targets
        if hitl_actions is not None:
            self.config.hitl_actions = hitl_actions
        if max_wait_seconds is not None:
            self.config.max_wait_seconds = max_wait_seconds

        logger.info(
            "[HitlConfigManager] Configurazione HITL aggiornata: all=%s, nodes=%s, targets=%s, actions=%s, max_wait=%ss",
            self.config.hitl_all,
            self.config.hitl_nodes,
            self.config.hitl_targets,
            self.config.hitl_actions,
            self.config.max_wait_seconds,
        )
        return self.config

    def should_interrupt(
        self,
        node_name: str,
        state: dict[str, Any],
        proposed_target: str | None = None,
        proposed_action: str | None = None,
    ) -> bool:
        """Determina se un'esecuzione richiede l'invocazione di un interrupt HITL."""
        cfg = self.config

        # 1. Se hitl_all è attivo a livello globale
        if cfg.hitl_all:
            return True

        # 2. Se il nome del nodo rientra tra i nodi monitorati
        if node_name in cfg.hitl_nodes:
            return True

        # 3. Se il target rientra tra i dispositivi protetti
        if proposed_target and proposed_target in cfg.hitl_targets:
            return True

        # 4. Se l'azione rientra tra le azioni critiche
        if proposed_action and proposed_action in cfg.hitl_actions:
            return True

        # 5. Verifica nelle escalation pendenti nello stato del grafo
        pending_escalations = state.get("pending_escalations", [])
        for esc in pending_escalations:
            source = esc.get("source_agent")
            target = esc.get("target_device") or esc.get("target")
            action = esc.get("proposed_action") or esc.get("action")

            if source and source in cfg.hitl_nodes:
                return True
            if target and target in cfg.hitl_targets:
                return True
            if action and action in cfg.hitl_actions:
                return True

        # 6. Fallback allo stato dinamico del contesto del grafo (per retrocompatibilità)
        state_config = state.get("config", {})
        if state_config.get("hitl_all", False):
            return True
        if node_name in state_config.get("hitl_nodes", []):
            return True
        if proposed_target and proposed_target in state_config.get("hitl_targets", []):
            return True

        return False


# Singleton esportato
hitl_manager = HitlConfigManager()
