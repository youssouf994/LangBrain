"""
Sistema nervoso del dominio energia: collega la rete simulata alla gerarchia di agenti.

- Via afferente: osserva le anomalie della simulazione. Quando compare un'anomalia nuova del dominio degli agenti
  (o quando tutto è rientrato e qualche leva è rimasta accesa) registra uno stimolo nell'audit log ed esegue un ciclo
  del grafo. In automatico solo se l'operatore lo accende, con un intervallo minimo tra un ciclo e l'altro e un numero
  massimo di cicli automatici: ogni ciclo usa il modello LLM reale e costa.
- Via efferente: gli agenti comandano la rete con le leve (app.tools.strumenti_energia).
- Mentre il grafo ragiona, e finché attende l'operatore, la simulazione è ferma: il mondo aspetta la decisione.

Il grafo del dominio energia è separato da quello della smart home dell'API: ha i propri agenti, i propri tool e un
Brain con i prompt del dominio, e usa lo stesso checkpointer (thread "energia-...").
"""

import asyncio
import logging
import time
import uuid
from collections import deque
from typing import Any

from app.core.errori_llm import ErroreLLM, oscura_segreti

logger = logging.getLogger(__name__)

INTERVALLO_MINIMO_PREDEFINITO_S = 20.0
CICLI_AUTOMATICI_PREDEFINITI = 5


class SistemaNervoso:
    def __init__(self) -> None:
        self.grafo = None
        self.strumenti: dict[str, Any] = {}
        self.automatico = False
        self.intervallo_minimo_s = INTERVALLO_MINIMO_PREDEFINITO_S
        self.cicli_automatici_massimi = CICLI_AUTOMATICI_PREDEFINITI
        self.cicli_automatici = 0
        self.cicli = 0
        self.stato = "pronto"               # pronto | in_esecuzione | in_attesa_operatore
        self.thread_id: str | None = None
        self.richiesta: Any = None          # contenuto dell'interrupt HITL in attesa
        self.nodo_attivo: str | None = None
        self.nodi_visitati: list[str] = []
        self.ultimo_stimolo: dict[str, Any] | None = None
        self.passi: deque[dict[str, Any]] = deque(maxlen=300)
        self._firma_gestita: frozenset[str] = frozenset()
        self._ultimo_ciclo = 0.0
        self._riprendere_simulazione = False
        self._compito: asyncio.Task | None = None
        self._sorveglianza: asyncio.Task | None = None

    # ------------------------------------------------------------------------------------------ grafo

    def prepara(self):
        """Compila (una volta) il grafo del dominio energia con i suoi agenti e tool."""
        if self.grafo is not None:
            return self.grafo
        from langgraph.checkpoint.memory import MemorySaver

        from app.agents.agenti_energia import (
            BRAIN_SYSTEM_PROMPT_ENERGIA, BRAIN_USER_PROMPT_ENERGIA, TARGET_ENERGIA, crea_agenti_energia,
        )
        from app.checkpointer import get_checkpointer
        from app.graph.builder import build_graph
        from app.tools.strumenti_energia import registra_strumenti_energia

        self.strumenti = registra_strumenti_energia()
        self.grafo, _ = build_graph(
            custom_agent_instances=crea_agenti_energia(self.strumenti),
            checkpointer=get_checkpointer() or MemorySaver(),
            tools=self.strumenti,
            brain_options={
                "managed_targets": list(TARGET_ENERGIA),
                "system_prompt": BRAIN_SYSTEM_PROMPT_ENERGIA,
                "user_prompt_template": BRAIN_USER_PROMPT_ENERGIA,
            },
        )
        return self.grafo

    @staticmethod
    def albero() -> list[dict[str, Any]]:
        from app.agents.agenti_energia import GERARCHIA_ENERGIA, LEVE_CRITICHE
        nodi = [{"nome": "brain", "padre": None, "livello": 0, "priorita": 1000.0, "leva": None, "critica": False}]
        nodi += [{"nome": r.nome, "padre": "brain" if r.padre == "Brain" else r.padre, "livello": r.livello,
                  "priorita": r.priorita, "leva": r.leva, "critica": r.leva in LEVE_CRITICHE} for r in GERARCHIA_ENERGIA]
        return nodi

    # ------------------------------------------------------------------------------------------ stimoli

    @staticmethod
    def _anomalie_rilevanti(sim) -> list[dict[str, Any]]:
        from app.agents.agenti_energia import GERARCHIA_ENERGIA
        tipi = set().union(*(r.anomalie for r in GERARCHIA_ENERGIA))
        return [a for a in sim.anomalie if a["tipo"] in tipi]

    def stimolo(self, sim) -> str | None:
        """Motivo per svegliare il grafo, oppure None se non c'è niente di nuovo."""
        anomalie = self._anomalie_rilevanti(sim)
        firma = frozenset(a["id"] for a in anomalie)
        nuove = [a for a in anomalie if a["id"] not in self._firma_gestita]
        if nuove:
            return "Nuove anomalie: " + "; ".join(a["descrizione"] for a in nuove[:6])
        accese = [nome for nome, valore in sim.leve.items() if valore == "ON"]
        if not firma and self._firma_gestita and accese:
            return "Anomalie rientrate con leve ancora accese: " + ", ".join(accese)
        return None

    # ------------------------------------------------------------------------------------------ comandi

    def configura(self, automatico: bool | None = None, intervallo_minimo_s: float | None = None,
                  cicli_automatici_massimi: int | None = None, azzera_contatore: bool = False) -> None:
        if intervallo_minimo_s is not None:
            if not 1 <= intervallo_minimo_s <= 3600:
                raise ValueError("L'intervallo minimo deve essere tra 1 e 3600 secondi.")
            self.intervallo_minimo_s = float(intervallo_minimo_s)
        if cicli_automatici_massimi is not None:
            if not 0 <= cicli_automatici_massimi <= 1000:
                raise ValueError("Il numero massimo di cicli automatici deve essere tra 0 e 1000.")
            self.cicli_automatici_massimi = int(cicli_automatici_massimi)
        if azzera_contatore:
            self.cicli_automatici = 0
        if automatico is not None:
            self.automatico = bool(automatico)
        self._notifica()

    def avvia_ciclo(self, motivo: str, automatico: bool = False) -> str:
        """Esegue un ciclo del grafo in background. Solleva ValueError se ne è già in corso uno o si attende l'operatore."""
        from app.simulazione.esecutore import esecutore
        if self.stato != "pronto":
            raise ValueError("Il grafo sta già lavorando o attende la decisione dell'operatore.")
        from app.agents.agenti_energia import LEVE_CRITICHE
        grafo = self.prepara()
        sim = esecutore.simulatore()
        self._riprendere_simulazione = esecutore.stato == "in_corso"
        esecutore.pausa()
        self.cicli += 1
        if automatico:
            self.cicli_automatici += 1
        self._ultimo_ciclo = time.monotonic()
        self._firma_gestita = frozenset(a["id"] for a in self._anomalie_rilevanti(sim))
        self.thread_id = f"energia-{uuid.uuid4().hex[:10]}"
        self.ultimo_stimolo = {"ciclo": self.cicli, "motivo": motivo, "automatico": automatico,
                               "t_simulato": sim.tempo.isoformat(timespec="minutes")}
        self.nodi_visitati = []
        self._passo("stimolo", None, motivo)
        ingresso = {
            "messages": [], "readings": [], "recent_events": [], "pending_escalations": [], "next_agent": "brain",
            "hitl_required": False, "config": {"target_critici_brain": list(LEVE_CRITICHE), "stimolo": motivo},
        }
        self.stato = "in_esecuzione"
        self._compito = asyncio.get_running_loop().create_task(self._esegui(grafo, ingresso, stimolo=motivo))
        return self.thread_id

    def decidi(self, decisione: str, motivazione: str = "") -> None:
        """Risposta dell'operatore alla richiesta HITL in attesa (APPROVA, RESPINGI, OVERRIDE...)."""
        from langgraph.types import Command
        if self.stato != "in_attesa_operatore" or self.grafo is None:
            raise ValueError("Nessuna richiesta dell'operatore in attesa.")
        self._passo("operatore", None, f"Decisione dell'operatore: {decisione}. {motivazione}".strip())
        comando = Command(resume={"decision": decisione, "reasoning": motivazione or f"Decisione {decisione} dalla pagina /energia"})
        self.richiesta = None
        self.stato = "in_esecuzione"
        self._compito = asyncio.get_running_loop().create_task(self._esegui(self.grafo, comando))

    async def _esegui(self, grafo, ingresso: Any, stimolo: str | None = None) -> None:
        from app.simulazione.esecutore import esecutore
        from app.tools.event_log import EventLog
        config = {"configurable": {"thread_id": self.thread_id}}
        self.stato = "in_esecuzione"
        self.nodo_attivo = "brain" if stimolo else None
        self._notifica()
        if stimolo:
            try:
                await EventLog().log_event(actor="sistema_nervoso", action="STIMOLO", target="rete_energetica",
                                           old_value="", new_value=f"ciclo {self.cicli}", reasoning=stimolo[:1000])
            except Exception as e:
                logger.warning("[Sistema nervoso] Stimolo non registrato nell'audit log: %s", e)
        try:
            async for blocco in grafo.astream(ingresso, config=config, stream_mode="updates"):
                for nodo, aggiornamento in blocco.items():
                    if nodo == "__interrupt__":
                        for interruzione in aggiornamento:
                            self.richiesta = getattr(interruzione, "value", None)
                            self._passo("pausa", self.nodo_attivo, (self.richiesta or {}).get("prompt") if isinstance(self.richiesta, dict) else str(self.richiesta))
                        continue
                    dati = aggiornamento if isinstance(aggiornamento, dict) else {}
                    messaggi = dati.get("messages") or []
                    testo = getattr(messaggi[-1], "content", None) if messaggi else None
                    if nodo not in self.nodi_visitati:
                        self.nodi_visitati.append(nodo)
                    prossimo = dati.get("next_agent")
                    self.nodo_attivo = None if str(prossimo).upper() == "END" else prossimo
                    self._passo("nodo", nodo, str(testo)[:1500] if testo else None, prossimo=prossimo)
        except asyncio.CancelledError:
            raise
        except ErroreLLM as errore:
            self._passo("errore", self.nodo_attivo, f"{errore.codice}: {errore.messaggio} {errore.suggerimento}")
        except Exception as errore:
            logger.exception("[Sistema nervoso] Errore durante il ciclo del grafo.")
            self._passo("errore", self.nodo_attivo, oscura_segreti(str(errore))[:500])
        try:
            istantanea = await grafo.aget_state(config)
            in_pausa = any(t.interrupts for t in istantanea.tasks)
        except Exception:
            in_pausa = False
        if in_pausa:
            self.stato = "in_attesa_operatore"
            if self.richiesta is None:
                self.richiesta = next((i.value for t in istantanea.tasks for i in t.interrupts), None)
        else:
            self.stato = "pronto"
            self.nodo_attivo = None
            self._passo("fine", None, "Ciclo completato.")
            if self._riprendere_simulazione and not esecutore.simulatore().finita:
                esecutore.avvia()
        self._notifica()

    # ------------------------------------------------------------------------------------------ sorveglianza

    async def sorveglia(self) -> None:
        """Ciclo di fondo della via afferente: in automatico sveglia il grafo quando serve."""
        from app.simulazione.esecutore import esecutore
        while True:
            await asyncio.sleep(0.5)
            try:
                if not self.automatico or self.stato != "pronto" or esecutore.sim is None:
                    continue
                if self.cicli_automatici >= self.cicli_automatici_massimi:
                    continue
                if time.monotonic() - self._ultimo_ciclo < self.intervallo_minimo_s:
                    continue
                motivo = self.stimolo(esecutore.sim)
                if motivo:
                    self.avvia_ciclo(motivo, automatico=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Sistema nervoso] Errore nella sorveglianza.")

    def avvia_sorveglianza(self) -> None:
        if self._sorveglianza is None or self._sorveglianza.done():
            self._sorveglianza = asyncio.get_running_loop().create_task(self.sorveglia())

    async def chiudi(self) -> None:
        for compito in (self._sorveglianza, self._compito):
            if compito is not None and not compito.done():
                compito.cancel()
                try:
                    await compito
                except (asyncio.CancelledError, Exception):
                    pass
        self._sorveglianza = self._compito = None
        if self.stato == "in_esecuzione":
            self.stato = "pronto"

    # ------------------------------------------------------------------------------------------ viste

    def _passo(self, tipo: str, nodo: str | None, testo: str | None, **extra: Any) -> None:
        from app.simulazione.esecutore import esecutore
        sim = esecutore.sim
        self.passi.append({
            "ciclo": self.cicli, "tipo": tipo, "nodo": nodo, "testo": testo,
            "t_simulato": sim.tempo.isoformat(timespec="minutes") if sim else None,
            "t_reale": time.strftime("%H:%M:%S"), **extra,
        })
        self._notifica()

    @staticmethod
    def _notifica() -> None:
        from app.simulazione.esecutore import esecutore
        esecutore.versione += 1

    def stato_pubblico(self, passi: int = 60) -> dict[str, Any]:
        from app.simulazione.esecutore import esecutore
        sim = esecutore.sim
        return {
            "stato": self.stato, "automatico": self.automatico, "intervallo_minimo_s": self.intervallo_minimo_s,
            "cicli": self.cicli, "cicli_automatici": self.cicli_automatici,
            "cicli_automatici_massimi": self.cicli_automatici_massimi, "thread_id": self.thread_id,
            "nodo_attivo": self.nodo_attivo, "nodi_visitati": self.nodi_visitati, "richiesta": self.richiesta,
            "ultimo_stimolo": self.ultimo_stimolo, "stimolo_in_attesa": self.stimolo(sim) if sim else None,
            "passi": list(self.passi)[-passi:],
        }


sistema_nervoso = SistemaNervoso()
