import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.base_agent import BaseAgent
from app.agents.dynamic_agent import DynamicAgent
from app.graph.builder import _resolve_registered_node, build_graph, wrap_node_with_hitl
from app.graph.orchestrator import BrainAgent


def graph_state(**overrides):
    state = {
        "messages": [],
        "readings": [],
        "recent_events": [],
        "pending_escalations": [],
        "next_agent": "brain",
        "hitl_required": False,
        "config": {},
    }
    state.update(overrides)
    return state


class RecordingAgent(BaseAgent):
    def __init__(self):
        super().__init__("recording_agent", ["device"], 30, 1.0)
        self.received_events = []

    async def process(self, state, recent_events, relevant_readings, agent_escalations):
        self.received_events = recent_events
        return {"next_agent": "END"}


class BlockingRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.graph.hitl_config import hitl_manager

        hitl_manager.update_config(
            hitl_all=False,
            hitl_nodes=[],
            hitl_targets=[],
            hitl_actions=[],
            max_wait_seconds=None,
        )

    async def test_wrapper_uses_base_agent_call_and_loads_db_events(self):
        agent = RecordingAgent()
        db_events = [{"target": "device", "action": "FORCE_SHUTDOWN"}]
        agent.event_log.get_recent_events = AsyncMock(return_value=db_events)

        result = await wrap_node_with_hitl("recording_agent", agent)(graph_state())

        self.assertEqual(result["next_agent"], "END")
        self.assertEqual(agent.received_events, db_events)
        agent.event_log.get_recent_events.assert_awaited_once()

    async def test_mao_awaits_async_openai_completion(self):
        from app.MAO.model_access_object import Mao
        from openai import AsyncOpenAI

        mao = Mao()
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "OK"
        client = mao.providers["openrouter"]["client"]
        mao.providers["openrouter"]["enabled"] = True
        self.assertIsInstance(client, AsyncOpenAI)

        completion = AsyncMock(return_value=response)
        try:
            with patch.object(client.chat.completions, "create", completion):
                result = await mao.call_model(
                    "Rispondi soltanto OK.",
                    "Test asincrono.",
                    provider="openrouter",
                    fallback_on_error=False,
                )
            self.assertEqual(result, "OK")
            completion.assert_awaited_once()
        finally:
            await mao.aclose()

    async def test_mao_skips_remote_fallbacks_without_real_credentials(self):
        from app.MAO.model_access_object import Mao

        with patch.dict(
            "os.environ",
            {"GEMINI_API_KEY": "nessuna", "OPENROUTER_API_KEY": "nessuna"},
        ):
            mao = Mao()
        execute = AsyncMock(side_effect=ConnectionError("local unavailable"))
        try:
            with patch.object(mao, "_execute_chat", execute):
                with self.assertRaisesRegex(RuntimeError, "Nessun provider LLM disponibile"):
                    await mao.call_model("system", "user", provider="local")
            self.assertEqual(execute.await_count, 1)
            self.assertEqual(execute.await_args.kwargs["provider_key"], "local")
        finally:
            await mao.aclose()

    async def test_mao_timeout_defaults_to_40_seconds_and_is_configurable(self):
        from app.MAO.model_access_object import Mao

        with patch.dict("os.environ", {"MAO_TIMEOUT_SECONDS": "40"}):
            mao = Mao()
        try:
            self.assertEqual(mao.timeout_seconds, 40.0)
            self.assertEqual(mao.http_client.timeout.read, 40.0)
        finally:
            await mao.aclose()

    async def test_health_check_does_not_report_ok_for_control_flags(self):
        brain = BrainAgent(tools=[])
        brain.ask_brain = AsyncMock(return_value="STATUS: OK")
        readings = [
            {"sensor_id": "ac_living_room", "value": "REJECTED", "unit": "status"}
        ]

        result = await brain.check_body_status(graph_state(), readings, [])

        message = result["messages"][0].content
        self.assertIn("MACRO_ADJUSTMENT_REQUIRED", message)
        self.assertIn("ac_living_room=REJECTED", message)
        brain.ask_brain.assert_not_awaited()

    async def test_llm_proxy_returns_503_without_unhandled_traceback(self):
        from fastapi import HTTPException
        from app.api.main import LlmProxyRequest, invoke_llm

        fake_mao = MagicMock()
        fake_mao.default_provider = "local"
        fake_mao.call_model = AsyncMock(
            side_effect=RuntimeError("Nessun provider LLM disponibile ha completato la richiesta.")
        )
        fake_mao.aclose = AsyncMock()

        with patch("app.MAO.model_access_object.Mao", return_value=fake_mao):
            with self.assertRaises(HTTPException) as raised:
                await invoke_llm(
                    LlmProxyRequest(
                        system_prompt="system",
                        user_prompt="user",
                        provider="local",
                    )
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertFalse(fake_mao.call_model.await_args.kwargs["fallback_on_error"])
        fake_mao.aclose.assert_awaited_once()

    async def test_parent_receives_child_escalation_and_routes_to_brain(self):
        parent = DynamicAgent(
            name="organ_security",
            managed_targets=["front_door_lock"],
            parent_agent_name="Brain",
            sub_agent_names=["component_door_lock"],
            tools={},
        )
        escalation = {
            "source_agent": "component_door_lock",
            "target_device": "front_door_lock",
            "proposed_action": "LOCKED",
            "reason": "test",
        }
        parent.event_log.get_recent_events = AsyncMock(return_value=[])

        result = await parent(graph_state(pending_escalations=[escalation], next_agent="organ_security"))

        self.assertEqual(result["next_agent"], "brain")
        self.assertIn("inoltrata", result["messages"][0].content)

    async def test_parent_with_direct_targets_delegates_each_child_once(self):
        parent = DynamicAgent(
            name="organ_security",
            managed_targets=["front_door_lock"],
            parent_agent_name="Brain",
            sub_agent_names=["component_door_lock", "component_alarm"],
            tools={},
        )

        first = await parent.process(graph_state(next_agent="organ_security"), [], [], [])
        self.assertEqual(first["next_agent"], "component_door_lock")

        second_state = graph_state(next_agent="organ_security", config={"_hierarchy_visited": ["organ_security", "component_door_lock"]})
        second = await parent.process(second_state, [], [], [])
        self.assertEqual(second["next_agent"], "component_alarm")

        completed_state = graph_state(
            next_agent="organ_security",
            config={"_hierarchy_visited": ["organ_security", "component_door_lock", "component_alarm"]},
        )
        with patch.object(parent, "ask_brain", return_value="DECISIONE: NONE\nMOTIVAZIONE: stabile"):
            completed = await parent.process(completed_state, [], [], [])
        self.assertEqual(completed["next_agent"], "brain")

    def test_router_resolves_brain_case_insensitively(self):
        nodes = {"brain", "organ_security"}
        self.assertEqual(_resolve_registered_node("Brain", nodes), "brain")
        self.assertEqual(_resolve_registered_node("ORGAN_SECURITY", nodes), "organ_security")

    async def test_brain_ends_after_registered_hierarchy_returns(self):
        brain = BrainAgent(tools=[], sub_agent_names=["organ_security"])
        state = graph_state(
            next_agent="brain",
            config={"_hierarchy_visited": ["organ_security", "component_alarm"]},
        )

        result = await brain.process(state, [], [], [])

        self.assertEqual(result["next_agent"], "END")

    async def test_brain_never_routes_to_an_unregistered_default(self):
        brain = BrainAgent(tools=[], sub_agent_names=["organ_security"])

        initial = await brain.process(graph_state(), [], [], [])
        unknown = await brain.process(graph_state(next_agent="agent_climate"), [], [], [])

        self.assertEqual(initial["next_agent"], "organ_security")
        self.assertEqual(unknown["next_agent"], "END")

    async def test_brain_can_route_directly_to_a_registered_component(self):
        brain = BrainAgent(
            tools=[],
            sub_agent_names=["organ_security"],
            registered_agent_names=["organ_security", "component_alarm"],
        )

        result = await brain.process(graph_state(next_agent="COMPONENT_ALARM"), [], [], [])

        self.assertEqual(result["next_agent"], "component_alarm")

    async def test_brain_with_no_registered_sub_agents_ends(self):
        brain = BrainAgent(tools=[], sub_agent_names=[])

        result = await brain.process(graph_state(), [], [], [])

        self.assertEqual(result["next_agent"], "END")

    async def test_graph_with_explicit_empty_registry_does_not_add_climate_agent(self):
        graph, _ = build_graph(custom_agent_instances={})

        self.assertNotIn("agent_climate", graph.get_graph().nodes)
        with patch(
            "app.tools.event_log.EventLog.get_recent_events",
            AsyncMock(return_value=[]),
        ):
            result = await graph.ainvoke(
                graph_state(),
                config={"configurable": {"thread_id": "empty-registry-regression"}},
            )

        self.assertEqual(result["next_agent"], "END")


if __name__ == "__main__":
    unittest.main()
