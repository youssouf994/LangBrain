"""
Costruzione e compilazione dinamica del Grafo LangGraph.
Supporta la gerarchia ricorsiva N-livelli:
  Cervello (Brain - Livello 0) <-> Organi (Livello 1) <-> Componenti dell'Organo (Livello 2) <-> Sotto-Componenti
"""

import logging
from typing import Any
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.agent_climate import ClimateAgent
from app.graph.orchestrator import BrainAgent
from app.graph.state import GraphState
from app.tools.sensor_tools import get_default_iot_tools

logger = logging.getLogger(__name__)


def build_graph(custom_agent_instances: dict[str, Any] | None = None):
    """
    Costruisce e compila il grafo con topologia ad albero gerarchico (Padre <-> Figlio).
    Ogni nodo dell'agente può instradare verso il proprio Padre, verso un Figlio o terminare (END).
    """
    shared_tools = get_default_iot_tools()
    brain_agent = BrainAgent(tools=list(shared_tools.values()))

    workflow = StateGraph(GraphState)

    # 1. Registra l'Orchestratore Padre (Brain)
    workflow.add_node("brain", brain_agent)

    registered_nodes = {"brain"}

    # 2. Registra tutti i sotto-agenti (dinamici e nativi)
    if custom_agent_instances:
        for node_name, agent_obj in custom_agent_instances.items():
            workflow.add_node(node_name, agent_obj)
            registered_nodes.add(node_name)
    else:
        # Default di fallback con solo agent_climate
        climate_agent = ClimateAgent(tools=shared_tools)
        workflow.add_node("agent_climate", climate_agent)
        registered_nodes.add("agent_climate")

    # 3. Flusso iniziale: START -> Brain
    workflow.add_edge(START, "brain")

    # 4. Router universale: legge next_agent dallo stato e instrada verso qualsiasi nodo valido o END
    def generic_router(state: GraphState) -> str:
        next_node = state.get("next_agent", "END")
        if next_node in registered_nodes:
            return next_node
        return END

    routing_map = {node_id: node_id for node_id in registered_nodes}
    routing_map[END] = END

    # Applica il router universale a tutti i nodi della gerarchia
    for node_id in registered_nodes:
        workflow.add_conditional_edges(node_id, generic_router, routing_map)

    # 5. Checkpointer per la persistenza di stato
    checkpointer = MemorySaver()
    compiled_graph = workflow.compile(checkpointer=checkpointer)

    logger.info(f"Grafo LangGraph Gerarchico assemblato con {len(registered_nodes)} nodi agenti e routing universale.")
    return compiled_graph, shared_tools
