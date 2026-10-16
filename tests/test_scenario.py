import os
from sqlite_chiusura import connetti
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite

from app.agents.agent_registry import AgentRegistry
from app.core.configurazione import carica_configurazione
from app.core.modelli_agenti import risolvi_modello
from app.db.scenario import AGENTI_BASE, SCENARI, azzera_database, crea_backup, crea_scenario
from app.tools.event_log import EventLog

RADICE = Path(__file__).resolve().parents[1]


class ScenarioTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self._cartella.cleanup)
        self.db = os.path.join(self._cartella.name, "scenario.db")

    def conta(self, tabella):
        with connetti(self.db) as db:
            return db.execute(f"SELECT COUNT(*) FROM {tabella}").fetchone()[0]

    async def test_lo_scenario_base_crea_la_gerarchia_e_lo_storico(self):
        riepilogo = await crea_scenario(self.db, "base")

        configs = {c["name"]: c for c in await AgentRegistry(db_path=self.db).get_all_agent_configs()}
        self.assertEqual(set(configs), {a[0] for a in AGENTI_BASE})
        self.assertEqual((configs["component_door_lock"]["level"], configs["component_door_lock"]["parent_agent_name"]), (2, "organ_security"))
        self.assertEqual(configs["organ_security"]["sub_agent_names"], ["component_alarm", "component_door_lock"])
        self.assertEqual((riepilogo["eventi"], self.conta("events")), (6, 6))

    async def test_i_target_dello_scenario_sono_tutti_elencati_in_configurazione_toml(self):
        cfg = carica_configurazione(RADICE / "configurazione.toml")
        for _, _, target, _, _ in AGENTI_BASE:
            for dispositivo in target:
                self.assertTrue(cfg.dispositivo_ammesso(dispositivo) and cfg.regola_per(dispositivo), dispositivo)

    async def test_gli_agenti_dello_scenario_si_possono_istanziare(self):
        await crea_scenario(self.db, "base")
        istanze = await AgentRegistry(db_path=self.db).build_agent_instances()
        self.assertEqual(set(istanze), {a[0] for a in AGENTI_BASE})

    async def test_lo_storico_e_fuori_dalle_finestre_degli_agenti(self):
        await crea_scenario(self.db, "base")
        for finestra_minuti in (30, 60, 240):
            recenti = await EventLog(["all"], finestra_minuti, self.db).get_recent_events()
            self.assertEqual(recenti, [], f"finestra {finestra_minuti} min")

    async def test_lo_scenario_vuoto_azzera_tutto_senza_nemmeno_il_seme(self):
        await crea_scenario(self.db, "base")
        await crea_scenario(self.db, "vuoto")
        for tabella in ("events", "readings", "agents_registry", "agent_models", "hitl_scadenze"):
            self.assertEqual(self.conta(tabella), 0, tabella)

    async def test_conflitto_porta_attiva_l_escalation_del_componente(self):
        from app.agents.dynamic_agent import DynamicAgent

        await crea_scenario(self.db, "conflitto_porta")
        recenti = await EventLog(["front_door_lock"], 30, self.db).get_recent_events()
        self.assertEqual([(e["actor"], e["action"]) for e in recenti], [("user_manual", "SECURITY_LOCK")])

        istanze = await AgentRegistry(db_path=self.db).build_agent_instances()
        porta = istanze["component_door_lock"]
        self.assertIsInstance(porta, DynamicAgent)
        porta.event_log.db_path = self.db
        porta.event_log.log_event = AsyncMock()
        porta.ask_brain = AsyncMock(return_value="DECISIONE: ACTION\nMOTIVAZIONE: chiudere")

        risultato = await porta.process({"config": {}}, await porta.event_log.get_recent_events(), [], [])

        self.assertEqual(risultato["next_agent"], "organ_security")
        self.assertEqual(risultato["pending_escalations"][0]["source_agent"], "component_door_lock")

    async def test_finestra_aperta_e_il_conflitto_dimostrativo_storico(self):
        await crea_scenario(self.db, "finestra_aperta")
        recenti = await EventLog(["ac_living_room"], 30, self.db).get_recent_events()
        self.assertEqual([(e["actor"], e["action"], e["new_value"]) for e in recenti], [("agent_security", "FORCE_SHUTDOWN", "OFF")])

    async def test_i_modelli_per_agente_dello_scenario_si_risolvono_con_ereditarieta(self):
        await crea_scenario(self.db, "base", {"organ_security": ("MistralAI", "ministral-8b-latest"), "Brain": ("openrouter", None)})

        assert_uguale = self.assertEqual
        assert_uguale((await risolvi_modello("component_alarm", self.db)).provider, "mistral")
        assert_uguale((await risolvi_modello("component_lights", self.db)).origine, "Brain")

    async def test_scenario_o_provider_sconosciuti_sono_rifiutati_prima_di_toccare_il_database(self):
        with self.assertRaises(ValueError):
            await crea_scenario(self.db, "inventato")
        with self.assertRaises(ValueError):
            await crea_scenario(self.db, "base", {"organ_security": ("provider_inventato", None)})
        self.assertFalse(os.path.exists(self.db))

    async def test_ricreare_lo_scenario_e_idempotente_e_cancella_i_dati_precedenti(self):
        await crea_scenario(self.db, "base")
        async with aiosqlite.connect(self.db) as db:
            await db.execute("INSERT INTO events (actor, action, target) VALUES ('x', 'residuo', 'y')")
            await db.execute("CREATE TABLE IF NOT EXISTS checkpoints (thread_id TEXT)")
            await db.execute("INSERT INTO checkpoints VALUES ('vecchio')")
            await db.commit()

        await crea_scenario(self.db, "base")

        self.assertEqual((self.conta("events"), self.conta("agents_registry")), (6, 6))
        with connetti(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE action = 'residuo'").fetchone()[0], 0)
            # i checkpoint vecchi non sopravvivono (la tabella viene ricreata dal checkpointer all'avvio)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'checkpoints'").fetchone()[0], 0)

    async def test_azzera_database_elimina_le_tabelle_e_tollera_quelle_mancanti(self):
        async with aiosqlite.connect(self.db) as db:
            await db.execute("CREATE TABLE events (event_id INTEGER PRIMARY KEY, actor TEXT)")
            await db.execute("INSERT INTO events (actor) VALUES ('x')")
            await db.commit()
        await azzera_database(self.db)
        with connetti(self.db) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall(), [])

    async def test_un_database_con_schema_vecchio_viene_ricreato_da_capo(self):
        with connetti(self.db) as db:
            db.execute("CREATE TABLE events (event_id INTEGER PRIMARY KEY, actor TEXT NOT NULL)")
            db.execute("CREATE TABLE agents_registry (name TEXT PRIMARY KEY)")
            db.execute("INSERT INTO agents_registry VALUES ('vecchio')")

        await crea_scenario(self.db, "base")

        self.assertEqual((self.conta("events"), self.conta("agents_registry")), (6, 6))

    def test_il_backup_copia_il_database_con_suffisso_db(self):
        Path(self.db).write_bytes(b"contenuto")
        backup = crea_backup(self.db)

        self.assertTrue(backup.name.endswith(".db") and ".backup-" in backup.name)
        self.assertEqual(backup.read_bytes(), b"contenuto")
        self.assertIsNone(crea_backup(os.path.join(self._cartella.name, "non_esiste.db")))
        vuoto = Path(self._cartella.name) / "vuoto.db"
        vuoto.write_bytes(b"")
        self.assertIsNone(crea_backup(str(vuoto)))

    def test_ogni_scenario_ha_un_nome_documentato(self):
        self.assertEqual(set(SCENARI), {"vuoto", "base", "conflitto_porta", "finestra_aperta"})


class ScriptScenarioTest(unittest.TestCase):
    def setUp(self):
        self._cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self._cartella.cleanup)
        self.db = os.path.join(self._cartella.name, "script.db")

    def esegui(self, *argomenti, **kwargs):
        return subprocess.run(
            [sys.executable, str(RADICE / "examples" / "crea_scenario.py"), "--db", self.db, *argomenti],
            capture_output=True, text=True, cwd=RADICE, timeout=120, **kwargs,
        )

    def test_lo_script_crea_lo_scenario_e_fa_il_backup_di_un_database_esistente(self):
        with connetti(self.db) as db:
            db.execute("CREATE TABLE events (event_id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL)")
            db.execute("INSERT INTO events (actor, action, target) VALUES ('vecchio', 'vecchia', 'vecchio')")
        risultato = self.esegui("--scenario", "base", "--si")

        self.assertEqual(risultato.returncode, 0, risultato.stderr)
        self.assertIn("6 agenti", risultato.stdout)
        self.assertIn("Backup:", risultato.stdout)
        backup = [f for f in os.listdir(self._cartella.name) if ".backup-" in f]
        self.assertEqual(len(backup), 1)
        with connetti(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM agents_registry").fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE actor = 'vecchio'").fetchone()[0], 0)
        with connetti(os.path.join(self._cartella.name, backup[0])) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE actor = 'vecchio'").fetchone()[0], 1)

    def test_senza_conferma_non_tocca_nulla(self):
        Path(self.db).write_bytes(b"dati da non perdere")
        risultato = self.esegui("--scenario", "base", input="n\n")

        self.assertNotEqual(risultato.returncode, 0)
        self.assertEqual(Path(self.db).read_bytes(), b"dati da non perdere")

    def test_argomenti_non_validi_sono_rifiutati(self):
        self.assertNotEqual(self.esegui("--scenario", "inventato", "--si").returncode, 0)
        self.assertNotEqual(self.esegui("--modello", "senza_provider", "--si").returncode, 0)


if __name__ == "__main__":
    unittest.main()
