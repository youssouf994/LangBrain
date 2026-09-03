"""
Costruzione e compilazione dinamica del Grafo LangGraph.
Supporta la gerarchia ricorsiva N-livelli e la gestione centralizzata degli Interrupt HITL.
"""

import logging
import time
from typing import Any
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.agent_climate import ClimateAgent
from app.graph.hitl_config import hitl_manager
from app.graph.orchestrator import BrainAgent
from app.graph.state import GraphState
from app.tools.sensor_tools import get_default_iot_tools

logger = logging.getLogger(__name__)


def wrap_node_with_hitl(node_name: str, agent_obj: Any):
    """
    Wrapper universale che consente di attivare un interrupt HITL (Human-in-the-Loop)
    ovunque nel flusso prima o dopo l'esecuzione del nodo, in base alle regole dinamiche.
    """
    async def hitl_wrapped_agent_node(state: GraphState) -> dict[str, Any]:
        # 1. Intercettazione PRE-ESECUZIONE del nodo
        if hitl_manager.should_interrupt(node_name, state):
            logger.warning(f"[HITL Interceptor] Invocazione interrupt() prima di eseguire il nodo '{node_name}'.")
            cfg = hitl_manager.get_config()
            human_payload = interrupt({
                "type": "hitl_node_entry_interrupt",
                "node_name": node_name,
                "timestamp": time.time(),
                "max_wait_seconds": cfg.max_wait_seconds,
                "prompt": (
                    f"Approvazione Umana Richiesta: esecuzione nodo '{node_name}'. "
                    f"Attesa max: {cfg.max_wait_seconds or 'illimitata'}s."
                ),
            })

            # Analisi della decisione inviata via API POST /graph/resume
            decision_val = "APPROVA"
            reasoning = "Approvato via HITL API"
            if isinstance(human_payload, dict):
                decision_val = str(human_payload.get("decision", "APPROVA")).upper()
                reasoning = human_payload.get("reasoning", reasoning)
            elif human_payload:
                decision_val = str(human_payload).upper()

            if "RESPINGI" in decision_val or "REJECT" in decision_val or "NO" in decision_val:
                logger.info(f"[HITL Interceptor] Nodo '{node_name}' bloccato da rifiuto umano ({reasoning}).")
                try:
                    from app.tools.event_log import EventLog
                    log = EventLog()
                    events = await log.get_recent_events()
                    for e in events:
                        act = str(e.get("action", ""))
                        if not act.startswith("RESOLVED_") and not act.startswith("RECONCILED_"):
                            t = e.get("target")
                            await log.log_event(
                                actor="Brain",
                                action=f"REJECTED_{act}",
                                target=t,
                                old_value=str(e.get("new_value", "UNKNOWN")),
                                new_value="REJECTED",
                                reasoning=f"Rifiuto umano via HITL: {reasoning}",
                                escalated=False
                            )
                            await log.mark_resolved(t)
                except Exception as ex:
                    logger.error(f"[HITL Interceptor] Errore salvataggio REJECTED su DB: {ex}")

                return {
                    "messages": [AIMessage(content=f"[HITL Interceptor] Nodo '{node_name}' annullato da approvazione umana ({reasoning}).")],
                    "next_agent": "END",
                }

            elif "OVERRIDE" in decision_val:
                # ── GOD MODE SEMANTICO ─────────────────────────────────────────────────
                # L'operatore umano ha inviato una direttiva in linguaggio naturale
                # (campo 'reasoning') che bypassa i lock di priorità.
                # Il Brain traduce la frase in comandi JSON ed esegue force_execute_tool.
                logger.warning(
                    f"[HITL Interceptor] OVERRIDE ricevuto per nodo '{node_name}'. "
                    f"Avvio Arbitrato Semantico MAO. Direttiva: '{reasoning}'"
                )
                # Recupera i target dei conflitti pendenti (fallback per _execute_semantic_override)
                fallback_target = "unknown"
                fallback_action = "UNBLOCK_AND_SET"
                try:
                    from app.tools.event_log import EventLog
                    log = EventLog()
                    events = await log.get_recent_events()
                    # Individua il primo target non ancora riconciliato
                    for e in events:
                        act = str(e.get("action", ""))
                        if not act.startswith("RESOLVED_") and not act.startswith("RECONCILED_") and not act.startswith("UNBLOCKED"):
                            fallback_target = e.get("target", fallback_target)
                            fallback_action = e.get("action", fallback_action)
                            break
                except Exception as ex:
                    logger.warning(f"[HITL Interceptor] Impossibile leggere eventi dal DB per override: {ex}")

                # Delega _execute_semantic_override al BrainAgent (ha accesso ai tools)
                override_msgs: list[str] = []
                if hasattr(agent_obj, "_execute_semantic_override"):
                    try:
                        override_msgs = await agent_obj._execute_semantic_override(
                            human_directive=reasoning,
                            fallback_target=fallback_target,
                            fallback_action=fallback_action,
                        )
                    except Exception as ov_ex:
                        logger.error(f"[HITL Interceptor] Errore durante Semantic Override: {ov_ex}")
                        override_msgs = [f"[OVERRIDE ERROR] {ov_ex}"]
                else:
                    override_msgs = [f"[HITL Interceptor] OVERRIDE ignorato: l'agente '{node_name}' non espone _execute_semantic_override."]

                combined_msg = "\n".join(override_msgs) if override_msgs else f"[Brain_Override] Override eseguito su '{fallback_target}'."
                return {
                    "messages": [AIMessage(content=combined_msg)],
                    "pending_escalations": [],
                    "next_agent": "END",
                }

            else:
                # Approvazione normale (APPROVA / YES)
                if node_name == "brain":
                    try:
                        from app.tools.event_log import EventLog
                        log = EventLog()
                        events = await log.get_recent_events()
                        for e in events:
                            act = str(e.get("action", ""))
                            if not act.startswith("RESOLVED_") and not act.startswith("RECONCILED_"):
                                t = e.get("target")
                                await log.log_event(
                                    actor="Brain",
                                    action=f"RECONCILED_{act}",
                                    target=t,
                                    old_value=str(e.get("new_value", "UNKNOWN")),
                                    new_value="RECONCILED_OK",
                                    reasoning=f"Approvazione umana via HITL: {reasoning}",
                                    escalated=False
                                )
                                await log.mark_resolved(t)
                    except Exception as ex:
                        logger.error(f"[HITL Interceptor] Errore salvataggio RECONCILED su DB: {ex}")

                    # Termina il ciclo evitando la prosecuzione non autorizzata
                    return {
                        "messages": [AIMessage(content=f"[HITL Interceptor] Nodo '{node_name}' approvato da decisione umana ({reasoning}).")],
                        "next_agent": "END",
                    }

        # 2. Esecuzione dell'agente reale
        if callable(getattr(agent_obj, "process", None)):
            recent_events = state.get("recent_events", [])
            relevant_readings = state.get("readings", [])
            agent_escalations = state.get("pending_escalations", [])
            res = await agent_obj.process(state, recent_events, relevant_readings, agent_escalations)
        elif callable(agent_obj):
            res = await agent_obj(state)
        else:
            res = {}

        # 3. Intercettazione POST-ESECUZIONE per escalation o azioni prodotte
        pending_esc = res.get("pending_escalations", [])
        for esc in pending_esc:
            t = esc.get("target_device")
            a = esc.get("proposed_action")
            if hitl_manager.should_interrupt(node_name, state, proposed_target=t, proposed_action=a):
                logger.warning(
                    f"[HITL Interceptor] Invocazione interrupt() post-analisi del nodo '{node_name}' per target '{t}' ({a})."
                )
                cfg = hitl_manager.get_config()
                human_payload = interrupt({
                    "type": "hitl_action_proposal_interrupt",
                    "node_name": node_name,
                    "target_device": t,
                    "proposed_action": a,
                    "timestamp": time.time(),
                    "max_wait_seconds": cfg.max_wait_seconds,
                    "prompt": (
                        f"Approvazione Umana Richiesta per target '{t}' (Azione: {a}). "
                        f"Attesa max: {cfg.max_wait_seconds or 'illimitata'}s."
                    ),
                })

                decision_val = "APPROVA"
                if isinstance(human_payload, dict):
                    decision_val = str(human_payload.get("decision", "APPROVA")).upper()
                elif human_payload:
                    decision_val = str(human_payload).upper()

                if "RESPINGI" in decision_val or "REJECT" in decision_val or "NO" in decision_val:
                    res["pending_escalations"] = []
                    res["messages"] = [AIMessage(content=f"[HITL Interceptor] Azione proposta su '{t}' respinta da approvazione umana.")]

        return res

    return hitl_wrapped_agent_node


def build_graph(custom_agent_instances: dict[str, Any] | None = None):
    """
    Costruisce e compila il grafo con topologia ad albero gerarchico (Padre <-> Figlio)
    e wrapper per la gestione dinamica degli Interrupt HITL.
    """
    shared_tools = get_default_iot_tools()
    brain_agent = BrainAgent(tools=list(shared_tools.values()))

    workflow = StateGraph(GraphState)

    # 1. Registra l'Orchestratore Padre (Brain) avvolto con HITL Interceptor
    workflow.add_node("brain", wrap_node_with_hitl("brain", brain_agent))
    registered_nodes = {"brain"}

    # 2. Registra tutti i sotto-agenti avvolti con HITL Interceptor
    if custom_agent_instances:
        for node_name, agent_obj in custom_agent_instances.items():
            workflow.add_node(node_name, wrap_node_with_hitl(node_name, agent_obj))
            registered_nodes.add(node_name)
    else:
        climate_agent = ClimateAgent(tools=shared_tools)
        workflow.add_node("agent_climate", wrap_node_with_hitl("agent_climate", climate_agent))
        registered_nodes.add("agent_climate")

    # 3. Flusso iniziale: START -> Brain
    workflow.add_edge(START, "brain")

    # 4. Router universale
    def generic_router(state: GraphState) -> str:
        next_node = state.get("next_agent", "END")
        if next_node in registered_nodes:
            return next_node
        return END

    routing_map = {node_id: node_id for node_id in registered_nodes}
    routing_map[END] = END

    for node_id in registered_nodes:
        workflow.add_conditional_edges(node_id, generic_router, routing_map)

    # 5. Checkpointer per la persistenza di stato
    checkpointer = MemorySaver()
    compiled_graph = workflow.compile(checkpointer=checkpointer)

    logger.info(f"Grafo LangGraph Gerarchico assemblato con {len(registered_nodes)} nodi agenti e wrapper HITL dinamici.")
    return compiled_graph, shared_tools
