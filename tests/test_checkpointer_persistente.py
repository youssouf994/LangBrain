import os
from sqlite_chiusura import connetti
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.checkpointer import apri_checkpointer, chiudi_checkpointer, get_checkpointer, svuota_checkpoint
from app.graph.builder import build_graph
from app.graph.hitl_config import hitl_manager


def stato_iniziale():
    return {
        "messages": [],
        "readings": [],
        "recent_events": [],
        "pending_escalations": [],
        "next_agent": "brain",
        "hitl_required": False,
        "config": {},
    }


def configura_hitl(nodi):
    hitl_manager.update_config(hitl_all=False, hitl_nodes=nodi, hitl_targets=[], hitl_actions=[], max_wait_seconds=None)


class CheckpointerPersistenteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._cartella = tempfile.TemporaryDirectory()
        self.percorso_db = os.path.join(self._cartella.name, "checkpoint.db")
        self.addCleanup(self._cartella.cleanup)
        self.addAsyncCleanup(chiudi_checkpointer)
        self.addCleanup(configura_hitl, [])

        # Il nodo Brain interrotto dall'HITL non deve leggere né scrivere il DB applicativo reale.
        eventi = patch("app.tools.event_log.EventLog.get_recent_events", AsyncMock(return_value=[]))
        eventi.start()
        self.addCleanup(eventi.stop)

        configura_hitl(["brain"])
        self.config = {"configurable": {"thread_id": "thread-persistente"}}

    async def test_interrupt_pendente_sopravvive_al_riavvio(self):
        grafo, _ = build_graph(custom_agent_instances={}, checkpointer=await apri_checkpointer(self.percorso_db))
        await grafo.ainvoke(stato_iniziale(), config=self.config)
        prima = await grafo.aget_state(self.config)
        self.assertEqual(prima.next, ("brain",))

        # Riavvio: la connessione viene chiusa e riaperta sullo stesso file.
        await chiudi_checkpointer()
        self.assertIsNone(get_checkpointer())
        grafo_riavviato, _ = build_graph(custom_agent_instances={}, checkpointer=await apri_checkpointer(self.percorso_db))

        dopo = await grafo_riavviato.aget_state(self.config)
        self.assertEqual(dopo.next, ("brain",))
        self.assertEqual(len(dopo.tasks[0].interrupts), 1)

        risultato = await grafo_riavviato.ainvoke(
            Command(resume={"decision": "APPROVA", "reasoning": "ok dopo il riavvio"}), config=self.config
        )
        self.assertEqual(risultato["next_agent"], "END")
        self.assertEqual((await grafo_riavviato.aget_state(self.config)).next, ())

    async def test_la_ricompilazione_del_grafo_conserva_i_thread(self):
        saver = await apri_checkpointer(self.percorso_db)
        grafo, _ = build_graph(custom_agent_instances={}, checkpointer=saver)
        await grafo.ainvoke(stato_iniziale(), config=self.config)

        ricompilato, _ = build_graph(custom_agent_instances={}, checkpointer=saver)

        self.assertEqual((await ricompilato.aget_state(self.config)).next, ("brain",))

    async def test_svuota_checkpoint_elimina_i_thread(self):
        saver = await apri_checkpointer(self.percorso_db)
        grafo, _ = build_graph(custom_agent_instances={}, checkpointer=saver)
        await grafo.ainvoke(stato_iniziale(), config=self.config)

        await svuota_checkpoint()

        self.assertEqual((await grafo.aget_state(self.config)).next, ())

    async def test_tabelle_create_nello_stesso_file_del_db_applicativo(self):
        await apri_checkpointer(self.percorso_db)
        await chiudi_checkpointer()

        with connetti(self.percorso_db) as db:
            tabelle = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"checkpoints", "writes"} <= tabelle)

    async def test_apertura_ripetuta_restituisce_la_stessa_istanza(self):
        primo = await apri_checkpointer(self.percorso_db)
        self.assertIs(await apri_checkpointer(self.percorso_db), primo)

    async def test_apertura_su_un_altro_file_sostituisce_l_istanza(self):
        primo = await apri_checkpointer(self.percorso_db)
        secondo = await apri_checkpointer(os.path.join(self._cartella.name, "altro.db"))
        self.assertIsNot(secondo, primo)
        self.assertIs(get_checkpointer(), secondo)

    async def test_chiusura_senza_apertura_non_solleva_errori(self):
        await chiudi_checkpointer()
        await chiudi_checkpointer()

    def test_senza_checkpointer_il_grafo_usa_uno_stato_volatile(self):
        grafo, _ = build_graph(custom_agent_instances={})
        self.assertIsInstance(grafo.checkpointer, MemorySaver)


if __name__ == "__main__":
    unittest.main()
