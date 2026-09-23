"""
Rotte della simulazione energetica (/energia/...). I permessi sono nella matrice di `app.core.ruoli`:
lettura per il tirocinante, controllo dell'esecuzione per il medico di guardia, scenario ed eventi per il primario.

Le rotte si registrano direttamente sull'app con `registra_rotte` (non con un APIRouter incluso): così compaiono
in `app.routes` una per una e i test che verificano la protezione di ogni endpoint le vedono.
"""

import asyncio
import json
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.configurazione import get_configurazione
from app.core.ruoli import Ruolo
from app.simulazione.esecutore import esecutore
from app.simulazione.motore import LEVE
from app.simulazione.sistema_nervoso import sistema_nervoso

_INTESTAZIONI_STREAM = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ScenarioRequest(BaseModel):
    """Nuova simulazione: sostituisce quella attuale."""
    tipo: Literal["italia", "generata"] = "italia"
    seed: int = 42
    durata_ore: float = Field(8760, gt=0, description="Durata simulata in ore (8760 = un anno)")
    passo_minuti: int = Field(60, description="Passo di simulazione: 5, 15, 30 o 60 minuti")
    inizio: str | None = Field(None, description="Data e ora di inizio, es. 2026-01-01T00:00")
    zone: int = 20
    citta: int = 200
    centrali: int = 120


class ControlloRequest(BaseModel):
    azione: Literal["avvia", "pausa", "passo", "velocita"]
    velocita: float | None = Field(None, description="Secondi simulati per secondo reale; 0 = massima")
    passi: int = 1


class LevaRequest(BaseModel):
    leva: str
    valore: Literal["ON", "OFF"]
    motivazione: str = "Comando manuale dell'operatore dalla pagina /energia"


class CicloRequest(BaseModel):
    motivo: str = "Ciclo avviato a mano dall'operatore"


class DecisioneRequest(BaseModel):
    """Risposta alla richiesta del grafo in attesa: APPROVA, RESPINGI oppure OVERRIDE (con la direttiva in `motivazione`)."""
    decisione: str = "APPROVA"
    motivazione: str = ""


class SistemaNervosoConfigRequest(BaseModel):
    automatico: bool | None = Field(None, description="Il sistema nervoso avvia da solo i cicli del grafo (usa il modello LLM reale)")
    intervallo_minimo_s: float | None = Field(None, description="Secondi reali minimi tra due cicli automatici")
    cicli_automatici_massimi: int | None = Field(None, description="Tetto ai cicli automatici (protegge i crediti)")
    azzera_contatore: bool = False


class EventoRequest(BaseModel):
    tipo: str
    entita: str | None = None
    durata_ore: float | None = None


def _stato_completo() -> dict[str, Any]:
    sim = esecutore.simulatore()
    return {
        "esecuzione": esecutore.stato_esecuzione(), "istantanea": sim.istantanea(), "riepilogo": sim.riepilogo(),
        "sistema_nervoso": sistema_nervoso.stato_pubblico(),
    }


async def rete():
    """Topologia della rete (parte statica): entità, posizioni, capacità. L'ordine è quello di /energia/stato."""
    topologia = esecutore.simulatore().topologia()  # prima: crea lo scenario al primo accesso
    return {"versione_rete": esecutore.versione_rete, **topologia, "albero_agenti": sistema_nervoso.albero()}


async def stato():
    """Stato dell'esecuzione, valori attuali di tutte le entità, anomalie ed eventi, statistiche cumulative."""
    return _stato_completo()


async def stream(request: Request, frequenza: float = Query(4, gt=0, le=20), limite: int | None = Query(None, ge=1)):
    """
    Flusso Server-Sent Events con lo stato completo, `frequenza` volte al secondo se qualcosa è cambiato
    (altrimenti un messaggio ogni 2 secondi per tenere viva la connessione). `limite` chiude dopo N messaggi.
    """
    async def flusso():
        inviati, ultima_versione, ultimo_invio = 0, -1, 0.0
        ciclo = asyncio.get_running_loop()
        while limite is None or inviati < limite:
            adesso = ciclo.time()
            if esecutore.versione != ultima_versione or adesso - ultimo_invio >= 2.0:
                ultima_versione, ultimo_invio = esecutore.versione, adesso
                yield "data: " + json.dumps(_stato_completo(), ensure_ascii=False, separators=(",", ":")) + "\n\n"
                inviati += 1
                if limite is not None and inviati >= limite:
                    break
            if await request.is_disconnected():
                break
            await asyncio.sleep(1 / frequenza)

    return StreamingResponse(flusso(), media_type="text/event-stream", headers=_INTESTAZIONI_STREAM)


def _campiona(righe: list[dict[str, Any]], massimo: int) -> list[dict[str, Any]]:
    """Riduce una serie a `massimo` punti facendo la media dei valori numerici di gruppi consecutivi."""
    if len(righe) <= massimo:
        return righe
    passo = len(righe) / massimo
    risultato = []
    for i in range(massimo):
        gruppo = righe[int(i * passo):max(int((i + 1) * passo), int(i * passo) + 1)]
        media: dict[str, Any] = {}
        for chiave, valore in gruppo[0].items():
            if isinstance(valore, (int, float)) and not isinstance(valore, bool):
                media[chiave] = round(sum(r[chiave] for r in gruppo) / len(gruppo), 4)
            elif isinstance(valore, dict):
                media[chiave] = {k: round(sum(r[chiave][k] for r in gruppo) / len(gruppo), 1) for k in valore}
            else:
                media[chiave] = valore
        risultato.append(media)
    return risultato


async def storico(scala: Literal["passi", "giorni"] = "passi", massimo: int = Query(600, ge=10, le=20000)):
    """Serie nazionali: `passi` (ultimi 7 giorni simulati, un punto per passo) o `giorni` (tutta la simulazione)."""
    sim = esecutore.simulatore()
    righe = list(sim.serie_passi) if scala == "passi" else list(sim.serie_giorni)
    return {"scala": scala, "punti_totali": len(righe), "serie": _campiona(righe, massimo)}


async def entita(entita_id: str):
    """Tutti i valori di un'entità, eventi e anomalie che la riguardano e il suo storico orario (ultimi 14 giorni)."""
    try:
        return {"id": entita_id, **esecutore.simulatore().descrivi_entita(entita_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Entità '{entita_id}' non trovata.")


async def controllo(body: ControlloRequest):
    """Avvia, mette in pausa, avanza di N passi o cambia la velocità della simulazione."""
    try:
        if body.velocita is not None:
            esecutore.imposta_velocita(body.velocita)
        elif body.azione == "velocita":
            raise ValueError("Indica il campo 'velocita'.")
        if body.azione == "avvia":
            esecutore.avvia()
        elif body.azione == "pausa":
            esecutore.pausa()
        elif body.azione == "passo":
            esecutore.passo_singolo(body.passi)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return esecutore.stato_esecuzione()


async def scenario(body: ScenarioRequest):
    """Crea una nuova simulazione (Italia o rete generata per le prove di carico) e ferma quella in corso."""
    if sistema_nervoso.stato == "in_esecuzione":
        raise HTTPException(status_code=409, detail="Il grafo degli agenti sta lavorando: attendi la fine del ciclo.")
    try:
        esecutore.configura(**body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return esecutore.stato_esecuzione()


async def evento(body: EventoRequest):
    """Inietta un evento (guasto, crisi gas, ondata di calore o di freddo, calma di vento, picco del gas, ripristino)."""
    try:
        return esecutore.inietta_evento(body.tipo, body.entita, body.durata_ore)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


async def leva(body: LevaRequest):
    """Comando manuale di una leva da parte dell'operatore, validato da configurazione.toml e registrato nell'audit log."""
    from app.tools.event_log import EventLog
    if body.leva not in LEVE:
        raise HTTPException(status_code=422, detail=f"Leva sconosciuta: '{body.leva}'.")
    validazione = get_configurazione().valida_comando(body.leva, body.valore)
    if not validazione.ammesso:
        raise HTTPException(status_code=422, detail=validazione.motivo)
    sim = esecutore.simulatore()
    precedente = sim.leve[body.leva]
    sim.imposta_leva(body.leva, validazione.valore)
    # Il comando dell'operatore vale per un po': gli agenti non lo ribaltano per qualche ora simulata.
    opposto = "OFF" if validazione.valore == "ON" else "ON"
    sim.imposta_veto(body.leva, opposto, autore="operatore", motivo=body.motivazione)
    esecutore.versione += 1
    await EventLog().log_event(actor="operatore", action=f"LEVA_{validazione.valore}", target=body.leva,
                               old_value=precedente, new_value=validazione.valore, reasoning=body.motivazione[:1000])
    return {"leva": body.leva, "valore": sim.leve[body.leva], "precedente": precedente, "veto": sim.veto(body.leva)}


async def ciclo_grafo(body: CicloRequest):
    """Sveglia a mano il grafo degli agenti del dominio energia (un ciclo, modello LLM reale)."""
    try:
        thread_id = sistema_nervoso.avvia_ciclo(body.motivo[:500])
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"thread_id": thread_id, **sistema_nervoso.stato_pubblico(passi=5)}


async def decisione_operatore(body: DecisioneRequest, request: Request):
    """Risponde alla richiesta HITL del grafo energia. OVERRIDE richiede il primario."""
    from app.api.main import richiedi_ruolo
    if "OVERRIDE" in body.decisione.upper():
        richiedi_ruolo(request, Ruolo.PRIMARIO)
    try:
        sistema_nervoso.decidi(body.decisione.upper(), body.motivazione[:1000])
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return sistema_nervoso.stato_pubblico(passi=5)


async def configura_sistema_nervoso(body: SistemaNervosoConfigRequest):
    """Modalità automatica, intervallo minimo tra cicli e tetto ai cicli automatici."""
    try:
        sistema_nervoso.configura(**body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return sistema_nervoso.stato_pubblico(passi=0)


def registra_rotte(app: FastAPI) -> None:
    for metodo, percorso, gestore in (
        ("GET", "/energia/rete", rete),
        ("GET", "/energia/stato", stato),
        ("GET", "/energia/stream", stream),
        ("GET", "/energia/storico", storico),
        ("GET", "/energia/entita/{entita_id}", entita),
        ("POST", "/energia/controllo", controllo),
        ("POST", "/energia/scenario", scenario),
        ("POST", "/energia/evento", evento),
        ("POST", "/energia/leva", leva),
        ("POST", "/energia/sistema-nervoso/ciclo", ciclo_grafo),
        ("POST", "/energia/sistema-nervoso/decisione", decisione_operatore),
        ("POST", "/energia/sistema-nervoso/configura", configura_sistema_nervoso),
    ):
        app.add_api_route(percorso, gestore, methods=[metodo], tags=["Simulazione energetica"])
