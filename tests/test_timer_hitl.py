import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from langchain_core.messages import AIMessage
from langgraph.types import Command

from app.agents.base_agent import BaseAgent
from app.core.configurazione import costruisci_configurazione, imposta_configurazione
from app.core.errori_llm import ErroreLLM
from app.graph import timer_hitl
from app.graph.builder import build_graph
from app.graph.hitl_config import hitl_manager
from app.graph.timer_hitl import GestoreTimerHitl, attesa_massima_secondi, durata_timer_secondi


class Orologio:
    def __init__(self):
        self.ora = 1_000_000.0

    def __call__(self):
        return self.ora

    def avanza(self, secondi):
        self.ora += secondi


class AgenteConLlmInguasto(BaseAgent):
    async def process(self, state, recent_events, relevant_readings, agent_escalations):
        raise ErroreLLM("CREDITI_ESAURITI", "crediti insufficienti")


def configura(test, **hitl):
    dati = {"hitl": {"timer_attivo": 1, "timer_predefinito_secondi": 60, "azione_alla_scadenza": "respingi", **hitl}}
    imposta_configurazione(costruisci_configurazione(dati))
    test.addCleanup(imposta_configurazione, None)


class BaseTimerTest(unittest.IsolatedAsyncioTestCase):
    thread = "thread-timer"

    async def asyncSetUp(self):
        self._cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self._cartella.cleanup)
        self.db = os.path.join(self._cartella.name, "timer.db")
        self.orologio = Orologio()
        self.grafo = None
        self.gestore = GestoreTimerHitl(lambda: self.grafo, self.db, self.orologio)
        await self.gestore.assicura_tabella()

        hitl_manager.update_config(hitl_all=False, hitl_nodes=["brain"], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)
        self.addCleanup(hitl_manager.update_config, hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)
        self.enterContext(patch("app.tools.event_log.EventLog.get_recent_events", AsyncMock(return_value=[])))
        self.config = {"configurable": {"thread_id": self.thread}}

    def costruisci_grafo(self, agenti=None, prossimo="brain"):
        self.grafo, _ = build_graph(custom_agent_instances=agenti or {})
        self.stato_iniziale = {
            "messages": [], "readings": [], "recent_events": [], "pending_escalations": [],
            "next_agent": prossimo, "hitl_required": False, "config": {},
        }

    async def avvia(self):
        await self.grafo.ainvoke(self.stato_iniziale, config=self.config)

    async def in_pausa(self):
        istantanea = await self.grafo.aget_state(self.config)
        return any(t.interrupts for t in istantanea.tasks)

    async def ultimo_messaggio(self):
        return (await self.grafo.aget_state(self.config)).values["messages"][-1].content


class TimerDisattivatoTest(BaseTimerTest):
    async def test_senza_timer_nessuna_scadenza_e_il_grafo_attende_senza_limite(self):
        imposta_configurazione(costruisci_configurazione({"hitl": {"timer_attivo": 0}}))
        self.addCleanup(imposta_configurazione, None)
        self.costruisci_grafo()
        await self.avvia()

        stato = await self.gestore.stato(self.thread)
        self.orologio.avanza(10_000)
        respinti = await self.gestore.processa_scadute()

        self.assertEqual((stato["attivo"], stato["in_pausa"], stato["secondi_rimanenti"]), (False, True, None))
        self.assertEqual(respinti, [])
        self.assertTrue(await self.in_pausa())
        self.assertEqual(await self.gestore.elenco(), [])
        self.assertIsNone(durata_timer_secondi())


class TimerAttivoTest(BaseTimerTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        configura(self)
        self.costruisci_grafo()

    async def test_la_scadenza_parte_alla_pausa_e_il_tempo_rimanente_scende(self):
        await self.avvia()

        iniziale = await self.gestore.stato(self.thread)
        self.orologio.avanza(25)
        dopo = await self.gestore.stato(self.thread)

        self.assertEqual((iniziale["attivo"], iniziale["in_pausa"], iniziale["secondi_totali"], iniziale["secondi_rimanenti"]), (True, True, 60, 60))
        self.assertEqual((dopo["secondi_rimanenti"], dopo["scaduto"], dopo["azione_alla_scadenza"]), (35, False, "respingi"))
        self.assertEqual(dopo["tipo"], "hitl_node_entry_interrupt")
        self.assertTrue(dopo["scade_il"].endswith("+00:00"))

    async def test_riletture_ripetute_non_azzerano_il_timer(self):
        await self.avvia()
        await self.gestore.stato(self.thread)
        self.orologio.avanza(50)
        for _ in range(3):
            stato = await self.gestore.stato(self.thread)
        self.assertEqual(stato["secondi_rimanenti"], 10)

    async def test_la_scadenza_sopravvive_a_un_riavvio(self):
        await self.avvia()
        await self.gestore.stato(self.thread)
        self.orologio.avanza(20)

        riavviato = GestoreTimerHitl(lambda: self.grafo, self.db, self.orologio)

        self.assertEqual((await riavviato.stato(self.thread))["secondi_rimanenti"], 40)

    async def test_alla_scadenza_la_richiesta_viene_respinta_senza_azioni(self):
        await self.avvia()
        await self.gestore.stato(self.thread)
        self.orologio.avanza(61)

        respinti = await self.gestore.processa_scadute()

        self.assertEqual(respinti, [self.thread])
        self.assertFalse(await self.in_pausa())
        messaggio = await self.ultimo_messaggio()
        self.assertIn("annullato", messaggio)
        self.assertIn("Timer HITL scaduto", messaggio)
        self.assertEqual(await self.gestore.elenco(), [])

    async def test_prima_della_scadenza_non_succede_nulla(self):
        await self.avvia()
        await self.gestore.stato(self.thread)
        self.orologio.avanza(59)

        self.assertEqual(await self.gestore.processa_scadute(), [])
        self.assertTrue(await self.in_pausa())

    async def test_se_l_operatore_ha_gia_risposto_la_scadenza_non_fa_nulla(self):
        await self.avvia()
        await self.gestore.stato(self.thread)
        await self.grafo.ainvoke(Command(resume={"decision": "APPROVA", "reasoning": "ok"}), config=self.config)
        self.orologio.avanza(1000)

        self.assertEqual(await self.gestore.processa_scadute(), [])
        self.assertFalse(await self.in_pausa())
        self.assertEqual((await self.gestore.stato(self.thread))["in_pausa"], False)
        self.assertEqual(await self.gestore.elenco(), [])

    async def test_un_nuovo_interrupt_sullo_stesso_thread_ha_un_nuovo_timer(self):
        await self.avvia()
        await self.gestore.stato(self.thread)
        self.orologio.avanza(50)
        await self.grafo.ainvoke(Command(resume={"decision": "APPROVA"}), config=self.config)
        await self.gestore.stato(self.thread)

        self.hitl_ancora_su_brain = True
        await self.avvia()
        nuovo = await self.gestore.stato(self.thread)

        self.assertEqual(nuovo["secondi_rimanenti"], 60)

    async def test_la_durata_impostata_via_api_sostituisce_il_predefinito(self):
        hitl_manager.update_config(max_wait_seconds=10)
        await self.avvia()

        stato = await self.gestore.stato(self.thread)

        self.assertEqual((stato["secondi_totali"], durata_timer_secondi(), attesa_massima_secondi()), (10, 10, 10))

    async def test_il_payload_dell_interrupt_riporta_la_durata_del_timer(self):
        await self.avvia()
        payload = (await self.grafo.aget_state(self.config)).tasks[0].interrupts[0].value
        self.assertEqual(payload["max_wait_seconds"], 60)
        self.assertIn("60", payload["prompt"])

    async def test_l_elenco_ordina_le_richieste_dalla_piu_urgente(self):
        for nome, secondi_prima in (("thread-a", 0), ("thread-b", 30)):
            self.orologio.avanza(secondi_prima)
            self.config = {"configurable": {"thread_id": nome}}
            await self.avvia()
            await self.gestore.stato(nome)

        elenco = await self.gestore.elenco()

        self.assertEqual([r["thread_id"] for r in elenco], ["thread-a", "thread-b"])
        self.assertEqual([r["secondi_rimanenti"] for r in elenco], [30, 60])

    async def test_ogni_tipo_di_pausa_ha_il_suo_timer_anche_l_llm_non_utilizzabile(self):
        self.costruisci_grafo({"agente_llm": AgenteConLlmInguasto("agente_llm", ["lampada"], 30, 1.0)}, prossimo="agente_llm")
        hitl_manager.update_config(hitl_nodes=[])
        await self.avvia()

        stato = await self.gestore.stato(self.thread)
        self.orologio.avanza(61)
        respinti = await self.gestore.processa_scadute()

        self.assertEqual(stato["tipo"], "llm_failure_human_intervention")
        self.assertEqual(respinti, [self.thread])
        self.assertFalse(await self.in_pausa())
        self.assertIn("annullato dall'operatore", await self.ultimo_messaggio())

    async def test_il_ciclo_di_sorveglianza_respinge_da_solo_alla_scadenza(self):
        await self.avvia()
        await self.gestore.stato(self.thread)
        self.orologio.avanza(61)

        with patch.object(timer_hitl, "INTERVALLO_CONTROLLO_SECONDI", 0.01):
            compito = asyncio.create_task(self.gestore.sorveglia())
            for _ in range(100):
                await asyncio.sleep(0.02)
                # Il ciclo ha finito quando il grafo è ripreso E la scadenza è stata registrata e tolta (ultimo passo): cancellarlo
                # prima lascerebbe una scrittura a metà e, su Windows, il file SQLite aperto alla pulizia della cartella.
                if not await self.in_pausa() and not await self.gestore.elenco():
                    break
            compito.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await compito

        self.assertFalse(await self.in_pausa())


class AzioneUmanoTest(BaseTimerTest):
    async def test_con_azione_umano_il_grafo_resta_in_pausa_e_la_scadenza_e_segnalata(self):
        configura(self, azione_alla_scadenza="umano")
        self.costruisci_grafo()
        await self.avvia()
        await self.gestore.stato(self.thread)
        self.orologio.avanza(61)

        respinti = await self.gestore.processa_scadute()
        stato = await self.gestore.stato(self.thread)
        ripetuto = await self.gestore.processa_scadute()

        self.assertEqual((respinti, ripetuto), ([], []))
        self.assertTrue(await self.in_pausa())
        self.assertEqual((stato["scaduto"], stato["secondi_rimanenti"], stato["stato_timer"]), (True, 0, "scaduta_in_attesa"))

        await self.grafo.ainvoke(Command(resume={"decision": "APPROVA"}), config=self.config)
        self.assertFalse((await self.gestore.stato(self.thread))["in_pausa"])


class EndpointTimerTest(BaseTimerTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        configura(self)
        self.costruisci_grafo()
        self.enterContext(patch("app.api.main.gestore_timer", self.gestore))
        self.enterContext(patch("app.api.main._graph", self.grafo))

    async def test_lo_stato_del_grafo_riporta_il_tempo_rimanente(self):
        from app.api.main import get_graph_state

        await self.avvia()
        await self.gestore.stato(self.thread)
        self.orologio.avanza(15)

        risposta = await get_graph_state(self.thread)

        self.assertTrue(risposta["is_interrupted"])
        self.assertEqual((risposta["hitl_timer"]["secondi_rimanenti"], risposta["hitl_timer"]["secondi_totali"]), (45, 60))

    async def test_lo_stato_senza_pausa_non_ha_timer(self):
        from app.api.main import get_graph_state

        risposta = await get_graph_state("thread-mai-avviato")

        self.assertFalse(risposta["is_interrupted"])
        self.assertEqual((risposta["hitl_timer"]["in_pausa"], risposta["hitl_timer"]["secondi_rimanenti"]), (False, None))

    async def test_l_elenco_delle_scadenze(self):
        from app.api.main import get_hitl_deadlines

        await self.avvia()
        await self.gestore.stato(self.thread)

        risposta = await get_hitl_deadlines()

        self.assertTrue(risposta["attivo"])
        self.assertEqual([r["thread_id"] for r in risposta["richieste"]], [self.thread])

    async def test_la_ripresa_dell_operatore_chiude_il_timer_e_riporta_lo_stato(self):
        from app.api.main import HitlResumeRequest, resume_graph

        await self.avvia()
        await self.gestore.stato(self.thread)

        risposta = await resume_graph(HitlResumeRequest(decision="RESPINGI", thread_id=self.thread))

        self.assertFalse(risposta["hitl_timer"]["in_pausa"])
        self.assertEqual(await self.gestore.elenco(), [])

    async def test_la_ripresa_senza_richiesta_in_attesa_risponde_409(self):
        from app.api.main import HitlResumeRequest, resume_graph

        await self.avvia()
        await self.gestore.stato(self.thread)
        self.orologio.avanza(61)
        await self.gestore.processa_scadute()  # scaduta e respinta in automatico

        with self.assertRaises(HTTPException) as ctx:
            await resume_graph(HitlResumeRequest(decision="APPROVA", thread_id=self.thread))

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("scaduta", ctx.exception.detail)

    async def test_un_ciclo_che_si_ferma_riporta_subito_il_timer(self):
        from app.api.main import RunCycleRequest, run_graph_cycle

        risposta = await run_graph_cycle(RunCycleRequest(force_next_agent="brain", thread_id=self.thread))

        self.assertEqual(risposta["hitl_timer"]["secondi_rimanenti"], 60)


class ConfigurazioneApiHitlTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(hitl_manager.update_config, max_wait_seconds=None)

    async def test_un_null_esplicito_azzera_l_attesa_massima(self):
        from app.api.main import update_hitl_config
        from app.graph.hitl_config import HitlConfigSchema

        hitl_manager.update_config(max_wait_seconds=30)
        await update_hitl_config(HitlConfigSchema(max_wait_seconds=None))
        self.assertIsNone(hitl_manager.get_config().max_wait_seconds)

    async def test_un_campo_omesso_lascia_invariata_l_attesa_massima(self):
        from app.api.main import update_hitl_config
        from app.graph.hitl_config import HitlConfigSchema

        hitl_manager.update_config(max_wait_seconds=30)
        await update_hitl_config(HitlConfigSchema(hitl_all=False))
        self.assertEqual(hitl_manager.get_config().max_wait_seconds, 30)

    def test_update_config_distingue_non_indicato_da_azzerato(self):
        hitl_manager.update_config(max_wait_seconds=45)
        hitl_manager.update_config(hitl_all=False)
        self.assertEqual(hitl_manager.get_config().max_wait_seconds, 45)
        hitl_manager.update_config(max_wait_seconds=None)
        self.assertIsNone(hitl_manager.get_config().max_wait_seconds)


class ConfigurazioneTimerTest(unittest.TestCase):
    def test_la_voce_e_un_interruttore_0_1(self):
        from app.core.configurazione import ErroreConfigurazione

        self.assertTrue(costruisci_configurazione({"hitl": {"timer_attivo": 1}}).hitl_timer_attivo)
        self.assertFalse(costruisci_configurazione({"hitl": {"timer_attivo": 0}}).hitl_timer_attivo)
        self.assertTrue(costruisci_configurazione({"hitl": {"timer_attivo": True}}).hitl_timer_attivo)
        for non_valido in (2, "si", -1, 0.5):
            with self.subTest(valore=non_valido), self.assertRaises(ErroreConfigurazione):
                costruisci_configurazione({"hitl": {"timer_attivo": non_valido}})

    def test_valori_predefiniti_e_validazione_di_durata_e_azione(self):
        from app.core.configurazione import ErroreConfigurazione

        predefinita = costruisci_configurazione({})
        self.assertEqual((predefinita.hitl_timer_attivo, predefinita.hitl_timer_secondi, predefinita.hitl_azione_alla_scadenza), (False, 300, "umano"))
        for dati in ({"hitl": {"timer_predefinito_secondi": 0}}, {"hitl": {"timer_predefinito_secondi": "60"}},
                     {"hitl": {"azione_alla_scadenza": "approva"}}):
            with self.subTest(dati=dati), self.assertRaises(ErroreConfigurazione):
                costruisci_configurazione(dati)

    def test_il_conflitto_demo_all_avvio_e_spento_di_default(self):
        self.assertFalse(costruisci_configurazione({}).demo_conflitto_all_avvio)
        self.assertTrue(costruisci_configurazione({"demo": {"conflitto_all_avvio": 1}}).demo_conflitto_all_avvio)


if __name__ == "__main__":
    unittest.main()
