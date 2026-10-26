"""check_body_status valuta solo i segnali della finestra del run (prima ignorava `state`)."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from app.graph.orchestrator import BrainAgent


def segnale(nome: str, ore_fa: float | None) -> dict:
    voce = {"sensor_id": nome, "agent_owner": "brain", "value": "21", "unit": "C"}
    if ore_fa is not None:
        voce["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=ore_fa)).isoformat()
    return voce


def stato(config: dict) -> dict:
    return {"messages": [], "readings": [], "recent_events": [], "pending_escalations": [], "next_agent": "END",
            "hitl_required": False, "config": config}


class CheckBodyStatusTest(unittest.IsolatedAsyncioTestCase):
    async def chiama(self, config: dict) -> str:
        brain = BrainAgent(tools=[])
        brain.ask_brain = AsyncMock(return_value="STATUS: OK")
        letture = [segnale("recente", 0.5), segnale("vecchio", 10), segnale("senza_data", None)]
        await brain.check_body_status(stato(config), letture, [])
        return brain.ask_brain.await_args.args[1]

    async def test_scarta_i_segnali_oltre_la_finestra_di_default(self):
        prompt = await self.chiama({})
        self.assertIn("recente", prompt)
        self.assertIn("senza_data", prompt)       # non databile: resta
        self.assertNotIn("vecchio", prompt)

    async def test_la_finestra_del_run_si_legge_dallo_stato(self):
        self.assertIn("vecchio", await self.chiama({"signals_window_hours": 24}))
        self.assertNotIn("vecchio", await self.chiama({"readings_window_hours": 1}))


if __name__ == "__main__":
    unittest.main()
