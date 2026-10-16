"""Approvazioni: identificatore stabile, una sola pendente, approve/reject, già chiusa senza eccezioni, nessun OVERRIDE."""

import asyncio
import os
import tempfile
import unittest
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.core import approvazioni, archivio, contesto
from app.db.database import Database
from app.graph.timer_hitl import GestoreTimerHitl
from app.tools.nervous_system import NervousSystem


class Stato(TypedDict, total=False):
    esito: str


def costruisci_grafo():
    """Un nodo che propone un'azione e aspetta la decisione di una persona, poi un secondo nodo che ne propone un'altra."""

    def nodo(nome, target, azione):
        async def esegui(stato: Stato) -> Stato:
            risposta = interrupt({"type": "hitl_action_proposal_interrupt", "node_name": nome, "target_device": target,
                                  "proposed_action": azione, "max_wait_seconds": 300})
            decisione = risposta.get("decision") if isinstance(risposta, dict) else str(risposta)
            return {"esito": f"{stato.get('esito', '')}{nome}:{decisione};"}
        return esegui

    g = StateGraph(Stato)
    g.add_node("clima", nodo("clima", "ac_salotto", "ON"))
    g.add_node("luci", nodo("luci", "luce_cucina", "OFF"))
    g.add_edge(START, "clima")
    g.add_edge("clima", "luci")
    g.add_edge("luci", END)
    return g.compile(checkpointer=InMemorySaver())


class ApprovazioniTest(unittest.IsolatedAsyncioTestCase):
    thread = "ap01"

    async def asyncSetUp(self):
        self.db = os.path.join(tempfile.mkdtemp(prefix="lb_appr_"), "a.db")
        await Database(self.db).init_db()
        self.addAsyncCleanup(archivio.chiudi_motori)
        self.grafo = costruisci_grafo()
        self.gestore = GestoreTimerHitl(lambda: self.grafo, self.db)
        await self.gestore.assicura_tabella()
        await self.grafo.ainvoke({}, config={"configurable": {"thread_id": self.thread}})

    async def pendente(self):
        return await approvazioni.approvazione_in_attesa(self.grafo, self.gestore, self.thread)

    async def decidi(self, approval_id, decisione="approve", motivo="ok"):
        return await approvazioni.decidi(self.grafo, self.gestore, self.thread, approval_id, decisione, motivo, db_path=self.db)

    async def test_la_pendente_ha_id_stabile_e_testi_in_italiano(self):
        a = await self.pendente()
        self.assertTrue(a.approval_id)
        self.assertEqual((a.target, a.azione, a.node, a.max_wait_seconds), ("ac_salotto", "ON", "clima", 300))
        self.assertEqual(a.description, "Il sistema propone di eseguire 'ON' su 'ac_salotto'.")
        self.assertIn("approvare", a.reason)
        self.assertEqual((await self.pendente()).approval_id, a.approval_id)  # riletta, stesso id

    async def test_approve_riprende_il_grafo_e_registra_la_decisione(self):
        a = await self.pendente()
        with contesto.contesto("cid-ap", "user:anna@example.com"):
            esito = await self.decidi(a.approval_id, "approve", "va bene")
        self.assertEqual((esito.stato, esito.decisione, esito.approval_id), ("decisa", "approve", a.approval_id))
        eventi = [e for e in await NervousSystem(db_path=self.db).get_recent_events() if e["approval_id"] == a.approval_id]
        self.assertEqual(len(eventi), 1)
        d = eventi[0]
        self.assertEqual((d["event_id"], d["actor"], d["outcome"], d["correlation_id"], d["action"]),
                         (esito.decision_id, "user:anna@example.com", "applied", "cid-ap", "APPROVAL_APPROVED"))
        valori = (await self.grafo.aget_state({"configurable": {"thread_id": self.thread}})).values
        self.assertEqual(valori["esito"], "clima:APPROVA;")

    async def test_reject_registra_rejected(self):
        a = await self.pendente()
        esito = await self.decidi(a.approval_id, "reject")
        self.assertEqual(esito.stato, "decisa")
        d = next(e for e in await NervousSystem(db_path=self.db).get_recent_events() if e["approval_id"] == a.approval_id)
        self.assertEqual((d["outcome"], d["action"], d["new_value"]), ("rejected", "APPROVAL_REJECTED", "REJECTED"))
        valori = (await self.grafo.aget_state({"configurable": {"thread_id": self.thread}})).values
        self.assertEqual(valori["esito"], "clima:RESPINGI;")

    async def test_la_seconda_decisione_e_already_closed_e_non_cambia_nulla(self):
        a = await self.pendente()
        await self.decidi(a.approval_id, "approve")
        seconda = await self.decidi(a.approval_id, "reject", "ci ho ripensato")
        self.assertEqual((seconda.stato, seconda.decision_id), ("already_closed", None))
        valori = (await self.grafo.aget_state({"configurable": {"thread_id": self.thread}})).values
        self.assertEqual(valori["esito"], "clima:APPROVA;")  # la seconda non ha avuto effetto
        eventi = [e for e in await NervousSystem(db_path=self.db).get_recent_events() if e["approval_id"] == a.approval_id]
        self.assertEqual(len(eventi), 1)

    async def test_decisioni_concorrenti_una_sola_vince(self):
        a = await self.pendente()
        esiti = await asyncio.gather(*[self.decidi(a.approval_id, "approve") for _ in range(4)])
        self.assertEqual(sorted(e.stato for e in esiti), ["already_closed"] * 3 + ["decisa"])

    async def test_dopo_la_decisione_arriva_la_prossima_pendente_con_un_altro_id(self):
        prima = await self.pendente()
        await self.decidi(prima.approval_id, "approve")
        dopo = await self.pendente()
        self.assertEqual((dopo.node, dopo.target), ("luci", "luce_cucina"))
        self.assertNotEqual(dopo.approval_id, prima.approval_id)
        # la decisione sulla vecchia non tocca la nuova
        self.assertEqual((await self.decidi(prima.approval_id, "approve")).stato, "already_closed")
        self.assertEqual((await self.pendente()).approval_id, dopo.approval_id)

    async def test_override_e_decisioni_sconosciute_sono_rifiutate_e_la_richiesta_resta_aperta(self):
        a = await self.pendente()
        for decisione in ("OVERRIDE", "override", "retry", "boh", "", None):
            esito = await self.decidi(a.approval_id, decisione)
            self.assertEqual(esito.stato, "decisione_non_valida", decisione)
        self.assertEqual((await self.pendente()).approval_id, a.approval_id)

    async def test_id_sconosciuto_e_thread_senza_richieste(self):
        esito = await self.decidi("id-che-non-esiste")
        self.assertEqual(esito.stato, "non_trovata")
        a = await self.pendente()
        await self.decidi(a.approval_id)
        b = await self.pendente()
        await self.decidi(b.approval_id)
        self.assertIsNone(await self.pendente())
        self.assertEqual((await self.decidi(b.approval_id)).stato, "already_closed")

    async def test_le_parole_italiane_valgono_come_quelle_inglesi(self):
        a = await self.pendente()
        self.assertEqual((await self.decidi(a.approval_id, "APPROVA")).decisione, "approve")


if __name__ == "__main__":
    unittest.main()
