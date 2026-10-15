"""
Errori del database degli eventi: EventLog li rilancia invece di ingoiarli, l'API risponde 503 senza dichiarare
riuscita un'operazione non avvenuta, il Brain completa il ciclo e avvisa l'operatore.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.db.database import Database
from app.graph.orchestrator import BrainAgent
from app.graph.state import EscalationItem
from app.tools.event_log import EventLog
from app.tools.sensor_tools import IoTDeviceTool

# A database in a folder that does not exist: every connection fails.
UNREACHABLE = str(Path(tempfile.gettempdir()) / "langbrain_missing_folder_win" / "events.db")


class EventLogErrorsTest(unittest.IsolatedAsyncioTestCase):
    async def test_database_errors_are_raised(self):
        log = EventLog(db_path=UNREACHABLE)
        with self.assertRaises(SQLAlchemyError):
            await log.mark_resolved("lampada")
        with self.assertRaises(SQLAlchemyError):
            await log.unblock_target("lampada", "prova")
        with self.assertRaises(SQLAlchemyError):
            await log.expire_old_control_flags(ttl_minutes=0)


class EventLogDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.mkdtemp(prefix="eventlog_win_")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db_path = os.path.join(self.folder, "events.db")
        await Database(db_path=self.db_path).init_db()
        self.log = EventLog(db_path=self.db_path)

    async def test_mark_resolved_returns_how_many_escalations_it_closed(self):
        await self.log.log_event("a", "ESCALATION_PROPOSED", "lampada", "OFF", "ON", "r", True)
        await self.log.log_event("b", "ESCALATION_PROPOSED", "lampada", "OFF", "ON", "r", True)
        self.assertEqual(await self.log.mark_resolved("lampada"), 2)
        self.assertEqual(await self.log.mark_resolved("lampada"), 0)

    async def test_expiry_skips_only_actions_already_expired_or_resolved(self):
        async with aiosqlite.connect(self.db_path) as db:
            for action in ("REJECTED_ON", "EXPIRED_REJECTED_ON", "RESOLVED_REJECTED_ON"):
                await db.execute("INSERT INTO events (actor, action, target, old_value, new_value, reasoning, escalated, timestamp) "
                                 "VALUES ('a', ?, 'lampada', 'x', 'y', 'r', 0, '2000-01-01 00:00:00')", (action,))
            await db.commit()
        self.assertEqual(await self.log.expire_old_control_flags(ttl_minutes=1), 1)
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT action FROM events ORDER BY action") as cursor:
                actions = [row[0] for row in await cursor.fetchall()]
        # REJECTED_ON expired; the already expired and the resolved ones are left as they are (no EXPIRED_EXPIRED_...).
        self.assertEqual(actions, ["EXPIRED_REJECTED_ON", "EXPIRED_REJECTED_ON", "RESOLVED_REJECTED_ON"])


class EventEndpointsErrorsTest(unittest.TestCase):
    """Nessun lifespan: gli endpoint creano EventLog sul percorso patchato, irraggiungibile."""

    def setUp(self):
        from app.api.main import app

        self.tool = IoTDeviceTool("lampada", "REJECTED")
        for bersaglio in (
            patch("app.api.main._shared_tools", {"lampada": self.tool}),
            patch("app.api.main.EventLog", lambda *a, **kw: EventLog(*a, db_path=UNREACHABLE, **kw)),
            patch.dict(os.environ, {"API_KEY": ""}),
        ):
            bersaglio.start()
            self.addCleanup(bersaglio.stop)
        self.client = TestClient(app)

    def test_unblock_answers_503_and_leaves_the_device_untouched(self):
        risposta = self.client.post("/events/unblock", json={"target": "lampada", "reasoning": "prova"})
        self.assertEqual(risposta.status_code, 503)
        self.assertRegex(risposta.json()["detail"], "Nulla è stato modificato|Nothing was changed")
        self.assertEqual(self.tool._current_value, "REJECTED")

    def test_reset_conflicts_answers_503(self):
        risposta = self.client.delete("/events/reset-conflicts/lampada")
        self.assertEqual(risposta.status_code, 503)


class BrainClosingErrorsTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_brain_completes_the_cycle_and_warns_the_operator(self):
        tool = IoTDeviceTool("lampada", "OFF")
        brain = BrainAgent(tools=[tool], sub_agent_names=[])
        brain.event_log.log_event = AsyncMock()
        brain.event_log.get_recent_events = AsyncMock(return_value=[])
        brain.event_log.mark_resolved = AsyncMock(side_effect=aiosqlite.OperationalError("database is locked"))
        brain.ask_brain = AsyncMock(return_value="DECISIONE: APPROVA\nMOTIVAZIONE: ok")
        escalation = EscalationItem(source_agent="figlio", target_device="lampada", proposed_action="ON",
                                    reason="prova", conflict_detected=True).model_dump()
        stato = {"messages": [], "readings": [], "recent_events": [], "next_agent": "brain", "hitl_required": False,
                 "config": {}, "pending_escalations": [escalation]}

        with patch("app.graph.orchestrator.flusso_brain_attivo", return_value=False):
            risultato = await brain.process(stato, [], [], [escalation])

        testo = "\n".join(m.content for m in risultato["messages"])
        self.assertIn("database is locked", testo)
        self.assertIn("APPROVATA", testo)
        self.assertEqual(await tool.get_tool_value(), "ON")


if __name__ == "__main__":
    unittest.main()
