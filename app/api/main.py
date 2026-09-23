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
import logging
from pathlib import Path
from typing import Any
import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from app.api.energia import registra_rotte as registra_rotte_energia
from app.agents.agent_registry import AgenteConFigliError, AgentRegistry, ErroreGerarchia
from app.checkpointer import apri_checkpointer, chiudi_checkpointer, get_checkpointer, svuota_checkpoint
from app.core.configurazione import get_configurazione
from app.core.errori_llm import ErroreLLM, oscura_segreti
from app.core.modelli_agenti import (
    NOME_BRAIN, assicura_tabella as assicura_tabella_modelli, imposta_modello, leggi_modello, nome_canonico_brain,
    rimuovi_modello, risolvi_modello,
)
from app.core.risultati import comanda_tool, leggi_tool
from app.core.ruoli import (
    ROTTE_PUBBLICHE, Ruolo, autenticazione_attiva, avvisi_configurazione, ruolo_della_chiave, ruolo_minimo,
)
from app.db.database import Database
from app.graph.builder import build_graph
from app.graph.hitl_config import NON_IMPOSTATO, HitlConfigSchema
from app.graph.timer_hitl import GestoreTimerHitl, timer_attivo
from app.MAO.model_access_object import PROVIDER_NOTI, Mao, normalizza_provider
from app.simulazione.esecutore import esecutore as esecutore_energia
from app.simulazione.sistema_nervoso import sistema_nervoso
from app.tools.event_log import EventLog

# ---------------------------------------------------------------------------
# Lifespan & Global State
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

db = Database()
registry = AgentRegistry()
_graph = None
_shared_tools: dict = {}
_graph_lock = asyncio.Lock()
gestore_timer = GestoreTimerHitl(lambda: _graph)


async def _recompile_system_graph():
    """Ricarica le istanze dal registro e ricompila il grafo LangGraph."""
    global _graph, _shared_tools
    async with _graph_lock:
        agent_instances = await registry.build_agent_instances(_shared_tools)
        # Compilazione e swap atomici di grafico e tool condivisi
        checkpointer = get_checkpointer()
        if checkpointer is None:
            logger.warning("Checkpointer SQLite non aperto: il grafo usa uno stato volatile in memoria.")
        _graph, _shared_tools = build_graph(custom_agent_instances=agent_instances, checkpointer=checkpointer)


_intestazione_chiave = APIKeyHeader(name="X-API-Key", auto_error=False)

def _nega_per_ruolo(ruolo: Ruolo, richiesto: Ruolo) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail=f"Ruolo insufficiente: serve '{richiesto.nome}', la chiave usata è di '{ruolo.nome}'.",
    )


def richiedi_ruolo(request: Request | None, minimo: Ruolo) -> None:
    """Verifica un requisito di ruolo più stretto di quello dell'endpoint (es. OVERRIDE su /graph/resume)."""
    ruolo = getattr(getattr(request, "state", None), "ruolo", Ruolo.PRIMARIO)
    if ruolo is not None and ruolo < minimo:
        raise _nega_per_ruolo(ruolo, minimo)


async def autentica_e_autorizza(request: Request, chiave: str | None = Security(_intestazione_chiave)) -> None:
    """
    Autentica la chiave inviata nell'header `X-API-Key` (401 se manca o non è valida) e verifica che il suo ruolo
    (tirocinante, medico_di_guardia, primario) basti per l'endpoint (403), secondo la matrice in `app.core.ruoli`.
    Se nessuna chiave è configurata l'API resta aperta e chi la usa vale come primario (avviso all'avvio).
    """
    percorso = getattr(request.scope.get("route"), "path", request.url.path)
    if (request.method, percorso) in ROTTE_PUBBLICHE:
        request.state.ruolo = None
        return
    if not autenticazione_attiva():
        request.state.ruolo = Ruolo.PRIMARIO
        return
    ruolo = ruolo_della_chiave(chiave)
    if ruolo is None:
        raise HTTPException(
            status_code=401,
            detail="API key mancante o non valida: invia l'header X-API-Key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    request.state.ruolo = ruolo
    richiesto = ruolo_minimo(request.method, percorso)
    if ruolo < richiesto:
        raise _nega_per_ruolo(ruolo, richiesto)


@asynccontextmanager
async def lifespan(app: FastAPI):
    for avviso in avvisi_configurazione():
        logger.warning(avviso)
    await db.init_db()
    await registry.init_registry_db()
    await assicura_tabella_modelli()
    await gestore_timer.assicura_tabella()
    await apri_checkpointer()
    await _recompile_system_graph()
    sorveglianza_timer = asyncio.create_task(gestore_timer.sorveglia())
    sistema_nervoso.avvia_sorveglianza()
    try:
        yield
    finally:
        sorveglianza_timer.cancel()
        try:
            await sorveglianza_timer
        except asyncio.CancelledError:
            pass
        await sistema_nervoso.chiudi()
        await esecutore_energia.chiudi()
        await chiudi_checkpointer()


app = FastAPI(
    title="LangBrain API",
    description="API FastAPI con LangGraph per la gestione gerarchica N-livelli (Cervello -> Organi -> Componenti)",
    version="2.1.0",
    lifespan=lifespan,
    dependencies=[Depends(autentica_e_autorizza)],
)

registra_rotte_energia(app)


@app.exception_handler(ErroreLLM)
async def gestisci_errore_llm(request: Request, errore: ErroreLLM):
    """Il modello non è utilizzabile: 503 con causa e suggerimento per l'operatore."""
    return JSONResponse(status_code=503, content={"detail": errore.messaggio, "errore_llm": errore.come_dizionario()})


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


class ToolFaultRequest(BaseModel):
    """Simula un guasto del dispositivo (solo tool mock): le operazioni successive falliscono con il messaggio indicato."""
    fault: str | None = None
    """Messaggio d'errore del guasto simulato; null rimuove il guasto."""
    operations: int | None = None
    """Numero di operazioni che falliscono prima che il guasto rientri; null = finché non viene rimosso."""
    commands_only: bool = False
    """Se true le letture funzionano e falliscono solo i comandi (sensore attivo, attuatore bloccato)."""


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
    level: int | None = None
    """Facoltativo: il livello è derivato (livello del padre + 1). Se indicato deve coincidere, altrimenti 422."""
    parent_agent_name: str | None = "Brain"
    """Nome dell'agente Padre (deve essere già registrato) verso cui fare escalation. Default: 'Brain'."""
    managed_targets: list[str]
    """Elenco di target/dispositivi controllati da questo agente. Condivisibili solo con antenati/discendenti."""
    sub_agent_names: list[str] = []
    """Solo informativo: i figli sono derivati da 'parent_agent_name' dei figli stessi. Non elencare figli non registrati."""
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


class ModelloAgenteRequest(BaseModel):
    """Provider e modello LLM propri di un agente. Se non impostati un agente eredita quelli del padre."""
    provider: str
    """Uno tra google_studio, openrouter, mistral, local (il provider deve avere la chiave configurata)."""
    model: str | None = None
    """Facoltativo: se omesso si usa il modello di default del provider (es. MISTRAL_MODEL)."""


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


_PAGINA_DEMO = Path(__file__).resolve().parent.parent / "static" / "demo_grafo.html"
_INTESTAZIONI_PAGINA = {
    # La pagina è autosufficiente: nessuna risorsa esterna, chiamate solo verso questa stessa API.
    "Content-Security-Policy": "default-src 'none'; img-src data:; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'",
    "Cache-Control": "no-store",
}


@app.get("/demo", include_in_schema=False)
async def pagina_demo():
    """Pagina dimostrativa che mostra il grafo in azione (si disattiva con `[demo] pagina_web = 0`)."""
    if not get_configurazione().demo_pagina_web or not _PAGINA_DEMO.is_file():
        raise HTTPException(status_code=404, detail="Pagina dimostrativa non disponibile.")
    return HTMLResponse(_PAGINA_DEMO.read_text(encoding="utf-8"), headers=_INTESTAZIONI_PAGINA)


_PAGINA_ENERGIA = Path(__file__).resolve().parent.parent / "static" / "energia.html"


@app.get("/energia", include_in_schema=False)
async def pagina_energia():
    """Pagina della simulazione energetica (si disattiva con `[demo] pagina_web = 0`, come /demo)."""
    if not get_configurazione().demo_pagina_web or not _PAGINA_ENERGIA.is_file():
        raise HTTPException(status_code=404, detail="Pagina della simulazione energetica non disponibile.")
    return HTMLResponse(_PAGINA_ENERGIA.read_text(encoding="utf-8"), headers=_INTESTAZIONI_PAGINA)


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
    graph = _graph
    if graph is None:
        raise HTTPException(status_code=500, detail="Graph not available")
    async with gestore_timer.blocco(thread_id):
        result = await graph.ainvoke(state, config=config)
    messages = result.get("messages", [])
    return {
        "thread_id": thread_id,
        "next_agent": result.get("next_agent"),
        "last_message": messages[-1].content if messages else None,
        "pending_escalations": result.get("pending_escalations", []),
        "hitl_timer": await gestore_timer.stato(thread_id),
    }


def _evento_sse(dati: dict) -> str:
    return "data: " + json.dumps(dati, ensure_ascii=False, default=str) + "\n\n"


def _descrivi_aggiornamento(nodo: str, aggiornamento: Any) -> dict:
    """Riassume ciò che un nodo ha prodotto in un ciclo: chi lo ha eseguito, a chi passa la mano e il suo messaggio."""
    dati = aggiornamento if isinstance(aggiornamento, dict) else {}
    messaggi = dati.get("messages") or []
    contenuto = getattr(messaggi[-1], "content", None) if messaggi else None
    return {
        "tipo": "nodo",
        "nodo": nodo,
        "prossimo": dati.get("next_agent"),
        "messaggio": str(contenuto)[:1500] if contenuto is not None else None,
        "escalation_pendenti": len(dati.get("pending_escalations") or []),
    }


async def _flusso_grafo(ingresso: Any, thread_id: str, solo_se_in_pausa: bool = False):
    """
    Esegue il grafo e produce un evento (Server-Sent Events) per ogni nodo che completa, poi uno di pausa se il grafo
    attende l'operatore e uno finale con lo stato del timer HITL. Il thread resta bloccato per tutta l'esecuzione,
    come nelle rotte non in streaming.
    """
    graph = _graph
    config = {"configurable": {"thread_id": thread_id}}
    yield _evento_sse({"tipo": "inizio", "thread_id": thread_id})
    try:
        async with gestore_timer.blocco(thread_id):
            if solo_se_in_pausa and (await gestore_timer._interrupt_corrente(thread_id))[0] is None:
                yield _evento_sse({"tipo": "errore", "codice": "NESSUNA_RICHIESTA", "messaggio": "Nessuna richiesta HITL in attesa su questo thread."})
                return
            async for blocco in graph.astream(ingresso, config=config, stream_mode="updates"):
                for nodo, aggiornamento in blocco.items():
                    if nodo == "__interrupt__":
                        for interruzione in aggiornamento:
                            yield _evento_sse({"tipo": "pausa", "richiesta": getattr(interruzione, "value", None)})
                    else:
                        yield _evento_sse(_descrivi_aggiornamento(nodo, aggiornamento))
    except asyncio.CancelledError:
        raise
    except ErroreLLM as errore:
        yield _evento_sse({"tipo": "errore", **errore.come_dizionario()})
    except Exception as errore:
        logger.exception("Errore durante l'esecuzione del grafo in streaming.")
        yield _evento_sse({"tipo": "errore", "codice": "ERRORE_GRAFO", "messaggio": oscura_segreti(str(errore))[:500]})
    istantanea = await graph.aget_state(config)
    yield _evento_sse({
        "tipo": "fine",
        "in_pausa": any(t.interrupts for t in istantanea.tasks),
        "next_agent": (istantanea.values or {}).get("next_agent"),
        "hitl_timer": await gestore_timer.stato(thread_id),
    })


_INTESTAZIONI_STREAM = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@app.post("/graph/run/stream", tags=["Graph"])
async def run_graph_cycle_stream(body: RunCycleRequest):
    """Come POST /graph/run, ma risponde con un flusso di eventi (`text/event-stream`), uno per nodo eseguito."""
    if _graph is None:
        raise HTTPException(status_code=500, detail="Graph not available")
    ingresso = {
        "messages": [], "readings": body.sensor_readings, "recent_events": [], "pending_escalations": [],
        "next_agent": body.force_next_agent, "hitl_required": False, "config": {"readings_window_hours": 4},
    }
    return StreamingResponse(
        _flusso_grafo(ingresso, body.thread_id or _THREAD_ID), media_type="text/event-stream", headers=_INTESTAZIONI_STREAM,
    )


@app.post("/graph/resume/stream", tags=["Graph"])
async def resume_graph_stream(body: HitlResumeRequest, request: Request = None):
    """Come POST /graph/resume, ma in streaming: mostra i nodi eseguiti dopo la decisione dell'operatore."""
    from langgraph.types import Command
    if "OVERRIDE" in body.decision.upper():
        richiedi_ruolo(request, Ruolo.PRIMARIO)
    if _graph is None:
        raise HTTPException(status_code=500, detail="Graph not available")
    thread_id = body.thread_id or _THREAD_ID
    if (await gestore_timer._interrupt_corrente(thread_id))[0] is None:
        raise HTTPException(
            status_code=409,
            detail="Nessuna richiesta HITL in attesa su questo thread (forse è già stata gestita o è scaduta).",
        )
    comando = Command(resume={"decision": body.decision, "reasoning": body.reasoning})
    return StreamingResponse(
        _flusso_grafo(comando, thread_id, solo_se_in_pausa=True), media_type="text/event-stream", headers=_INTESTAZIONI_STREAM,
    )


@app.get("/graph/state", tags=["Graph"])
async def get_graph_state(thread_id: str | None = None):
    """
    Restituisce lo stato attuale del grafo dal checkpointer (inclusi interrupt pendenti) e, se il grafo è in attesa
    dell'operatore, lo stato del timer HITL con i secondi rimanenti (`hitl_timer`).
    """
    config = {"configurable": {"thread_id": thread_id or _THREAD_ID}}
    graph = _graph
    if graph is None:
        raise HTTPException(status_code=500, detail="Graph not available")
    snapshot = await graph.aget_state(config)
    tasks = [
        {"id": t.id, "name": t.name, "interrupts": [str(i) for i in t.interrupts]}
        for t in snapshot.tasks
    ]
    return {
        "next": snapshot.next,
        "values": snapshot.values,
        "tasks": tasks,
        # `next` può risultare vuoto in una seconda pausa dello stesso nodo: si guardano gli interrupt dei task.
        "is_interrupted": any(len(t.interrupts) > 0 for t in snapshot.tasks),
        "hitl_timer": await gestore_timer.stato(thread_id or _THREAD_ID),
    }


@app.post("/graph/resume", tags=["Graph"])
async def resume_graph(body: HitlResumeRequest, request: Request = None):
    """
    Riprende l'esecuzione del grafo dopo un interrupt HITL inviando la decisione umana (APPROVA / RESPINGI).
    APPROVA/RESPINGI/RETRY richiedono il ruolo medico_di_guardia; OVERRIDE (decisione critica) il ruolo primario.
    """
    from langgraph.types import Command
    if "OVERRIDE" in body.decision.upper():
        richiedi_ruolo(request, Ruolo.PRIMARIO)
    thread_id = body.thread_id or _THREAD_ID
    config = {"configurable": {"thread_id": thread_id}}
    command = Command(resume={"decision": body.decision, "reasoning": body.reasoning})
    graph = _graph
    if graph is None:
        raise HTTPException(status_code=500, detail="Graph not available")
    async with gestore_timer.blocco(thread_id):
        if (await gestore_timer._interrupt_corrente(thread_id))[0] is None:
            raise HTTPException(
                status_code=409,
                detail="Nessuna richiesta HITL in attesa su questo thread (forse è già stata gestita o è scaduta).",
            )
        result = await graph.ainvoke(command, config=config)
    messages = result.get("messages", [])
    return {
        "status": "resumed",
        "decision_applied": body.decision,
        "last_message": messages[-1].content if messages else None,
        "next_agent": result.get("next_agent"),
        "hitl_timer": await gestore_timer.stato(thread_id),
    }


@app.get("/hitl/scadenze", tags=["Human-in-the-Loop"])
async def get_hitl_deadlines():
    """Elenca le richieste HITL in attesa con il timer e i secondi rimanenti, dalla più urgente."""
    return {"attivo": timer_attivo(), "richieste": await gestore_timer.elenco()}


# --- Human-in-the-Loop (HITL) Dynamic Config ---

@app.get("/hitl/config", tags=["Human-in-the-Loop"])
async def get_hitl_config():
    """Restituisce la configurazione dinamica dei punti di interrupt HITL e l'attesa massima, più le scelte di configurazione.toml."""
    from app.graph.hitl_config import hitl_manager
    scelte = get_configurazione()
    return {
        **hitl_manager.get_config().model_dump(),
        # Scelte lette da configurazione.toml (sola lettura): quale flusso HITL è attivo e come si comporta il timer
        "configurazione_file": {
            "livello": scelte.hitl_livello,
            "target_critici_brain": scelte.hitl_target_critici_brain,
            "timer_attivo": scelte.hitl_timer_attivo,
            "timer_predefinito_secondi": scelte.hitl_timer_secondi,
            "azione_alla_scadenza": scelte.hitl_azione_alla_scadenza,
        },
    }


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
        # null esplicito azzera l'attesa massima; se il campo è omesso resta invariata
        max_wait_seconds=body.max_wait_seconds if "max_wait_seconds" in body.model_fields_set else NON_IMPOSTATO,
        allow_override=getattr(body, "allow_override", None),
    )
    return {"status": "updated", "config": updated}


# --- Dynamic Sub-Agents & Hierarchy Management ---

@app.get("/agents", tags=["Dynamic Agents"])
async def list_agents():
    """Elenca tutti i sotto-agenti registrati a qualsiasi livello della gerarchia."""
    agents = await registry.get_all_agent_configs()
    for agente in agents:
        agente["llm"] = await _modello_effettivo(agente["name"])
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
        "level": raw.get("level"),
        "parent_agent_name": raw.get("parent_agent_name", "Brain"),
        "managed_targets": managed_targets,
        "sub_agent_names": raw.get("sub_agent_names", []),
        "system_prompt_template": raw.get("system_prompt_template") or raw.get("system_prompt"),
        "user_prompt_template": raw.get("user_prompt_template") or raw.get("user_prompt"),
        "conflict_window_minutes": int(raw.get("conflict_window_minutes", 30)),
        "priority_weight": float(raw.get("priority_weight", 1.0)),
    }

    # Provider/modello propri dell'agente (facoltativi): validati prima di toccare il registro
    modello_proprio = None
    if raw.get("provider") or raw.get("model"):
        modello_proprio = await _valida_provider_modello(raw.get("provider"), raw.get("model"))

    # Valida la gerarchia, salva su DB e ricompila il grafo
    try:
        registered_cfg = await registry.register_agent_config(config)
    except ErroreGerarchia as e:
        raise HTTPException(status_code=422, detail=str(e))
    if modello_proprio:
        await imposta_modello(registered_cfg["name"], *modello_proprio)
    await _recompile_system_graph()

    return {
        "status": "registered_and_compiled",
        "agent_name": registered_cfg["name"],
        "level": registered_cfg["level"],
        "parent_agent_name": registered_cfg["parent_agent_name"],
        "sub_agent_names": registered_cfg["sub_agent_names"],
        "managed_targets": registered_cfg["managed_targets"],
        "llm": await _modello_effettivo(registered_cfg["name"]),
        "graph_node_active": True,
    }


@app.delete("/agents/{agent_name}", tags=["Dynamic Agents"])
async def delete_agent(agent_name: str):
    """Rimuove un sotto-agente registrato e ricompila il grafo. Risponde 409 se ha ancora figli."""
    try:
        deleted = await registry.delete_agent(agent_name)
    except AgenteConFigliError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_name}' non trovato.")
    await rimuovi_modello(agent_name)
    await _recompile_system_graph()
    return {"status": "deleted", "agent_name": agent_name}


# --- Modello LLM per agente ---

async def _valida_provider_modello(provider: str | None, modello: str | None) -> tuple[str, str | None]:
    """Provider noto e con chiave configurata (422 altrimenti); il modello è facoltativo."""
    nome = normalizza_provider(provider)
    if not nome:
        raise HTTPException(status_code=422, detail="Il campo 'provider' è obbligatorio quando si indica un modello.")
    if nome not in PROVIDER_NOTI:
        raise HTTPException(status_code=422, detail=f"Provider '{provider}' sconosciuto: usa uno tra {list(PROVIDER_NOTI)}.")
    mao = Mao()
    try:
        abilitato = mao.providers[nome]["enabled"]
    finally:
        await mao.aclose()
    if not abilitato:
        raise HTTPException(status_code=422, detail=f"Il provider '{nome}' non ha una chiave API configurata nel file .env.")
    return nome, (modello or "").strip() or None


async def _modello_effettivo(nome: str) -> dict:
    """Provider e modello che l'agente userà davvero, con l'agente da cui derivano (proprio, un antenato o il predefinito)."""
    risolto = await risolvi_modello(nome)
    mao = Mao()
    try:
        descrizione = mao.descrivi_provider(risolto.provider, risolto.model)
    finally:
        await mao.aclose()
    return {
        **descrizione,
        "origine": risolto.origine,
        "ereditato": risolto.impostato and risolto.origine.casefold() != nome.casefold(),
    }


async def _agente_registrato(nome: str) -> str:
    """Nome canonico di un agente registrato (o del Brain); 404 se non esiste."""
    if nome.casefold() == NOME_BRAIN.casefold():
        return NOME_BRAIN
    for cfg in await registry.get_all_agent_configs():
        if cfg["name"].casefold() == nome.casefold():
            return cfg["name"]
    raise HTTPException(status_code=404, detail=f"Agente '{nome}' non trovato.")


async def _descrizione_modello_agente(nome: str) -> dict:
    proprio = await leggi_modello(nome)
    return {
        "agent": nome,
        "impostazione": {"provider": proprio[0], "model": proprio[1]} if proprio else None,
        "effettivo": await _modello_effettivo(nome),
    }


@app.get("/agents/{agent_name}/model", tags=["Dynamic Agents"])
async def get_agent_model(agent_name: str):
    """Impostazione propria e modello effettivo (con l'origine ereditata) di un agente, Brain compreso."""
    return await _descrizione_modello_agente(await _agente_registrato(agent_name))


@app.put("/agents/{agent_name}/model", tags=["Dynamic Agents"])
async def set_agent_model(agent_name: str, body: ModelloAgenteRequest):
    """
    Imposta provider e modello propri di un agente (Brain compreso). I suoi sotto-agenti senza impostazione propria
    lo ereditano. Ha effetto dalla chiamata LLM successiva, senza ricompilare il grafo.
    """
    nome = await _agente_registrato(agent_name)
    provider, modello = await _valida_provider_modello(body.provider, body.model)
    await imposta_modello(nome, provider, modello)
    return await _descrizione_modello_agente(nome)


@app.delete("/agents/{agent_name}/model", tags=["Dynamic Agents"])
async def delete_agent_model(agent_name: str):
    """Toglie l'impostazione propria: l'agente torna a ereditare il modello del padre (o il predefinito)."""
    nome = await _agente_registrato(agent_name)
    await rimuovi_modello(nome)
    return await _descrizione_modello_agente(nome)


# --- Tool IoT ---

async def _get_or_create_tool(device_id: str):
    """
    Restituisce il tool del dispositivo; se non esiste lo crea on-demand (stato OFF) nel registry condiviso,
    ma solo se il dispositivo è elencato in configurazione.toml (o la politica consente quelli non elencati).
    """
    from app.tools.sensor_tools import get_tool as _get_tool
    tool = _shared_tools.get(device_id)
    if not tool:
        if not get_configurazione().dispositivo_ammesso(device_id):
            raise HTTPException(
                status_code=404,
                detail=f"Il dispositivo '{device_id}' non è elencato in configurazione.toml: non viene creato.",
            )
        tool = _get_tool(device_id, initial_value="OFF", unit="")
        # Protegge la modifica della mappa condivisa
        async with _graph_lock:
            _shared_tools[device_id] = tool
    return tool


@app.get("/tools", tags=["IoT Tools"])
async def list_tools():
    """Elenca tutti i tool registrati e il loro valore corrente."""
    out = {}
    # Snapshot per evitare race con ricompilazione
    tools_snapshot = dict(_shared_tools)
    for name, tool in tools_snapshot.items():
        lettura = await leggi_tool(tool, name)
        out[name] = {"value": lettura.get("value"), "unit": getattr(tool, "unit", "")}
        if not lettura["success"]:
            out[name].update(error=lettura["response"], error_type=lettura.get("error_type"))
    return out


@app.get("/tools/{device_id}", tags=["IoT Tools"])
async def get_tool_endpoint(device_id: str):
    """
    Legge il valore corrente di un tool. Se il dispositivo non è ancora registrato, viene creato on-demand con stato OFF.
    Se il dispositivo è guasto risponde 502 con {device_name, success, response, error_type}.
    """
    tool = await _get_or_create_tool(device_id)
    lettura = await leggi_tool(tool, device_id)
    if not lettura["success"]:
        return JSONResponse(status_code=502, content=lettura)
    return {"device_id": device_id, "value": lettura["value"], "unit": getattr(tool, "unit", "")}


@app.post("/tools", tags=["IoT Tools"])
async def set_tool_endpoint(body: ToolWriteRequest):
    """
    Imposta direttamente il valore di un tool (bypass agenti). Crea il tool on-demand se non esiste.
    Il comando deve essere ammesso da configurazione.toml (dispositivo elencato e valore tra quelli ammessi), altrimenti 422.
    Se il dispositivo è guasto risponde 502 con {device_name, success, response, error_type}.
    """
    validazione = get_configurazione().valida_comando(body.target, body.value)
    if not validazione.ammesso:
        raise HTTPException(status_code=422, detail=validazione.motivo)
    tool = await _get_or_create_tool(body.target)
    comando = await comanda_tool(tool, body.target, validazione.valore)
    if not comando["success"]:
        return JSONResponse(status_code=502, content=comando)
    lettura = await leggi_tool(tool, body.target)
    return {"device_id": body.target, "new_value": lettura.get("value", validazione.valore)}


@app.post("/tools/{device_id}/fault", tags=["IoT Tools"])
async def set_tool_fault(device_id: str, body: ToolFaultRequest):
    """Simula (o rimuove) un guasto di un dispositivo mock, per provare troubleshooting ed escalation."""
    tool = await _get_or_create_tool(device_id)
    if not hasattr(tool, "imposta_guasto"):
        raise HTTPException(status_code=400, detail=f"Il tool '{device_id}' non supporta la simulazione di guasti.")
    tool.imposta_guasto(body.fault, body.operations, body.commands_only)
    return {"device_id": device_id, "fault": tool.guasto, "operations": tool.guasti_residui, "commands_only": tool.guasto_solo_comandi}


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
    except ErroreLLM as exc:
        raise HTTPException(status_code=503, detail=exc.messaggio) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        await mao.aclose()


# --- Health Check Macro (check_body_status) ---

@app.post("/graph/health-check", tags=["Graph"])
async def macro_health_check():
    """Invoca il check_body_status dell'Orchestratore (analisi macro trend)."""
    from app.graph.orchestrator import BrainAgent
    tools_snapshot = dict(_shared_tools)
    brain = BrainAgent(tools=list(tools_snapshot.values()))
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
    await assicura_tabella_modelli()
    await gestore_timer.assicura_tabella()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM agent_models")
        await db.execute("DELETE FROM hitl_scadenze")
        await db.commit()
    await svuota_checkpoint()

    # 2. Resetta HITL manager
    hitl_manager.update_config(hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)

    # 3. Resetta lo stato dei tool IoT condivisi
    async with _graph_lock:
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

    # 4. Ricompila la topologia del grafo (operazione protetta internamente)
    await _recompile_system_graph()

    return {"status": "reset_complete", "message": "Database svuotato, registro agenti resettato e grafo ricompilato."}
