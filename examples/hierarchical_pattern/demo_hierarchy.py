"""
Dimostrazione Pratica: Gerarchia Agenti N-Livelli
Cervello (Brain - Liv. 0) -> Organi (Liv. 1) -> Componenti dell'Organo (Liv. 2)

Struttura di questo test:
  1. Organo 1: 'organ_climate' (Livello 1) -> Gestisce 2 TOOL diretti ('ac_living_room', 'heater_bedroom')
  2. Organo 2: 'organ_security' (Livello 1) -> Gestisce 2 SOTTO-AGENTI ('component_door_lock', 'component_alarm')
      ├── Componente 1: 'component_door_lock' (Livello 2) -> Gestisce TOOL 'front_door_lock'
      └── Componente 2: 'component_alarm' (Livello 2) -> Gestisce TOOL 'alarm_system'
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.agents.agent_registry import AgentRegistry
from app.db.database import Database
from app.graph.builder import build_graph
from app.tools.event_log import EventLog

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("demo_hierarchy")


async def main():
    logger.info("=== AVVIO DEMO GERARCHIA: Cervello -> Organi -> Componenti ===")

    # 1. Inizializza DB e Registry
    db = Database()
    await db.init_db()

    registry = AgentRegistry()
    await registry.init_registry_db()

    # 2. Configura Organo 1 (gestisce 2 tool diretti)
    await registry.register_agent_config({
        "name": "organ_climate",
        "level": 1,
        "parent_agent_name": "Brain",
        "managed_targets": ["ac_living_room", "heater_bedroom"],
        "sub_agent_names": [],
        "system_prompt_template": "Sei l'Organo del Clima (Livello 1). Gestisci il condizionatore ed il riscaldamento.",
        "priority_weight": 1.0,
    })

    # 3. Configura Organo 2 (gestisce 2 sotto-agenti/componenti)
    await registry.register_agent_config({
        "name": "organ_security",
        "level": 1,
        "parent_agent_name": "Brain",
        "managed_targets": ["front_door_lock", "alarm_system"],
        "sub_agent_names": ["component_door_lock", "component_alarm"],
        "system_prompt_template": "Sei l'Organo di Sicurezza (Livello 1). Coordini i componenti serratura ed allarme.",
        "priority_weight": 500.0,
    })

    # 4. Configura Componente 1 dell'Organo Sicurezza (Livello 2)
    await registry.register_agent_config({
        "name": "component_door_lock",
        "level": 2,
        "parent_agent_name": "organ_security",
        "managed_targets": ["front_door_lock"],
        "sub_agent_names": [],
        "system_prompt_template": "Sei il Componente Serratura (Livello 2). Gestisci la serratura principale.",
        "priority_weight": 100.0,
    })

    # 5. Configura Componente 2 dell'Organo Sicurezza (Livello 2)
    await registry.register_agent_config({
        "name": "component_alarm",
        "level": 2,
        "parent_agent_name": "organ_security",
        "managed_targets": ["alarm_system"],
        "sub_agent_names": [],
        "system_prompt_template": "Sei il Componente Allarme (Livello 2). Gestisci l'allarme antintrusione.",
        "priority_weight": 200.0,
    })

    # 6. Stampa l'albero gerarchico visualizzabile
    tree = await registry.get_hierarchy_tree()
    logger.info("Albero Gerarchico Generato:\n%s", json.dumps(tree, indent=2, ensure_ascii=False))

    # 7. Compila il grafo dinamico con tutti gli agenti registrati
    instances = await registry.build_agent_instances()
    graph, shared_tools = build_graph(custom_agent_instances=instances)
    thread_config = {"configurable": {"thread_id": "demo_hierarchy_session"}}

    # 8. Esegue un ciclo di test normale
    logger.info("\n--- [TEST CICLO 1] Esecuzione Normale del Grafo Gerarchico ---")
    initial_state = {
        "messages": [],
        "readings": [],
        "recent_events": [],
        "pending_escalations": [],
        "next_agent": "brain",
        "hitl_required": False,
        "config": {"readings_window_hours": 4},
    }

    result = await graph.ainvoke(initial_state, config=thread_config)
    messages = result.get("messages", [])
    if messages:
        logger.info("Esito Ciclo 1: %s", messages[-1].content)

    # 9. Simula un conflitto sull'Organo 2 / Componente Serratura per testare l'escalation ricorsiva
    logger.info("\n--- [TEST CICLO 2] Simulazione Conflitto sul Componente Serratura ---")
    event_log = EventLog()
    await event_log.log_event(
        actor="user_manual",
        action="MANUAL_UNLOCK_OVERRIDE",
        target="front_door_lock",
        old_value="LOCKED",
        new_value="UNLOCKED",
        reasoning="Sblocco manuale d'emergenza",
        escalated=False,
    )

    state_conflict = {
        "messages": [],
        "readings": [],
        "recent_events": await event_log.get_recent_events(),
        "pending_escalations": [],
        "next_agent": "component_door_lock",  # Parte dal Componente
        "hitl_required": False,
        "config": {"readings_window_hours": 4},
    }

    result2 = await graph.ainvoke(state_conflict, config=thread_config)
    messages2 = result2.get("messages", [])
    if messages2:
        logger.info("Esito Ciclo 2 (Escalation Componente -> Organo -> Brain): %s", messages2[-1].content)

    logger.info("=== DEMO GERARCHIA COMPLETATA CON SUCCESSO ===")


if __name__ == "__main__":
    asyncio.run(main())
