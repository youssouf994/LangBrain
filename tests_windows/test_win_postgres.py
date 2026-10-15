"""
Integrazione su Postgres reale: parte solo se `TEST_DATABASE_URL` è impostato (un database di PROVA: le tabelle vengono
cancellate). Esempio con Docker:

    docker run -d --name lb-pg -e POSTGRES_PASSWORD=prova -e POSTGRES_DB=langbrain_test -p 55432:5432 postgres:16
    set TEST_DATABASE_URL=postgresql://postgres:prova@localhost:55432/langbrain_test
"""

import asyncio
import os
import sys
import unittest

from sqlalchemy import text

from app.core import archivio, multiprocesso
from app.db.database import Database
from app.tools.nervous_system import NervousSystem

URL = os.getenv("TEST_DATABASE_URL")

if sys.platform == "win32":  # psycopg asincrono (checkpointer) non funziona con il ProactorEventLoop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

TABELLE = ("events", "signals", "readings", "node_runs", "runtime_state", "leases",
           "checkpoint_writes", "checkpoint_blobs", "checkpoints", "checkpoint_migrations")


@unittest.skipUnless(URL, "TEST_DATABASE_URL non impostato")
class PostgresTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        multiprocesso._pronte.clear()
        async with archivio.motore(URL).begin() as db:
            for tabella in TABELLE:
                await db.execute(text(f"DROP TABLE IF EXISTS {tabella} CASCADE"))
        self.addAsyncCleanup(archivio.chiudi_motori)  # i pool sono legati al loop del test

    async def test_init_db_crea_le_tabelle_ed_e_idempotente(self):
        await Database(URL).init_db()
        await Database(URL).init_db()
        async with archivio.motore(URL).connect() as db:
            tabelle = {r[0] for r in await db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
            indici = {r[0] for r in await db.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"))}
        self.assertTrue({"events", "signals", "readings", "node_runs"} <= tabelle)
        self.assertIn("idx_events_target_ts", indici)

    async def test_un_database_con_lo_schema_vecchio_viene_migrato(self):
        async with archivio.motore(URL).begin() as db:
            await db.execute(text("CREATE TABLE events (event_id SERIAL PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, "
                                  "target TEXT NOT NULL, reasoning TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, escalated INTEGER DEFAULT 0)"))
        await Database(URL).init_db()
        async with archivio.motore(URL).connect() as db:
            colonne = {r[0] for r in await db.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'events'"))}
        self.assertTrue({"old_value", "new_value"} <= colonne)

    async def test_eventi_escalation_e_ttl(self):
        await Database(URL).init_db()
        log = NervousSystem(db_path=URL)
        await log.log_event("a", "ESCALATION_PROPOSED", "lampada", "OFF", "ON", "r", True)
        await log.log_event("b", "ESCALATION_PROPOSED", "lampada", "OFF", "ON", "r", True)
        eventi = await log.get_recent_events()
        self.assertEqual(len(eventi), 2)
        self.assertRegex(eventi[0]["timestamp"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(eventi[0]["escalated"], 1)
        self.assertEqual(await log.mark_resolved("lampada"), 2)
        self.assertEqual(await log.mark_resolved("lampada"), 0)  # già risolte: non si riscrivono
        await log.unblock_target("lampada", "ok")
        self.assertIn("UNBLOCKED", {e["action"] for e in await log.get_recent_events()})
        await log.log_event("c", "REJECTED_ON", "pompa", "x", "y", "r", False)
        await asyncio.sleep(1.1)  # i timestamp sono al secondo e il limite del TTL è un "<" stretto
        self.assertEqual(await log.expire_old_control_flags(ttl_minutes=0), 1)

    async def test_segnali_e_traccia_dei_nodi(self):
        await Database(URL).init_db()
        log = NervousSystem(db_path=URL)
        self.assertEqual(await log.record_signals([{"receptor_id": "t1", "agent_owner": "a", "value": 21.5, "unit": "C"},
                                                    {"receptor_id": "t1", "agent_owner": "a", "value": "ON", "unit": ""}]), 1)
        self.assertEqual([s["value"] for s in await log.get_recent_signals(receptor_id="t1")], [21.5])
        from datetime import datetime, timezone
        await log.record_node_run("run1", "read", datetime.now(timezone.utc).isoformat(timespec="seconds"), 12.345, "ok")
        runs = await log.get_node_runs()
        self.assertEqual([(r["run_id"], r["node"], r["duration_ms"]) for r in runs], [("run1", "read", 12.35)])

    async def test_stato_condiviso_e_contratti(self):
        self.assertEqual(await multiprocesso.scrivi("k", {"a": 1}, percorso=URL), 1)
        self.assertEqual(await multiprocesso.scrivi("k", {"a": 2}, percorso=URL), 2)
        self.assertEqual(await multiprocesso.leggi("k", percorso=URL), {"a": 2})
        self.assertEqual(await multiprocesso.versioni("k", "altra", percorso=URL), {"k": 2, "altra": 0})

        self.assertTrue(await multiprocesso._acquisisci("c", "p1", 30, URL))
        self.assertFalse(await multiprocesso._acquisisci("c", "p2", 30, URL))   # occupato da un altro
        self.assertTrue(await multiprocesso._acquisisci("c", "p1", 30, URL))    # il proprietario rinnova
        await multiprocesso._rilascia("c", "p1", URL)
        self.assertTrue(await multiprocesso._acquisisci("c", "p2", 30, URL))
        self.assertTrue(await multiprocesso._acquisisci("scaduto", "p1", -1, URL))
        self.assertTrue(await multiprocesso._acquisisci("scaduto", "p2", 30, URL))  # scaduto: lo prende un altro

    async def test_checkpointer_su_postgres(self):
        from app import checkpointer
        saver = await checkpointer.apri_checkpointer(URL)
        self.addAsyncCleanup(checkpointer.chiudi_checkpointer)
        self.assertIs(checkpointer.get_checkpointer(), saver)
        self.assertIs(await checkpointer.apri_checkpointer(URL), saver)
        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
        from langgraph.checkpoint.base import empty_checkpoint
        await saver.aput(config, empty_checkpoint(), {"source": "input", "step": 0}, {})
        self.assertIsNotNone(await saver.aget_tuple(config))
        await checkpointer.svuota_checkpoint()
        self.assertIsNone(await saver.aget_tuple(config))
