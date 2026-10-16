"""
Approvazioni: la richiesta di una persona (un interrupt HITL del grafo) vista come un oggetto con un identificatore stabile.

Un thread del grafo ha al massimo UN interrupt in attesa, quindi al massimo una approvazione pendente per thread (con un
thread per appartamento: una per istanza). Le altre proposte restano nello stato del grafo (`pending_escalations`) e
arrivano una alla volta, quando la pendente viene decisa.

  - `approvazione_in_attesa(...)`: l'approvazione pendente del thread, con `approval_id` (l'id dell'interrupt di LangGraph,
    stabile finché la richiesta è aperta), `description` e `reason` in italiano, scadenza e azione alla scadenza.
  - `decidi(...)`: `approve` o `reject` (anche APPROVA/RESPINGI). OVERRIDE e ogni altra decisione sono rifiutate: nelle
    approvazioni non c'è OVERRIDE. Una seconda decisione sulla stessa approvazione dà l'esito `already_closed`, non
    un'eccezione. La decisione si registra nel registro (`events`) con `approval_id` e l'attore della richiesta.

Né `approvazione_in_attesa` né `decidi` sollevano per i casi previsti: restituiscono un esito strutturato.
"""

import logging
from dataclasses import dataclass
from typing import Any

from langgraph.types import Command
from sqlalchemy import select

from app.core import archivio
from app.db import schema
from app.tools.nervous_system import NervousSystem

logger = logging.getLogger(__name__)

# Esiti di `EsitoApprovazione.stato`
DECISA = "decisa"
GIA_CHIUSA = "already_closed"
NON_TROVATA = "non_trovata"
DECISIONE_NON_VALIDA = "decisione_non_valida"
ERRORE = "errore_interno"

# Decisioni ammesse -> la parola che il grafo capisce
_DECISIONI = {"approve": "APPROVA", "approva": "APPROVA", "reject": "RESPINGI", "respingi": "RESPINGI"}
_ESITO_DECISIONE = {"APPROVA": "applied", "RESPINGI": "rejected"}


@dataclass(frozen=True)
class Approvazione:
    approval_id: str
    thread_id: str
    tipo: str
    node: str | None
    target: str | None
    azione: str | None
    description: str
    reason: str
    max_wait_seconds: int | None = None
    expires_at: str | None = None
    expires_action: str | None = None


@dataclass(frozen=True)
class EsitoApprovazione:
    stato: str
    motivo: str
    approval_id: str | None = None
    decisione: str | None = None          # approve | reject
    decision_id: int | None = None
    risposta: str | None = None           # l'ultimo messaggio del grafo dopo la ripresa


def _testi(payload: dict[str, Any]) -> tuple[str, str]:
    """`description` e `reason` in italiano; un payload che li porta già (un nodo che li imposta) ha la precedenza."""
    tipo = str(payload.get("type", ""))
    nodo = payload.get("node_name")
    target, azione = payload.get("target_device"), payload.get("proposed_action")
    if tipo == "hitl_action_proposal_interrupt":
        descrizione = f"Il sistema propone di eseguire '{azione}' su '{target}'."
        motivo = f"Una persona deve approvare l'azione proposta dal nodo '{nodo}' prima che venga eseguita."
    elif tipo == "hitl_node_entry_interrupt":
        descrizione = f"Il nodo '{nodo}' sta per essere eseguito."
        motivo = "È richiesta l'approvazione di una persona prima di eseguire questo nodo."
    elif tipo == "llm_failure_human_intervention":
        errore = payload.get("errore") or {}
        descrizione = f"Il modello non è utilizzabile nel nodo '{nodo}'."
        motivo = str(errore.get("messaggio") or "Il modello linguistico non risponde: serve un intervento.")
    else:
        descrizione = f"Richiesta di approvazione ({tipo or 'sconosciuta'})."
        motivo = "È richiesta l'approvazione di una persona."
    return str(payload.get("description") or descrizione), str(payload.get("reason") or motivo)


async def _interrupt(grafo, thread_id: str) -> tuple[str | None, dict[str, Any]]:
    """(id, payload) dell'interrupt in attesa sul thread, oppure (None, {})."""
    istantanea = await grafo.aget_state({"configurable": {"thread_id": thread_id}})
    for task in istantanea.tasks:
        for interruzione in task.interrupts:
            valore = getattr(interruzione, "value", None)
            return str(getattr(interruzione, "id", None) or interruzione), valore if isinstance(valore, dict) else {}
    return None, {}


async def approvazione_in_attesa(grafo, gestore, thread_id: str) -> Approvazione | None:
    """L'approvazione pendente del thread, oppure None. `gestore` è il `GestoreTimerHitl` (dà la scadenza)."""
    approval_id, payload = await _interrupt(grafo, thread_id)
    if approval_id is None:
        return None
    descrizione, motivo = _testi(payload)
    stato_timer: dict[str, Any] = {}
    try:
        stato_timer = await gestore.stato(thread_id)
    except Exception as errore:  # la scadenza è un di più: l'approvazione c'è comunque
        logger.warning("[Approvazioni] Stato del timer non disponibile per '%s': %s", thread_id, errore)
    return Approvazione(
        approval_id=approval_id, thread_id=thread_id, tipo=str(payload.get("type", "")), node=payload.get("node_name"),
        target=payload.get("target_device"), azione=payload.get("proposed_action"), description=descrizione, reason=motivo,
        max_wait_seconds=payload.get("max_wait_seconds"), expires_at=stato_timer.get("scade_il"),
        expires_action=stato_timer.get("azione_alla_scadenza"))


async def _gia_decisa(approval_id: str, db_path: str | None) -> bool:
    async with archivio.motore(db_path).connect() as db:
        riga = (await db.execute(select(schema.events.c.event_id).where(schema.events.c.approval_id == approval_id).limit(1))).first()
    return riga is not None


async def decidi(grafo, gestore, thread_id: str, approval_id: str, decisione: str, motivo: str = "",
                 db_path: str | None = None) -> EsitoApprovazione:
    """Decide l'approvazione `approval_id` del thread: `approve` o `reject`. Non solleva per i casi previsti."""
    normalizzata = _DECISIONI.get((decisione or "").strip().lower())
    if normalizzata is None:
        return EsitoApprovazione(DECISIONE_NON_VALIDA, f"Decisione '{decisione}' non ammessa: usa 'approve' o 'reject'.",
                                 approval_id)
    nome = "approve" if normalizzata == "APPROVA" else "reject"
    try:
        async with gestore.blocco(thread_id):
            corrente, payload = await _interrupt(grafo, thread_id)
            if corrente != approval_id:
                if await _gia_decisa(approval_id, db_path):
                    return EsitoApprovazione(GIA_CHIUSA, "L'approvazione è già stata decisa.", approval_id)
                return EsitoApprovazione(NON_TROVATA, "Nessuna approvazione in attesa con questo identificativo.", approval_id)
            risultato = await grafo.ainvoke(
                Command(resume={"decision": normalizzata, "reasoning": motivo or f"Decisione '{nome}' via approvazione"}),
                config={"configurable": {"thread_id": thread_id}})
            messaggi = risultato.get("messages", []) if isinstance(risultato, dict) else []
            decision_id = await NervousSystem(db_path=db_path).log_event(
                None, f"APPROVAL_{'APPROVED' if normalizzata == 'APPROVA' else 'REJECTED'}",
                str(payload.get("target_device") or payload.get("node_name") or thread_id),
                "PENDING", "APPROVED" if normalizzata == "APPROVA" else "REJECTED",
                motivo or f"Decisione '{nome}'", outcome=_ESITO_DECISIONE[normalizzata], approval_id=approval_id)
            return EsitoApprovazione(DECISA, "Decisione applicata.", approval_id, nome, decision_id,
                                     messaggi[-1].content if messaggi else None)
    except Exception as errore:
        logger.exception("[Approvazioni] Decisione su '%s' non riuscita", approval_id)
        return EsitoApprovazione(ERRORE, f"Errore interno: {errore}", approval_id)
