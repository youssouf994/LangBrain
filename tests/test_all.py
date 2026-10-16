"""
Regressione generale di LangBrain.

Ogni controllo è un test pytest a sé, isolato dagli altri: database SQLite temporaneo con lo schema creato,
registro dei tool ripulito a ogni test, nessuna chiamata reale a un LLM. Una prova con un modello vero è
disponibile ma disattivata: `RUN_LLM_TESTS=1 python -m pytest tests/test_all.py -k reale`.

Le aree sono le stesse dello script precedente (utilità, database, tool IoT, MAO, agenti, builder, API, TTL, HITL,
gerarchia, agenti medici, override); le funzionalità più recenti hanno i propri file di test.
"""

import asyncio
import json
import os
from sqlite_chiusura import connetti
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException

from app.agents.agent_climate import ClimateAgent
from app.agents.agent_registry import AgentRegistry
from app.agents.base_agent import BaseAgent
from app.agents.dynamic_agent import DynamicAgent
from app.agents.medical_agents import CardiovascularOrganAgent, RespiratoryOrganAgent
from app.core.configurazione import costruisci_configurazione, imposta_configurazione
from app.core.constants import is_control_flag, is_flag_expired
from app.core.errori_llm import ErroreLLM
from app.db.database import Database
from app.graph.builder import build_graph, wrap_node_with_hitl
from app.graph.hitl_config import hitl_manager
from app.graph.orchestrator import BrainAgent
from app.MAO.model_access_object import Mao
from app.tools import sensor_tools
from app.tools.event_log import EventLog
from app.tools.medical_tools import HeartRateRegulatorTool, LungVentilatorTool, deterministic_biometric_normalizer
from app.tools.sensor_tools import IoTDeviceTool, get_default_iot_tools, get_tool
from app.tools.tool_wrapper import force_execute_tool


class BaseTest(unittest.IsolatedAsyncioTestCase):
    """Ambiente isolato: database temporaneo con lo schema, registro dei tool vuoto, HITL azzerato."""

    async def asyncSetUp(self):
        self._cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self._cartella.cleanup)
        self.db = os.path.join(self._cartella.name, "test.db")
        await Database(self.db).init_db()

        self.enterContext(patch.dict(sensor_tools._TOOL_REGISTRY, {}, clear=True))
        self._azzera_hitl()
        self.addCleanup(self._azzera_hitl)

    @staticmethod
    def _azzera_hitl():
        hitl_manager.update_config(hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)

    def log(self, **kwargs) -> EventLog:
        return EventLog(db_path=self.db, **kwargs)

    def usa_db(self, agente):
        """Fa scrivere l'agente sul database temporaneo del test invece che su quello predefinito."""
        agente.event_log.db_path = self.db
        return agente

    def righe(self, sql, *parametri):
        with connetti(self.db) as db:
            return db.execute(sql, parametri).fetchall()


STATO_BASE = {
    "messages": [], "readings": [], "recent_events": [], "pending_escalations": [],
    "next_agent": "brain", "hitl_required": False, "config": {},
}


# ── 1. Utilità ─────────────────────────────────────────────────────────────────────────────────────────────────


class UtilitaTest(unittest.TestCase):
    def test_is_control_flag(self):
        casi = [
            ("REJECTED", True), ("RECONCILED_foo", True), ("RESOLVED_bar", True), ("ESCALATION_baz", True),
            ("BLOCKED", True), ("rejected_lower", True), ("TOOL_ERROR_TURN_ON", True), ("INVALID_COMMAND_X", True),
            ("22.5°C", False), ("OFF", False), ("LOCKED", False), ("DISARMED", False), ("", False),
        ]
        sbagliati = [(valore, atteso) for valore, atteso in casi if is_control_flag(valore) != atteso]
        self.assertEqual(sbagliati, [])

    def test_filtro_dei_flag_di_controllo_per_l_event_producer(self):
        bloccati = ["REJECTED", "RECONCILED_ACT", "RESOLVED_ESC", "ESCALATION_PROP", "BLOCKED"]
        fisici = ["22.5°C", "OFF", "LOCKED", "DISARMED", "0", "100%"]
        self.assertTrue(all(is_control_flag(v) for v in bloccati))
        self.assertFalse(any(is_control_flag(v) for v in fisici))


# ── 2. Database ────────────────────────────────────────────────────────────────────────────────────────────────


class DatabaseTest(BaseTest):
    async def test_init_db_crea_le_tabelle_e_l_indice(self):
        tabelle = {r[0] for r in self.righe("SELECT name FROM sqlite_master WHERE type = 'table'")}
        indici = {r[0] for r in self.righe("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertTrue({"events", "readings"} <= tabelle)
        self.assertIn("idx_events_target_ts", indici)

    async def test_init_db_e_idempotente_e_non_semina_dati_di_prova_per_default(self):
        await Database(self.db).init_db()
        self.assertEqual(self.righe("SELECT COUNT(*) FROM events"), [(0,)])

    async def test_il_conflitto_dimostrativo_si_semina_solo_se_richiesto_e_non_si_accumula(self):
        imposta_configurazione(costruisci_configurazione({"demo": {"conflitto_all_avvio": 1}}))
        self.addCleanup(imposta_configurazione, None)

        await Database(self.db).init_db()
        await Database(self.db).init_db()

        self.assertEqual(self.righe("SELECT actor, action, target FROM events"), [("agent_security", "FORCE_SHUTDOWN", "ac_living_room")])

    async def test_un_database_con_lo_schema_vecchio_viene_migrato(self):
        vecchio = os.path.join(self._cartella.name, "vecchio.db")
        with connetti(vecchio) as db:
            db.execute("""CREATE TABLE events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
                          target TEXT NOT NULL, reasoning TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, escalated BOOLEAN DEFAULT 0)""")

        await Database(vecchio).init_db()

        with connetti(vecchio) as db:
            colonne = {r[1] for r in db.execute("PRAGMA table_info(events)")}
        self.assertTrue({"old_value", "new_value"} <= colonne)

    async def test_registrazione_e_lettura_degli_eventi(self):
        log = self.log(target=["all"], frequency=240)
        await log.log_event("test_actor", "TEST_ACTION", "ac_living_room", "OFF", "ON", "test", False)

        eventi = await log.get_recent_events()

        self.assertTrue(any(e["actor"] == "test_actor" and e["old_value"] == "OFF" and e["new_value"] == "ON" for e in eventi))

    async def test_mark_resolved_chiude_le_escalation_del_target(self):
        log = self.log(target=["all"], frequency=240)
        await log.log_event("agent_climate", "ESCALATION_PROPOSED", "heater_bedroom", "OFF", "22.5°C", "test", True)

        await log.mark_resolved("heater_bedroom")

        aperte = [e for e in await log.get_recent_events() if e["action"] == "ESCALATION_PROPOSED" and e["target"] == "heater_bedroom"]
        self.assertEqual(aperte, [])

    async def test_mark_resolved_chiude_anche_le_escalation_con_un_nome_di_azione_personalizzato(self):
        log = self.log(target=["all"], frequency=240)
        await log.log_event("organ_cardiovascular", "CRITICAL_ARHYTHMIA_ESCALATION", "cardiac_pacemaker", "160 BPM", "100 BPM", "x", True)
        await log.log_event("organ_cardiovascular", "CRITICAL_ARHYTHMIA_ESCALATION", "oxygen_regulator", "82", "95", "x", True)

        await log.mark_resolved("cardiac_pacemaker")

        azioni = {(e["target"], e["action"], e["escalated"]) for e in await log.get_recent_events()}
        self.assertIn(("cardiac_pacemaker", "RESOLVED_CRITICAL_ARHYTHMIA_ESCALATION", 0), azioni)
        self.assertIn(("oxygen_regulator", "CRITICAL_ARHYTHMIA_ESCALATION", 1), azioni)  # altri target: invariati

    async def test_mark_resolved_non_riscrive_un_evento_gia_risolto(self):
        log = self.log(target=["all"], frequency=240)
        await log.log_event("a", "RESOLVED_ESCALATION_PROPOSED", "x_dev", "1", "2", "r", True)

        await log.mark_resolved("x_dev")

        self.assertEqual([e["action"] for e in await log.get_recent_events()], ["RESOLVED_ESCALATION_PROPOSED"])

    async def test_gli_eventi_di_un_altro_target_non_vengono_letti(self):
        await self.log().log_event("x", "A", "front_door_lock", "1", "2", "r")
        self.assertEqual(await self.log(target=["heater_bedroom"], frequency=240).get_recent_events(), [])


# ── 3. Tool IoT ────────────────────────────────────────────────────────────────────────────────────────────────


class ToolIotTest(BaseTest):
    async def test_i_tool_predefiniti_sono_singleton(self):
        self.assertIs(get_default_iot_tools()["ac_living_room"], get_default_iot_tools()["ac_living_room"])

    async def test_lettura_e_scrittura(self):
        tool = get_tool("ac_living_room")
        await tool.set_tool_value("OFF")
        self.assertEqual(await tool.get_tool_value(), "OFF")
        await tool.set_tool_value("22.5°C")
        self.assertEqual(await tool.get_tool_value(), "22.5°C")

    async def test_l_unita_non_si_duplica(self):
        tool = get_tool("ac_living_room")
        await tool.set_tool_value("22.5°C")
        self.assertNotIn("°C°C", str(await tool.get_tool_value()))

    async def test_un_tool_creato_on_demand_e_lo_stesso_oggetto_alla_richiesta_successiva(self):
        primo = get_tool("dispositivo_on_demand", initial_value="IDLE", unit="status")
        self.assertEqual(await primo.get_tool_value(), "IDLE")
        await primo.set_tool_value("ACTIVE")
        self.assertEqual(await get_tool("dispositivo_on_demand").get_tool_value(), "ACTIVE")

    async def test_i_tool_condivisi_dal_grafo_sono_quelli_del_registro(self):
        _, condivisi = build_graph()
        self.assertIs(condivisi["ac_living_room"], sensor_tools._TOOL_REGISTRY["ac_living_room"])


# ── 4. MAO ─────────────────────────────────────────────────────────────────────────────────────────────────────


class MaoTest(unittest.IsolatedAsyncioTestCase):
    async def crea_mao(self, **variabili):
        with patch.dict(os.environ, variabili):
            mao = Mao()
        self.addAsyncCleanup(mao.aclose)
        return mao

    async def test_provider_registrati_e_predefinito_da_ambiente(self):
        mao = await self.crea_mao(DEFAULT_PROVIDER="openrouter")

        self.assertEqual(mao.default_provider, "openrouter")
        self.assertTrue({"openrouter", "google_studio", "mistral", "local"} <= set(mao.providers))

    async def test_provider_sconosciuto_solleva_un_errore_classificato(self):
        mao = await self.crea_mao()
        with self.assertRaises(ErroreLLM) as ctx:
            await mao.call_model("s", "u", provider="provider_inesistente", fallback_on_error=False)
        self.assertEqual(ctx.exception.codice, "PROVIDER_NON_SUPPORTATO")

    async def test_enable_reasoning_arriva_al_client_e_non_rompe_la_chiamata(self):
        mao = await self.crea_mao(OPENROUTER_API_KEY="chiave-di-prova")
        risposta = MagicMock()
        risposta.choices = [MagicMock()]
        risposta.choices[0].message.content = "test"
        risposta.choices[0].finish_reason = "stop"
        create = AsyncMock(return_value=risposta)

        with patch.object(mao.providers["openrouter"]["client"].chat.completions, "create", create):
            risultato = await mao.call_model("sys", "usr", enable_reasoning=True, provider="openrouter")

        self.assertEqual(risultato, "test")
        self.assertEqual(create.await_args.kwargs["extra_body"], {"reasoning": {"enabled": True}})


@unittest.skipUnless(os.getenv("RUN_LLM_TESTS") == "1", "chiamata reale a un LLM: impostare RUN_LLM_TESTS=1")
class MaoRealeTest(unittest.IsolatedAsyncioTestCase):
    async def test_chiamata_reale_al_provider_predefinito(self):
        mao = Mao()
        self.addAsyncCleanup(mao.aclose)
        risposta = await mao.call_model("Rispondi solo con OK.", "Test connessione.", max_tokens=200)
        self.assertTrue(risposta.strip())


# ── 5. BaseAgent ───────────────────────────────────────────────────────────────────────────────────────────────


class AgenteDiProva(BaseAgent):
    async def process(self, state, recent_events, relevant_readings, agent_escalations):
        return {}


class BaseAgentTest(BaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.agente = self.usa_db(AgenteDiProva("dummy", ["ac_living_room"], 30, 1.0))

    async def test_l_applicazione_dello_stato_e_idempotente(self):
        tool = get_default_iot_tools()["ac_living_room"]
        await tool.set_tool_value("OFF")
        strumenti = get_default_iot_tools()

        primo = await self.agente.apply_status("ac_living_room", "TURN_ON_AC", "22.5°C", "test", False, strumenti)
        secondo = await self.agente.apply_status("ac_living_room", "TURN_ON_AC", "22.5°C", "test", False, strumenti)

        self.assertIs(primo, True)
        self.assertIs(secondo, False)

    async def test_conflitto_recente_da_un_altro_attore(self):
        eventi = [
            {"actor": "Brain", "action": "FORCE_SHUTDOWN", "target": "ac_living_room"},
            {"actor": "dummy", "action": "TURN_ON", "target": "ac_living_room"},
        ]
        conflitto, evento = self.agente.check_for_recent_conflict("ac_living_room", eventi)
        assenza, _ = self.agente.check_for_recent_conflict("heater_bedroom", [])

        self.assertTrue(conflitto)
        self.assertEqual(evento["actor"], "Brain")
        self.assertFalse(assenza)

    async def test_creazione_di_un_escalation(self):
        esc = self.agente.create_escalation("ac_living_room", "22.5°C", "motivo", True, [])

        self.assertEqual((esc["source_agent"], esc["proposed_action"], esc["conflict_detected"]), ("dummy", "22.5°C", True))
        self.assertTrue(esc["id"])
        self.assertIsNone(esc["tool_result"])


# ── 6. ClimateAgent ────────────────────────────────────────────────────────────────────────────────────────────


class ClimateAgentTest(BaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tools = get_default_iot_tools()

    async def esegui(self, risposta_llm, valore_ac="OFF", eventi=None):
        await self.tools["ac_living_room"].set_tool_value(valore_ac)
        agente = self.usa_db(ClimateAgent(tools=self.tools))
        with patch.object(agente, "ask_brain", AsyncMock(return_value=risposta_llm)) as ask:
            risultato = await agente.process(dict(STATO_BASE), eventi or [], [], [])
        return risultato, ask

    async def test_ramo_action_attiva_il_condizionatore(self):
        risultato, _ = await self.esegui("DECISIONE: ACTION\nMOTIVAZIONE: caldo")
        self.assertEqual(risultato["next_agent"], "END")
        self.assertEqual(await self.tools["ac_living_room"].get_tool_value(), "22.5°C")

    async def test_ramo_none_lascia_tutto_com_e(self):
        risultato, _ = await self.esegui("DECISIONE: NONE\nMOTIVAZIONE: ok", valore_ac="22.5°C")
        self.assertEqual(risultato["next_agent"], "END")
        self.assertEqual(await self.tools["ac_living_room"].get_tool_value(), "22.5°C")

    async def test_un_dispositivo_bloccato_non_interpella_il_modello(self):
        risultato, ask = await self.esegui("", valore_ac="REJECTED")
        self.assertEqual(risultato["next_agent"], "END")
        ask.assert_not_awaited()

    async def test_ramo_escalate_non_tocca_il_dispositivo(self):
        conflitto = {"actor": "agent_security", "action": "FORCE_SHUTDOWN", "target": "ac_living_room", "escalated": 0}

        risultato, _ = await self.esegui("DECISIONE: ESCALATE\nMOTIVAZIONE: conflitto", eventi=[conflitto])

        self.assertEqual(await self.tools["ac_living_room"].get_tool_value(), "OFF")
        self.assertEqual(risultato["next_agent"], "brain")
        self.assertEqual(len(risultato["pending_escalations"]), 1)


# ── 7. BrainAgent ──────────────────────────────────────────────────────────────────────────────────────────────


class BrainAgentTest(BaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tools = get_default_iot_tools()
        self.brain = self.usa_db(BrainAgent(tools=list(self.tools.values())))
        self.escalation = {
            "source_agent": "agent_climate", "target_device": "ac_living_room", "proposed_action": "22.5°C",
            "reason": "test", "conflict_detected": True, "context_events": [],
        }

    async def chiama(self, risposta_llm, pendenti=None, prossimo="brain"):
        stato = {**STATO_BASE, "pending_escalations": pendenti or [], "next_agent": prossimo}
        with patch.object(self.brain, "ask_brain", AsyncMock(return_value=risposta_llm)):
            return await self.brain.process(stato, [], [], pendenti or [])

    async def test_instrada_verso_l_agente_clima(self):
        self.assertEqual((await self.chiama("DECISIONE: APPROVA"))["next_agent"], "agent_climate")

    async def test_approvazione_di_un_escalation(self):
        await self.tools["ac_living_room"].set_tool_value("OFF")

        risultato = await self.chiama("DECISIONE: APPROVA\nMOTIVAZIONE: ok", [self.escalation])

        self.assertEqual((risultato["next_agent"], risultato["pending_escalations"]), ("END", []))
        self.assertEqual(await self.tools["ac_living_room"].get_tool_value(), "22.5°C")

    async def test_rifiuto_di_un_escalation_marca_il_dispositivo(self):
        await self.tools["ac_living_room"].set_tool_value("OFF")

        await self.chiama("DECISIONE: RESPINGI\nMOTIVAZIONE: finestra aperta", [self.escalation])

        self.assertEqual(await self.tools["ac_living_room"].get_tool_value(), "REJECTED")

    async def test_check_body_status(self):
        letture = [{"sensor_id": k, "agent_owner": "brain", "value": str(await v.get_tool_value()), "unit": ""} for k, v in self.tools.items()]
        stato = {**STATO_BASE, "readings": letture, "next_agent": "END"}

        with patch.object(self.brain, "ask_brain", AsyncMock(return_value="STATUS: OK\nDETTAGLI: tutto ok")):
            risultato = await self.brain.check_body_status(stato, letture, [])

        self.assertIn("messages", risultato)

    async def test_override_semantico_accende_con_lo_stato_on(self):
        for nome in ("main_breaker", "emergency_lights"):
            self.brain.tools[nome] = get_tool(nome, "OFF", "")

        with patch.object(self.brain, "ask_brain", AsyncMock(return_value=json.dumps([
            {"target": "main_breaker", "action": "TURN_ON", "value": None},
            {"target": "emergency_lights", "action": "TURN_ON", "value": None},
        ]))):
            messaggi = await self.brain._execute_semantic_override(
                human_directive="Riattiva main_breaker ed emergency_lights", fallback_target="main_breaker",
                fallback_action="FORCE_SHUTDOWN",
            )

        self.assertEqual(await self.brain.tools["main_breaker"].get_tool_value(), "ON")
        self.assertEqual(await self.brain.tools["emergency_lights"].get_tool_value(), "ON")
        self.assertTrue(all("FORCE_SHUTDOWN" not in m for m in messaggi))

    async def test_override_con_una_risposta_non_json_non_esegue_nessun_comando(self):
        """Prima eseguiva un comando di ripiego non richiesto: ora il grafo si ferma per l'operatore."""
        stufa = get_tool("heater_test_ov", initial_value="OFF", unit="°C")
        self.brain.tools["heater_test_ov"] = stufa

        with patch.object(self.brain, "ask_brain", AsyncMock(return_value="Mi dispiace, non ho capito.")):
            with self.assertRaises(ErroreLLM) as ctx:
                await self.brain._execute_semantic_override(
                    human_directive="Accendi il riscaldamento", fallback_target="heater_test_ov", fallback_action="ON",
                )

        self.assertEqual(ctx.exception.codice, "RISPOSTA_NON_UTILIZZABILE")
        self.assertEqual(await stufa.get_tool_value(), "OFF")

    async def test_force_execute_tool_scavalca_i_blocchi_di_priorita(self):
        log = self.log(target=["pool_pump"])
        await log.log_event("organ_energy", "FORCE_SHUTDOWN", "pool_pump", "ON", "OFF", "Picco di rete", False)
        pompa = get_tool("pool_pump", initial_value="OFF", unit="")

        ok, messaggio = await force_execute_tool(
            target="pool_pump", tool_obj=pompa, action="UNBLOCK_AND_SET", new_value="ON",
            reasoning="richiesta dell'operatore", event_log=log,
        )

        self.assertTrue(ok, messaggio)
        self.assertEqual(await pompa.get_tool_value(), "ON")


# ── 8. Loop degli eventi ───────────────────────────────────────────────────────────────────────────────────────


class EventProducerTest(BaseTest):
    async def produci(self, tools, **kwargs):
        from run_loop import sensor_event_producer

        coda = asyncio.Queue()
        compito = asyncio.create_task(sensor_event_producer(coda, tools, poll_interval=0.01, **kwargs))
        self.addCleanup(compito.cancel)
        await asyncio.sleep(0.05)
        return coda, compito

    async def test_un_cambio_fisico_genera_un_evento(self):
        lampada = IoTDeviceTool("lampada", "OFF")
        coda, compito = await self.produci({"lampada": lampada}, max_events=1)

        await lampada.set_tool_value("ON")
        evento = await asyncio.wait_for(coda.get(), 2)
        await asyncio.wait_for(compito, 2)

        self.assertEqual((evento.sensor_id, evento.old_value, evento.new_value), ("lampada", "OFF", "ON"))

    async def test_un_flag_interno_non_genera_eventi(self):
        lampada = IoTDeviceTool("lampada", "OFF")
        coda, _ = await self.produci({"lampada": lampada})

        await lampada.set_tool_value("REJECTED")
        await asyncio.sleep(0.1)

        self.assertTrue(coda.empty())

    async def test_un_dispositivo_guasto_non_ferma_il_monitoraggio_degli_altri(self):
        guasto, sano = IoTDeviceTool("guasto", "OFF"), IoTDeviceTool("sano", "OFF")
        coda, compito = await self.produci({"guasto": guasto, "sano": sano}, max_events=1)

        guasto.imposta_guasto("sensore offline")
        await sano.set_tool_value("ON")
        evento = await asyncio.wait_for(coda.get(), 2)
        await asyncio.wait_for(compito, 2)

        self.assertEqual(evento.sensor_id, "sano")


# ── 9. API ─────────────────────────────────────────────────────────────────────────────────────────────────────


class ApiTest(BaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.registro = AgentRegistry(db_path=self.db)
        await self.registro.init_registry_db()
        self.enterContext(patch("app.api.main.registry", self.registro))
        self.enterContext(patch("app.api.main._recompile_system_graph", AsyncMock()))

    def crea(self, definizione):
        from app.api.main import CreateSubAgentRequest, create_sub_agent

        testo = definizione if isinstance(definizione, str) else json.dumps(definizione)
        return create_sub_agent(CreateSubAgentRequest(agent_definition=testo))

    def test_gli_schemi_delle_richieste(self):
        from app.api.main import CreateSubAgentRequest, LlmProxyRequest, RunCycleRequest, SeedConflictRequest, ToolWriteRequest

        self.assertEqual(RunCycleRequest().force_next_agent, "brain")
        self.assertEqual(ToolWriteRequest(target="ac_living_room", value="22.5°C").value, "22.5°C")
        self.assertEqual(SeedConflictRequest().actor, "agent_security")
        self.assertFalse(LlmProxyRequest(system_prompt="s", user_prompt="u").enable_reasoning)
        self.assertEqual(json.loads(CreateSubAgentRequest(agent_definition='{"agent_name": "x"}').agent_definition)["agent_name"], "x")

    def test_le_rotte_principali_sono_registrate(self):
        from app.api.main import app

        percorsi = {r.path for r in app.routes}
        for atteso in ("/", "/graph/run", "/tools", "/llm/invoke", "/agents/create", "/graph/health-check", "/graph/resume", "/hitl/config"):
            self.assertIn(atteso, percorsi)

    async def test_creazione_di_un_sotto_agente(self):
        risposta = await self.crea({"agent_name": "agent_test", "managed_targets": ["door"]})

        self.assertIn("registered", risposta["status"])
        self.assertEqual(risposta["agent_name"], "agent_test")

    async def test_json_non_valido_risponde_422(self):
        with self.assertRaises(HTTPException) as ctx:
            await self.crea("not json")
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_campi_obbligatori_mancanti_rispondono_422(self):
        with self.assertRaises(HTTPException) as ctx:
            await self.crea({"agent_name": "x"})
        self.assertEqual(ctx.exception.status_code, 422)

    def test_schemi_hitl(self):
        from app.api.main import HitlResumeRequest, UnblockTargetRequest

        self.assertEqual(HitlResumeRequest(decision="APPROVA", reasoning="ok").decision, "APPROVA")
        self.assertEqual(UnblockTargetRequest(target="ac_living_room").target, "ac_living_room")


# ── 10. TTL e sblocco ──────────────────────────────────────────────────────────────────────────────────────────


class TtlTest(BaseTest):
    def test_scadenza_dei_flag(self):
        vecchio = (datetime.now(timezone.utc) - timedelta(minutes=120)).strftime("%Y-%m-%d %H:%M:%S")
        recente = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        self.assertTrue(is_flag_expired(vecchio, ttl_minutes=60))
        self.assertFalse(is_flag_expired(recente, ttl_minutes=60))

    async def test_sblocco_ed_espirazione_su_database(self):
        log = self.log()
        await log.unblock_target("ac_living_room", "sblocco di prova", actor="test")

        eventi = await log.get_recent_events()
        scaduti = await log.expire_old_control_flags(ttl_minutes=0)

        self.assertTrue(any(e["action"] == "UNBLOCKED" and e["target"] == "ac_living_room" for e in eventi))
        self.assertIsInstance(scaduti, int)


# ── 11. HITL ───────────────────────────────────────────────────────────────────────────────────────────────────


class HitlTest(BaseTest):
    async def test_configurazione_dinamica_e_decisione_di_interrupt(self):
        hitl_manager.update_config(
            hitl_all=False, hitl_nodes=["organ_security"], hitl_targets=["front_door_lock"],
            hitl_actions=["FORCE_SHUTDOWN"], max_wait_seconds=120,
        )
        cfg = hitl_manager.get_config()

        self.assertEqual((cfg.hitl_nodes, cfg.hitl_targets, cfg.hitl_actions, cfg.max_wait_seconds),
                         (["organ_security"], ["front_door_lock"], ["FORCE_SHUTDOWN"], 120))
        self.assertTrue(hitl_manager.should_interrupt("organ_security", {}))
        self.assertFalse(hitl_manager.should_interrupt("agent_climate", {}))
        self.assertTrue(hitl_manager.should_interrupt("agent_climate", {}, proposed_target="front_door_lock"))
        self.assertTrue(hitl_manager.should_interrupt("agent_climate", {}, proposed_action="FORCE_SHUTDOWN"))

    async def test_max_wait_seconds_si_puo_azzerare(self):
        hitl_manager.update_config(max_wait_seconds=120)
        hitl_manager.update_config(hitl_all=False)
        self.assertEqual(hitl_manager.get_config().max_wait_seconds, 120)
        hitl_manager.update_config(max_wait_seconds=None)
        self.assertIsNone(hitl_manager.get_config().max_wait_seconds)

    async def test_override_semantico_dal_wrapper_non_scrive_riconciliazioni(self):
        hitl_manager.update_config(hitl_all=True, hitl_nodes=["brain"])
        brain = self.usa_db(BrainAgent())
        stufa = get_tool("heater_override_test", initial_value="OFF", unit="°C")
        brain.tools["heater_override_test"] = stufa
        chiamate = []

        async def override_finto(human_directive, fallback_target, fallback_action):
            chiamate.append(human_directive)
            await stufa.set_tool_value("22°C")
            return ["[Brain_Override] ✓ ESEGUITO — UNBLOCK_AND_SET su 'heater_override_test' → '22°C'"]

        brain._execute_semantic_override = override_finto
        stato = {**STATO_BASE, "hitl_required": True}

        with patch("app.graph.builder.interrupt", return_value={"decision": "OVERRIDE", "reasoning": "Accendi la stufa a 22 gradi"}), \
                patch("app.tools.event_log.EventLog.get_recent_events", AsyncMock(return_value=[])):
            risultato = await wrap_node_with_hitl("brain", brain)(stato)

        self.assertEqual(chiamate, ["Accendi la stufa a 22 gradi"])
        self.assertEqual(await stufa.get_tool_value(), "22°C")
        self.assertEqual(risultato["next_agent"], "END")
        self.assertIn("ESEGUITO", risultato["messages"][-1].content)


# ── 12. Gerarchia dinamica ─────────────────────────────────────────────────────────────────────────────────────


class GerarchiaTest(BaseTest):
    async def test_registro_albero_e_istanze(self):
        registro = AgentRegistry(db_path=self.db)
        await registro.init_registry_db()

        await registro.register_agent_config({"name": "organ_security", "level": 1, "parent_agent_name": "Brain", "managed_targets": ["alarm_system"], "sub_agent_names": []})
        await registro.register_agent_config({"name": "component_door_lock", "level": 2, "parent_agent_name": "organ_security", "managed_targets": ["front_door_lock"], "sub_agent_names": []})

        configs = await registro.get_all_agent_configs()
        organo = next(c for c in configs if c["name"] == "organ_security")
        albero = await registro.get_hierarchy_tree()
        istanze = await registro.build_agent_instances()

        self.assertEqual(organo["sub_agent_names"], ["component_door_lock"])  # derivati da parent_agent_name
        self.assertEqual(albero["root"], "Brain")
        self.assertIsInstance(istanze["organ_security"], DynamicAgent)
        self.assertIn("component_door_lock", istanze)

        # i figli si eliminano prima del padre
        self.assertTrue(await registro.delete_agent("component_door_lock"))
        self.assertTrue(await registro.delete_agent("organ_security"))


# ── 13. Strumenti e agenti medici ──────────────────────────────────────────────────────────────────────────────


class MedicoTest(BaseTest):
    def test_normalizzazione_deterministica(self):
        normale = deterministic_biometric_normalizer(75.0, 60.0, 100.0)
        patologico = deterministic_biometric_normalizer(160.0, 60.0, 100.0)

        self.assertTrue(normale["is_in_range"])
        self.assertEqual(normale["normalized_score"], -0.25)
        self.assertFalse(patologico["is_in_range"])
        self.assertEqual(patologico["recommended_target"], 100.0)

    async def test_lettura_e_scrittura_dei_tool_medici(self):
        cuore, polmone = HeartRateRegulatorTool(), LungVentilatorTool()
        self.assertEqual(await cuore.get_tool_value(), "72.0 BPM")
        await cuore.set_tool_value(160.0)
        self.assertEqual(await cuore.get_tool_value(), "160.0 BPM")
        self.assertEqual(await polmone.get_tool_value(), "98.0%")
        await polmone.set_tool_value(82.0)
        self.assertEqual(await polmone.get_tool_value(), "82.0%")

    async def test_un_flag_di_controllo_non_rompe_i_tool_numerici(self):
        cuore, polmone = HeartRateRegulatorTool(), LungVentilatorTool()
        await cuore.set_tool_value(120.0)

        self.assertIs(await cuore.set_tool_value("REJECTED"), True)
        self.assertIs(await polmone.set_tool_value("BLOCKED"), True)

        self.assertEqual(await cuore.get_tool_value(), "120.0 BPM")
        self.assertEqual(await polmone.get_tool_value(), "98.0%")

    async def test_un_tool_personalizzato_registrato_lo_trova_anche_il_brain(self):
        from app.tools.sensor_tools import registra_tool, trova_tool

        cuore = HeartRateRegulatorTool()
        registra_tool("cardiac_pacemaker", cuore)

        self.assertIs(trova_tool("cardiac_pacemaker", {}), cuore)

    async def test_omeostasi_cardiaca_e_respiratoria(self):
        cuore, polmone = HeartRateRegulatorTool(), LungVentilatorTool()
        await cuore.set_tool_value(160.0)
        await polmone.set_tool_value(82.0)
        strumenti = {"cardiac_pacemaker": cuore, "oxygen_regulator": polmone}
        cardio = self.usa_db(CardiovascularOrganAgent(tools=strumenti))
        respiro = self.usa_db(RespiratoryOrganAgent(tools=strumenti))

        esito_cuore = await cardio({**STATO_BASE, "next_agent": "organ_cardiovascular",
                                    "readings": [{"sensor_id": "cardiac_pacemaker", "agent_owner": "test", "value": "160.0", "unit": "BPM"}]})
        await respiro({**STATO_BASE, "next_agent": "organ_respiratory",
                       "readings": [{"sensor_id": "oxygen_regulator", "agent_owner": "test", "value": "82.0", "unit": "%"}]})

        self.assertEqual(esito_cuore["next_agent"], "brain")  # aritmia severa: escalation al Brain
        self.assertEqual(len(esito_cuore["pending_escalations"]), 1)
        self.assertEqual(await polmone.get_tool_value(), "95.0%")  # ipossia: ripristinato il target di SpO2


if __name__ == "__main__":
    unittest.main()
