"""
API FastAPI — LangBrain
Espone endpoint per:
  - Invocare il grafo agenti (singolo ciclo)
  - Leggere/scrivere tool IoT
  - Ispezionare eventi e letture dal DB
  - Creare / Gestire N sotto-agenti dinamicamente a qualsiasi livello della gerarchia
  - Visualizzare l'albero gerarchico (Cervello -> Organi -> Componenti dell'Organo)
  - Invocare direttamente MAO (LLM proxy)
  - Human-in-the-Loop (HITL resume / state)
"""

from contextlib import asynccontextmanager
import json
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.agents.agent_registry import AgentRegistry
from app.db.database import Database
from app.graph.builder import build_graph
from app.graph.hitl_config import HitlConfigSchema
from app.tools.event_log import EventLog

# ---------------------------------------------------------------------------
# Lifespan & Global State
# ---------------------------------------------------------------------------

db = Database()
registry = AgentRegistry()
_graph = None
_shared_tools: dict = {}


async def _recompile_system_graph():
    """Ricarica le istanze dal registro e ricompila il grafo LangGraph."""
    global _graph, _shared_tools
    agent_instances = await registry.build_agent_instances(_shared_tools)
    _graph, _shared_tools = build_graph(custom_agent_instances=agent_instances)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    await registry.init_registry_db()
    await _recompile_system_graph()
    yield


app = FastAPI(
    title="LangBrain API",
    description="API FastAPI con LangGraph per la gestione gerarchica N-livelli (Cervello -> Organi -> Componenti)",
    version="2.1.0",
    lifespan=lifespan,
)

_THREAD_ID = "api_session"
_THREAD_CONFIG = {"configurable": {"thread_id": _THREAD_ID}}

# ---------------------------------------------------------------------------
# Schemi Pydantic
# ---------------------------------------------------------------------------


class RunCycleRequest(BaseModel):
    """Payload per invocare un ciclo del grafo agenti."""
    sensor_readings: list[dict[str, Any]] = []
    force_next_agent: str = "brain"
    thread_id: str | None = None
    """Identificatore di thread per la persistenza del grafo ed il resume HITL."""


class ToolWriteRequest(BaseModel):
    """Payload per impostare il valore di un tool IoT."""
    target: str
    value: Any


class SeedConflictRequest(BaseModel):
    """Semina un evento di conflitto nel DB per simulare uno scenario di escalation."""
    actor: str = "agent_security"
    action: str = "FORCE_SHUTDOWN"
    target: str = "ac_living_room"
    old_value: str = "22.5°C"
    new_value: str = "OFF"
    reasoning: str = "Simulazione conflitto via API"


class LlmProxyRequest(BaseModel):
    """Invoca direttamente il MAO (LLM proxy) senza passare per il grafo."""
    system_prompt: str
    user_prompt: str
    provider: str | None = None
    model: str | None = None
    temperature: float = 0.0
    max_tokens: int = 2048
    enable_reasoning: bool = False
    fallback_on_error: bool = False


class CreateSubAgentSchema(BaseModel):
    """Schema strutturato per la creazione o aggiornamento di un sotto-agente a qualsiasi livello."""
    name: str
    """Identificatore dell'agente. Es: 'organ_security', 'climate_living_room'."""
    level: int = 1
    """Livello gerarchico: 1 = Organo, 2 = Componente dell'Organo, N = Sotto-Componente."""
    parent_agent_name: str | None = "Brain"
    """Nome dell'agente Padre verso cui fare escalation. Default: 'Brain'."""
    managed_targets: list[str]
    """Elenco di target/dispositivi controllati da questo agente."""
    sub_agent_names: list[str] = []
    """Elenco facoltativo di sotto-agenti coordinati da questo agente."""
    system_prompt_template: str | None = None
    """Template di istruzioni specifiche per il modello LLM dell'agente."""
    user_prompt_template: str | None = None
    """Template facoltativo per la formattazione dei dati sensori dell'agente."""
    conflict_window_minutes: int = 30
    priority_weight: float = 1.0


class CreateSubAgentRequest(BaseModel):
    """
    Consente di inviare una stringa JSON grezza o un dizionario.
    Template hardcoded di esempio:
    {
        "name": "organ_security",
        "level": 1,
        "parent_agent_name": "Brain",
        "managed_targets": ["front_door_lock", "alarm_system"],
        "sub_agent_names": [],
        "system_prompt_template": "Sei l'organo di sicurezza...",
        "conflict_window_minutes": 15,
        "priority_weight": 500.0
    }
    """
    agent_definition: str


class HitlResumeRequest(BaseModel):
    """Payload per riprendere l'esecuzione del grafo sospeso da un interrupt HITL.
    
    decision: 'APPROVA' approva l'azione proposta dall'agente.
              'RESPINGI' rifiuta l'azione proposta.
              'OVERRIDE' ignora i lock di priorità ed esegue la direttiva del campo 'reasoning' in linguaggio naturale.
    """
    decision: str = "APPROVA"
    reasoning: str = "Approvato dall'utente tramite HITL API"
    thread_id: str | None = None


class UnblockTargetRequest(BaseModel):
    """Sblocca manualmente o event-driven un dispositivo in stato REJECTED/BLOCKED."""
    target: str = "ac_living_room"
    reasoning: str = "Sblocco manuale via API / evento sensore esterno"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    return {"status": "ok", "version": "2.1.0", "architecture": "Hierarchical N-Level (Brain -> Organs -> Components)"}


# --- Grafo Agenti ---

@app.post("/graph/run", tags=["Graph"])
async def run_graph_cycle(body: RunCycleRequest):
    """Esegue un singolo ciclo del grafo agenti LangGraph."""
    thread_id = body.thread_id or _THREAD_ID
    config = {"configurable": {"thread_id": thread_id}}
    state = {
        "messages": [],
        "readings": body.sensor_readings,
        "recent_events": [],
        "pending_escalations": [],
        "next_agent": body.force_next_agent,
        "hitl_required": False,
        "config": {"readings_window_hours": 4},
    }
    result = await _graph.ainvoke(state, config=config)
    messages = result.get("messages", [])
    return {
        "thread_id": thread_id,
        "next_agent": result.get("next_agent"),
        "last_message": messages[-1].content if messages else None,
        "pending_escalations": result.get("pending_escalations", []),
    }


@app.get("/graph/state", tags=["Graph"])
async def get_graph_state(thread_id: str | None = None):
    """Restituisce lo stato attuale del grafo dal checkpointer (inclusi interrupt pendenti)."""
    config = {"configurable": {"thread_id": thread_id or _THREAD_ID}}
    snapshot = await _graph.aget_state(config)
    tasks = [
        {"id": t.id, "name": t.name, "interrupts": [str(i) for i in t.interrupts]}
        for t in snapshot.tasks
    ]
    return {
        "next": snapshot.next,
        "values": snapshot.values,
        "tasks": tasks,
        "is_interrupted": len(snapshot.next) > 0 and len(snapshot.tasks) > 0 and len(snapshot.tasks[0].interrupts) > 0,
    }


@app.post("/graph/resume", tags=["Graph"])
async def resume_graph(body: HitlResumeRequest):
    """Riprende l'esecuzione del grafo dopo un interrupt HITL inviando la decisione umana (APPROVA / RESPINGI)."""
    from langgraph.types import Command
    config = {"configurable": {"thread_id": body.thread_id or _THREAD_ID}}
    command = Command(resume={"decision": body.decision, "reasoning": body.reasoning})
    result = await _graph.ainvoke(command, config=config)
    messages = result.get("messages", [])
    return {
        "status": "resumed",
        "decision_applied": body.decision,
        "last_message": messages[-1].content if messages else None,
        "next_agent": result.get("next_agent"),
    }


# --- Human-in-the-Loop (HITL) Dynamic Config ---

@app.get("/hitl/config", tags=["Human-in-the-Loop"])
async def get_hitl_config():
    """Restituisce la configurazione dinamica dei punti di interrupt HITL e l'attesa massima."""
    from app.graph.hitl_config import hitl_manager
    return hitl_manager.get_config()


@app.post("/hitl/config", tags=["Human-in-the-Loop"])
async def update_hitl_config(body: HitlConfigSchema):
    """
    Imposta dinamica dei punti di interrupt HITL (nodi, target sensori, azioni) e l'attesa massima.
    """
    from app.graph.hitl_config import hitl_manager
    updated = hitl_manager.update_config(
        hitl_all=body.hitl_all,
        hitl_nodes=body.hitl_nodes,
        hitl_targets=body.hitl_targets,
        hitl_actions=body.hitl_actions,
        max_wait_seconds=body.max_wait_seconds,
    )
    return {"status": "updated", "config": updated}


# --- Dynamic Sub-Agents & Hierarchy Management ---

@app.get("/agents", tags=["Dynamic Agents"])
async def list_agents():
    """Elenca tutti i sotto-agenti registrati a qualsiasi livello della gerarchia."""
    agents = await registry.get_all_agent_configs()
    return {"count": len(agents), "agents": agents}


@app.get("/agents/hierarchy", tags=["Dynamic Agents"])
async def get_agent_hierarchy():
    """Restituisce l'albero gerarchico completo: Cervello (Brain) -> Organi -> Componenti dell'Organo."""
    return await registry.get_hierarchy_tree()


@app.post("/agents/create", tags=["Dynamic Agents"])
async def create_sub_agent(body: CreateSubAgentRequest):
    """
    Crea ed istanzia a runtime un nuovo sotto-agente (Organo o Componente) registrandolo nel grafo.
    Accetta un JSON grezzo come stringa o un dizionario.
    """
    try:
        raw = json.loads(body.agent_definition)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"agent_definition non è JSON valido: {e}")

    # Normalizzazione campo nome ('name' oppure 'agent_name')
    name = raw.get("name") or raw.get("agent_name")
    if not name:
        raise HTTPException(status_code=422, detail="Campo 'name' o 'agent_name' obbligatorio.")

    managed_targets = raw.get("managed_targets")
    if not managed_targets or not isinstance(managed_targets, list):
        raise HTTPException(status_code=422, detail="Campo 'managed_targets' (lista non vuota) obbligatorio.")

    config = {
        "name": name,
        "level": int(raw.get("level", 1)),
        "parent_agent_name": raw.get("parent_agent_name", "Brain"),
        "managed_targets": managed_targets,
        "sub_agent_names": raw.get("sub_agent_names", []),
        "system_prompt_template": raw.get("system_prompt_template") or raw.get("system_prompt"),
        "user_prompt_template": raw.get("user_prompt_template") or raw.get("user_prompt"),
        "conflict_window_minutes": int(raw.get("conflict_window_minutes", 30)),
        "priority_weight": float(raw.get("priority_weight", 1.0)),
    }

    # Salva su DB e ricompila il grafo
    registered_cfg = await registry.register_agent_config(config)
    await _recompile_system_graph()

    return {
        "status": "registered_and_compiled",
        "agent_name": name,
        "level": config["level"],
        "parent_agent_name": config["parent_agent_name"],
        "managed_targets": config["managed_targets"],
        "graph_node_active": True,
    }


@app.delete("/agents/{agent_name}", tags=["Dynamic Agents"])
async def delete_agent(agent_name: str):
    """Rimuove un sotto-agente registrato e ricompila il grafo."""
    deleted = await registry.delete_agent(agent_name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_name}' non trovato.")
    await _recompile_system_graph()
    return {"status": "deleted", "agent_name": agent_name}


# --- Tool IoT ---

@app.get("/tools", tags=["IoT Tools"])
async def list_tools():
    """Elenca tutti i tool registrati e il loro valore corrente."""
    out = {}
    for name, tool in _shared_tools.items():
        out[name] = {
            "value": await tool.get_tool_value(),
            "unit": getattr(tool, "unit", ""),
        }
    return out


@app.get("/tools/{device_id}", tags=["IoT Tools"])
async def get_tool_endpoint(device_id: str):
    """Legge il valore corrente di un tool. Se il dispositivo non è ancora registrato, viene creato on-demand con stato OFF."""
    from app.tools.sensor_tools import get_tool as _get_tool
    tool = _shared_tools.get(device_id)
    if not tool:
        # Creazione on-demand: registra il dispositivo nel registry singleton
        tool = _get_tool(device_id, initial_value="OFF", unit="")
        _shared_tools[device_id] = tool
    return {"device_id": device_id, "value": await tool.get_tool_value(), "unit": getattr(tool, "unit", "")}


@app.post("/tools", tags=["IoT Tools"])
async def set_tool_endpoint(body: ToolWriteRequest):
    """Imposta direttamente il valore di un tool (bypass agenti). Crea il tool on-demand se non esiste."""
    from app.tools.sensor_tools import get_tool as _get_tool
    tool = _shared_tools.get(body.target)
    if not tool:
        tool = _get_tool(body.target, initial_value="OFF", unit="")
        _shared_tools[body.target] = tool
    await tool.set_tool_value(body.value)
    return {"device_id": body.target, "new_value": await tool.get_tool_value()}


# --- Database / Audit Log ---

@app.get("/events", tags=["Database"])
async def get_events(window_minutes: int = 240):
    """Recupera gli ultimi eventi dal DB nella finestra temporale specificata."""
    log = EventLog(target=["all"], frequency=window_minutes)
    events = await log.get_recent_events()
    return {"count": len(events), "events": events}


@app.post("/events/seed-conflict", tags=["Database"])
async def seed_conflict(body: SeedConflictRequest):
    """Semina un evento di conflitto nel DB per simulare escalation."""
    log = EventLog()
    await log.log_event(
        actor=body.actor,
        action=body.action,
        target=body.target,
        old_value=body.old_value,
        new_value=body.new_value,
        reasoning=body.reasoning,
        escalated=False,
    )
    tool = _shared_tools.get(body.target)
    if tool:
        await tool.set_tool_value(body.new_value)
    return {"seeded": True, "event": body.model_dump()}


@app.delete("/events/reset-conflicts/{target}", tags=["Database"])
async def reset_conflicts(target: str):
    """Marca come risolti tutti gli eventi ESCALATION_PROPOSED per il target."""
    log = EventLog()
    await log.mark_resolved(target)
    return {"resolved": True, "target": target}


@app.post("/events/unblock", tags=["Database"])
async def unblock_target_endpoint(body: UnblockTargetRequest):
    """Sblocca un dispositivo in stato REJECTED/BLOCKED ripristinandolo a OFF/IDLE."""
    log = EventLog()
    await log.unblock_target(target=body.target, reasoning=body.reasoning)
    tool = _shared_tools.get(body.target)
    if tool:
        await tool.set_tool_value("OFF")
    return {"unblocked": True, "target": body.target, "reasoning": body.reasoning}


# --- LLM Proxy (MAO) ---

@app.post("/llm/invoke", tags=["LLM"])
async def invoke_llm(body: LlmProxyRequest):
    """Chiama direttamente il MAO con system/user prompt e provider a scelta."""
    from app.MAO.model_access_object import Mao
    mao = Mao()
    try:
        response = await mao.call_model(
            system_prompt=body.system_prompt,
            user_prompt=body.user_prompt,
            provider=body.provider,
            model=body.model,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            fallback_on_error=body.fallback_on_error,
            enable_reasoning=body.enable_reasoning,
        )
        return {"response": response, "provider": body.provider or mao.default_provider}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        await mao.aclose()


# --- Health Check Macro (check_body_status) ---

@app.post("/graph/health-check", tags=["Graph"])
async def macro_health_check():
    """Invoca il check_body_status dell'Orchestratore (analisi macro trend)."""
    from app.graph.orchestrator import BrainAgent
    brain = BrainAgent(tools=list(_shared_tools.values()))
    log = EventLog(target=["all"], frequency=240)
    recent_events = await log.get_recent_events()
    readings = [
        {"sensor_id": k, "agent_owner": "brain", "value": str(await v.get_tool_value()), "unit": getattr(v, "unit", "")}
        for k, v in _shared_tools.items()
    ]
    from app.graph.state import GraphState
    state: GraphState = {
        "messages": [], "readings": readings, "recent_events": recent_events,
        "pending_escalations": [], "next_agent": "END", "hitl_required": False, "config": {}
    }
    result = await brain.check_body_status(state, readings, recent_events)
    messages = result.get("messages", [])
    return {"result": messages[-1].content if messages else None}


# --- System Reset Endpoint ---

@app.delete("/system/reset", tags=["System"])
async def reset_system_state():
    """
    Svuota il database degli eventi, cancella il registro degli agenti dinamici, 
    ripristina la configurazione HITL e resetta lo stato dei tool IoT condivisi.
    """
    import aiosqlite
    from app.db.database import DB_PATH
    from app.graph.hitl_config import hitl_manager

    # 1. Svuota le tabelle SQLite (events, readings, agents_registry)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM events")
        await db.execute("DELETE FROM readings")
        await db.execute("DELETE FROM agents_registry")
        await db.commit()

    # 2. Resetta HITL manager
    hitl_manager.update_config(hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)

    # 3. Resetta lo stato dei tool IoT condivisi
    for name, tool in _shared_tools.items():
        if hasattr(tool, "set_tool_value"):
            if "lock" in name:
                await tool.set_tool_value("LOCKED")
            elif "alarm" in name:
                await tool.set_tool_value("DISARMED")
            elif "lights" in name:
                await tool.set_tool_value("0%")
            else:
                await tool.set_tool_value("OFF")

    # 4. Ricompila la topologia del grafo
    await _recompile_system_graph()

    return {"status": "reset_complete", "message": "Database svuotato, registro agenti resettato e grafo ricompilato."}
