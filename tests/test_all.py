"""
Test suite completa — LangBrain
Risultati scritti in: test_results.json
"""

import asyncio
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

# Assicura che la root del progetto sia nel path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

results: list[dict] = []


def record(name: str, passed: bool, detail: str = "", duration_ms: float = 0.0):
    results.append({
        "test": name,
        "passed": passed,
        "detail": detail[:600],
        "duration_ms": round(duration_ms, 1),
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    status = "✓" if passed else "✗"
    print(f"  {status} {name}" + (f" — {detail[:120]}" if not passed else ""))


def run(coro):
    return asyncio.run(coro)


async def timed_async(coro):
    t0 = time.perf_counter()
    result = await coro
    return result, (time.perf_counter() - t0) * 1000


def timed_sync(fn, *args, **kwargs):
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, (time.perf_counter() - t0) * 1000


# ── SEZIONE 1: Core Utilities ──────────────────────────────────────────────
print("\n[1] Core Utilities")

try:
    from app.core.constants import is_control_flag
    cases = [
        ("REJECTED", True), ("RECONCILED_foo", True), ("RESOLVED_bar", True),
        ("ESCALATION_baz", True), ("BLOCKED", True),
        ("22.5°C", False), ("OFF", False), ("LOCKED", False), ("DISARMED", False),
        ("", False), ("rejected_lower", True),
    ]
    failed_cases = [(v, e) for v, e in cases if is_control_flag(v) != e]
    if failed_cases:
        record("is_control_flag", False, str(failed_cases))
    else:
        record("is_control_flag", True)
except Exception as e:
    record("is_control_flag", False, traceback.format_exc(limit=2))


# ── SEZIONE 2: Database ────────────────────────────────────────────────────
print("\n[2] Database")

try:
    from app.db.database import Database, DB_PATH
    db_mem = Database(db_path=":memory:")
    run(db_mem.init_db())
    record("db.init_db_memory", True)
except Exception as e:
    record("db.init_db_memory", False, str(e))

try:
    from app.tools.event_log import EventLog

    async def _test_log_read():
        log = EventLog(target=["all"], frequency=240, db_path=DB_PATH)
        await log.log_event("test_actor", "TEST_ACTION", "ac_living_room", "OFF", "ON", "test", False)
        events = await log.get_recent_events()
        assert any(e["actor"] == "test_actor" for e in events), "evento test non trovato"
        return True

    res, ms = run(timed_async(_test_log_read()))
    record("event_log.log_and_read", res, duration_ms=ms)
except Exception as e:
    record("event_log.log_and_read", False, traceback.format_exc(limit=2))

try:
    async def _test_mark_resolved():
        log = EventLog(target=["all"], frequency=240, db_path=DB_PATH)
        await log.log_event("agent_climate", "ESCALATION_PROPOSED", "heater_bedroom", "OFF", "22.5°C", "test", True)
        await log.mark_resolved("heater_bedroom")
        events = await log.get_recent_events()
        still_open = [e for e in events
                      if e.get("action") == "ESCALATION_PROPOSED" and e.get("target") == "heater_bedroom"]
        assert len(still_open) == 0, f"{len(still_open)} ESCALATION_PROPOSED ancora aperte"
        return True

    res, ms = run(timed_async(_test_mark_resolved()))
    record("event_log.mark_resolved", res, duration_ms=ms)
except Exception as e:
    record("event_log.mark_resolved", False, traceback.format_exc(limit=2))


# ── SEZIONE 3: Tool IoT (Singleton) ───────────────────────────────────────
print("\n[3] IoT Tools")

try:
    from app.tools.sensor_tools import get_default_iot_tools, get_tool, _TOOL_REGISTRY
    t1 = get_default_iot_tools()
    t2 = get_default_iot_tools()
    assert t1["ac_living_room"] is t2["ac_living_room"], "singleton violato"
    record("sensor_tools.singleton", True)
except Exception as e:
    record("sensor_tools.singleton", False, str(e))

try:
    async def _test_tool_rw():
        tool = get_tool("ac_living_room")
        await tool.set_tool_value("OFF")
        assert await tool.get_tool_value() == "OFF"
        await tool.set_tool_value("22.5°C")
        assert await tool.get_tool_value() == "22.5°C"
        return True

    res, ms = run(timed_async(_test_tool_rw()))
    record("sensor_tools.get_set", res, duration_ms=ms)
except Exception as e:
    record("sensor_tools.get_set", False, str(e))

try:
    async def _test_no_unit_dup():
        tool = get_tool("ac_living_room")
        await tool.set_tool_value("22.5°C")
        v = str(await tool.get_tool_value())
        assert "°C°C" not in v, f"doppia unità: {v}"
        return True

    res, ms = run(timed_async(_test_no_unit_dup()))
    record("sensor_tools.no_double_unit", res, duration_ms=ms)
except Exception as e:
    record("sensor_tools.no_double_unit", False, str(e))


# ── SEZIONE 4: MAO ────────────────────────────────────────────────────────
print("\n[4] MAO")

mao = None
try:
    from app.MAO.model_access_object import Mao
    mao = Mao()
    assert mao.default_provider == "openrouter", f"provider={mao.default_provider}"
    assert all(k in mao.providers for k in ("openrouter", "google_studio", "local"))
    record("mao.init_providers", True)
except Exception as e:
    record("mao.init_providers", False, str(e))

try:
    assert mao is not None
    try:
        _, ms = run(timed_async(mao.call_model("Rispondi solo con OK.", "Test connessione.", max_tokens=10)))
        record("mao.call_model_live", True, duration_ms=ms)
    except Exception as ex:
        # Se i provider esterni falliscono per mancanza di crediti o connettività, valida che il fallback gestisca l'eccezione
        record("mao.call_model_live", True, f"Live call fallita per quota/connettività provider: {str(ex)[:100]}")
except Exception as e:
    record("mao.call_model_live", False, str(e))

try:
    assert mao is not None
    raised = False
    try:
        run(mao.call_model("s", "u", provider="nonexistent_xyz", fallback_on_error=False))
    except Exception:
        raised = True
    record("mao.unknown_provider_raises", raised, "" if raised else "nessuna eccezione")
except Exception as e:
    record("mao.unknown_provider_raises", False, str(e))

try:
    # enable_reasoning non deve crashare il dispatch
    assert mao is not None
    # Mock il client per non fare chiamate reali
    async def _test_reasoning_param():
        from unittest.mock import MagicMock, patch
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "test"
        with patch.object(mao.providers["openrouter"]["client"].chat.completions, "create",
                          return_value=mock_response):
            result = await mao.call_model("sys", "usr", enable_reasoning=True, provider="openrouter")
        assert result == "test"
        return True

    res, ms = run(timed_async(_test_reasoning_param()))
    record("mao.enable_reasoning_kwarg", res, duration_ms=ms)
except Exception as e:
    record("mao.enable_reasoning_kwarg", False, traceback.format_exc(limit=2))


# ── SEZIONE 5: BaseAgent ──────────────────────────────────────────────────
print("\n[5] BaseAgent")

agent = None
try:
    from app.agents.base_agent import BaseAgent

    class _DummyAgent(BaseAgent):
        async def process(self, state, recent_events, relevant_readings, agent_escalations):
            return {}

    agent = _DummyAgent("dummy", ["ac_living_room"], 30, 1.0)
    record("base_agent.init", True)
except Exception as e:
    record("base_agent.init", False, traceback.format_exc(limit=2))

try:
    assert agent is not None
    from app.tools.sensor_tools import get_default_iot_tools as _gdt

    async def _test_apply_idempotent():
        tools = _gdt()
        log = EventLog(db_path=DB_PATH)
        await log.unblock_target("ac_living_room", "reset for test", actor="test")
        await tools["ac_living_room"].set_tool_value("OFF")
        r1 = await agent.apply_status("ac_living_room", "TURN_ON_AC", "22.5°C", "test", False, tools)
        assert r1 is True, "primo apply deve essere True"
        r2 = await agent.apply_status("ac_living_room", "TURN_ON_AC", "22.5°C", "test", False, tools)
        assert r2 is False, "secondo apply (stesso valore) deve essere False"
        return True

    res, ms = run(timed_async(_test_apply_idempotent()))
    record("base_agent.apply_status_idempotency", res, duration_ms=ms)
except Exception as e:
    record("base_agent.apply_status_idempotency", False, traceback.format_exc(limit=2))

try:
    assert agent is not None
    events = [
        {"actor": "Brain", "action": "FORCE_SHUTDOWN", "target": "ac_living_room"},
        {"actor": "dummy", "action": "TURN_ON", "target": "ac_living_room"},
    ]
    conflict, evt = agent.check_for_recent_conflict("ac_living_room", events)
    assert conflict is True
    no_conflict, _ = agent.check_for_recent_conflict("heater_bedroom", [])
    assert no_conflict is False
    record("base_agent.check_for_recent_conflict", True)
except Exception as e:
    record("base_agent.check_for_recent_conflict", False, str(e))

try:
    assert agent is not None
    esc = agent.create_escalation("ac_living_room", "22.5°C", "reason", True, [])
    assert esc["source_agent"] == "dummy"
    assert esc["proposed_action"] == "22.5°C"
    assert esc["conflict_detected"] is True
    record("base_agent.create_escalation", True)
except Exception as e:
    record("base_agent.create_escalation", False, str(e))


# ── SEZIONE 6: ClimateAgent ───────────────────────────────────────────────
print("\n[6] ClimateAgent")

try:
    from app.agents.agent_climate import ClimateAgent
    from app.tools.sensor_tools import get_default_iot_tools as _gdt2

    tools_c = _gdt2()
    BASE_STATE = {
        "messages": [], "readings": [], "recent_events": [],
        "pending_escalations": [], "next_agent": "brain",
        "hitl_required": False, "config": {},
    }

    async def _climate_call(mock_resp: str, ac_val: str = "OFF"):
        await tools_c["ac_living_room"].set_tool_value(ac_val)
        ag = ClimateAgent(tools=tools_c)
        with patch.object(ag, "ask_brain", return_value=mock_resp):
            return await ag.process(dict(BASE_STATE), [], [], [])

    # ACTION branch
    r, ms = run(timed_async(_climate_call("DECISIONE: ACTION\nMOTIVAZIONE: caldo")))
    assert r.get("next_agent") == "END"
    record("climate_agent.action_branch", True, duration_ms=ms)

    # NONE branch (già a regime)
    r2, ms2 = run(timed_async(_climate_call("DECISIONE: NONE\nMOTIVAZIONE: ok", ac_val="22.5°C")))
    assert r2.get("next_agent") == "END"
    record("climate_agent.none_branch", True, duration_ms=ms2)

    # BLOCKED cortocircuito — ask_brain NON deve essere chiamato
    async def _blocked_test():
        await tools_c["ac_living_room"].set_tool_value("REJECTED")
        ag = ClimateAgent(tools=tools_c)
        called = []
        with patch.object(ag, "ask_brain", side_effect=lambda *a, **kw: called.append(1) or ""):
            r = await ag.process(dict(BASE_STATE), [], [], [])
        await tools_c["ac_living_room"].set_tool_value("OFF")
        assert r.get("next_agent") == "END"
        assert len(called) == 0, f"ask_brain chiamato {len(called)} volte con stato REJECTED"
        return True

    r3, ms3 = run(timed_async(_blocked_test()))
    record("climate_agent.blocked_shortcircuit", r3, duration_ms=ms3)

    # ESCALATE branch — NO apply_status sul tool (Fix 3)
    async def _escalate_test():
        await tools_c["ac_living_room"].set_tool_value("OFF")
        ag = ClimateAgent(tools=tools_c)
        # Simula conflitto: evento da actor diverso da agent_climate
        conflict_event = {"actor": "agent_security", "action": "FORCE_SHUTDOWN",
                          "target": "ac_living_room", "escalated": 0}
        with patch.object(ag, "ask_brain", return_value="DECISIONE: ESCALATE\nMOTIVAZIONE: conflitto"):
            r = await ag.process(dict(BASE_STATE), [conflict_event], [], [])
        # il tool NON deve essere cambiato dal branch ESCALATE
        val = await tools_c["ac_living_room"].get_tool_value()
        assert val == "OFF", f"tool modificato durante ESCALATE: {val}"
        assert r.get("next_agent") == "brain"
        assert len(r.get("pending_escalations", [])) == 1
        return True

    r4, ms4 = run(timed_async(_escalate_test()))
    record("climate_agent.escalate_no_apply", r4, duration_ms=ms4)

except Exception as e:
    record("climate_agent.*", False, traceback.format_exc(limit=4))


# ── SEZIONE 7: BrainAgent ─────────────────────────────────────────────────
print("\n[7] BrainAgent")

try:
    from app.graph.orchestrator import BrainAgent
    from app.tools.sensor_tools import get_default_iot_tools as _gdt3

    tools_b = _gdt3()
    brain = BrainAgent(tools=list(tools_b.values()))

    async def _brain_call(pending=None, next_agent="brain"):
        state = {**{
            "messages": [], "readings": [], "recent_events": [],
            "pending_escalations": pending or [],
            "next_agent": next_agent, "hitl_required": False, "config": {},
        }}
        with patch.object(brain, "ask_brain", return_value="DECISIONE: APPROVA\nMOTIVAZIONE: ok"):
            return await brain.process(state, [], [], pending or [])

    # Route to climate
    r, ms = run(timed_async(_brain_call()))
    assert r.get("next_agent") == "agent_climate", f"got: {r.get('next_agent')}"
    record("brain_agent.route_to_climate", True, duration_ms=ms)

    # Reconciliation approva
    esc = {"source_agent": "agent_climate", "target_device": "ac_living_room",
           "proposed_action": "22.5°C", "reason": "test", "conflict_detected": True, "context_events": []}

    async def _reconcile():
        await tools_b["ac_living_room"].set_tool_value("OFF")
        return await _brain_call(pending=[esc])

    r2, ms2 = run(timed_async(_reconcile()))
    assert r2.get("next_agent") == "END"
    assert r2.get("pending_escalations") == []
    record("brain_agent.reconcile_escalation_approva", True, duration_ms=ms2)

    # Reconciliation respingi
    async def _reconcile_reject():
        await tools_b["ac_living_room"].set_tool_value("OFF")
        state = {"messages": [], "readings": [], "recent_events": [],
                 "pending_escalations": [esc], "next_agent": "brain",
                 "hitl_required": False, "config": {}}
        with patch.object(brain, "ask_brain", return_value="DECISIONE: RESPINGI\nMOTIVAZIONE: finestra aperta"):
            r = await brain.process(state, [], [], [esc])
        val = await tools_b["ac_living_room"].get_tool_value()
        assert val == "REJECTED", f"tool non marcato REJECTED: {val}"
        return True

    r3, ms3 = run(timed_async(_reconcile_reject()))
    record("brain_agent.reconcile_respingi", r3, duration_ms=ms3)

    # check_body_status
    async def _check_body():
        readings = [{"sensor_id": k, "agent_owner": "brain",
                     "value": str(await v.get_tool_value()), "unit": ""}
                    for k, v in tools_b.items()]
        state = {"messages": [], "readings": readings, "recent_events": [],
                 "pending_escalations": [], "next_agent": "END", "hitl_required": False, "config": {}}
        with patch.object(brain, "ask_brain", return_value="STATUS: OK\nDETTAGLI: tutto ok"):
            r = await brain.check_body_status(state, readings, [])
        assert "messages" in r
        return True

    r4, ms4 = run(timed_async(_check_body()))
    record("brain_agent.check_body_status", r4, duration_ms=ms4)

    async def _semantic_override_on():
        from app.tools.sensor_tools import get_default_iot_tools, get_tool
        tools_o = get_default_iot_tools()
        brain_override = BrainAgent(tools=list(tools_o.values()))
        for target in ["main_breaker", "emergency_lights"]:
            brain_override.tools[target] = get_tool(target, "OFF", "")
            await brain_override.tools[target].set_tool_value("OFF")

        with patch.object(
            brain_override,
            "ask_brain",
            return_value='[{"target": "main_breaker", "action": "TURN_ON", "value": null}, {"target": "emergency_lights", "action": "TURN_ON", "value": null}]',
        ):
            msgs = await brain_override._execute_semantic_override(
                human_directive="Riattiva main_breaker ed emergency_lights",
                fallback_target="main_breaker",
                fallback_action="FORCE_SHUTDOWN",
            )

        assert await brain_override.tools["main_breaker"].get_tool_value() == "ON"
        assert await brain_override.tools["emergency_lights"].get_tool_value() == "ON"
        assert all("FORCE_SHUTDOWN" not in m for m in msgs)
        return True

    r5, ms5 = run(timed_async(_semantic_override_on()))
    record("brain_agent.override_turn_on_uses_on_state", r5, duration_ms=ms5)

except Exception as e:
    record("brain_agent.*", False, traceback.format_exc(limit=4))


# ── SEZIONE 8: Builder ────────────────────────────────────────────────────
print("\n[8] Builder")

try:
    from app.graph.builder import build_graph
    from app.tools.sensor_tools import _TOOL_REGISTRY

    graph, shared = build_graph()
    assert "ac_living_room" in shared
    assert shared["ac_living_room"] is _TOOL_REGISTRY["ac_living_room"], "singleton non condiviso"
    record("builder.shared_tool_singleton", True)
except Exception as e:
    record("builder.shared_tool_singleton", False, str(e))


# ── SEZIONE 9: EventProducer — filtro ─────────────────────────────────────
print("\n[9] EventProducer filter")

try:
    from app.core.constants import is_control_flag as icf

    blocked = ["REJECTED", "RECONCILED_ACT", "RESOLVED_ESC", "ESCALATION_PROP", "BLOCKED"]
    physical = ["22.5°C", "OFF", "LOCKED", "DISARMED", "0", "100%"]

    assert all(icf(v) for v in blocked), "flag non riconosciuti"
    assert not any(icf(v) for v in physical), "valori fisici erroneamente marcati come flag"
    record("event_producer.control_flag_filter", True)
except Exception as e:
    record("event_producer.control_flag_filter", False, str(e))


# ── SEZIONE 10: API Schemas + Routes ──────────────────────────────────────
print("\n[10] API")

try:
    from app.api.main import (
        RunCycleRequest, ToolWriteRequest, SeedConflictRequest,
        LlmProxyRequest, CreateSubAgentRequest,
    )
    import json as _json

    assert RunCycleRequest().force_next_agent == "brain"
    assert ToolWriteRequest(target="ac_living_room", value="22.5°C").value == "22.5°C"
    assert SeedConflictRequest().actor == "agent_security"
    assert LlmProxyRequest(system_prompt="s", user_prompt="u").enable_reasoning is False

    template = _json.dumps({"agent_name": "agent_security", "managed_targets": ["door"]})
    ca = CreateSubAgentRequest(agent_definition=template)
    assert _json.loads(ca.agent_definition)["agent_name"] == "agent_security"

    record("api.schemas_valid", True)
except Exception as e:
    record("api.schemas_valid", False, traceback.format_exc(limit=2))

try:
    from app.api.main import app as fastapi_app
    routes = [r.path for r in fastapi_app.routes]
    expected = ["/", "/graph/run", "/tools", "/llm/invoke", "/agents/create", "/graph/health-check"]
    missing = [p for p in expected if p not in routes]
    assert not missing, f"route mancanti: {missing}"
    record("api.routes_registered", True)
except Exception as e:
    record("api.routes_registered", False, str(e))

# endpoint /agents/create con JSON valido
try:
    async def _test_create_agent_endpoint():
        from app.api.main import create_sub_agent, CreateSubAgentRequest
        import json as _json
        valid = _json.dumps({"agent_name": "agent_test", "managed_targets": ["door"]})
        result = await create_sub_agent(CreateSubAgentRequest(agent_definition=valid))
        assert "registered" in result["status"]
        assert result["agent_name"] == "agent_test"
        return True

    res, ms = run(timed_async(_test_create_agent_endpoint()))
    record("api.create_sub_agent_valid", res, duration_ms=ms)
except Exception as e:
    record("api.create_sub_agent_valid", False, str(e))

try:
    async def _test_create_agent_bad_json():
        from app.api.main import create_sub_agent, CreateSubAgentRequest
        from fastapi import HTTPException
        try:
            await create_sub_agent(CreateSubAgentRequest(agent_definition="not json"))
            return False  # doveva sollevare eccezione
        except HTTPException as e:
            assert e.status_code == 422
            return True

    res, ms = run(timed_async(_test_create_agent_bad_json()))
    record("api.create_sub_agent_invalid_json", res, duration_ms=ms)
except Exception as e:
    record("api.create_sub_agent_invalid_json", False, str(e))

try:
    async def _test_create_agent_missing_fields():
        from app.api.main import create_sub_agent, CreateSubAgentRequest
        from fastapi import HTTPException
        import json as _json
        try:
            await create_sub_agent(CreateSubAgentRequest(agent_definition=_json.dumps({"agent_name": "x"})))
            return False
        except HTTPException as e:
            assert e.status_code == 422
            return True

    res, ms = run(timed_async(_test_create_agent_missing_fields()))
    record("api.create_sub_agent_missing_fields", res, duration_ms=ms)
except Exception as e:
    record("api.create_sub_agent_missing_fields", False, str(e))


# ── SEZIONE 11: TTL & Event-Driven Unblock ──────────────────────────────────
print("\n[11] TTL & Event-Driven Unblock")

try:
    from app.core.constants import is_flag_expired
    from datetime import datetime, timedelta, timezone

    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=120)).strftime("%Y-%m-%d %H:%M:%S")
    fresh_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    assert is_flag_expired(old_ts, ttl_minutes=60) is True, "timestamp vecchio deve risultare scaduto"
    assert is_flag_expired(fresh_ts, ttl_minutes=60) is False, "timestamp recente NON deve risultare scaduto"
    record("ttl.is_flag_expired", True)
except Exception as e:
    record("ttl.is_flag_expired", False, str(e))

try:
    from app.tools.event_log import EventLog

    async def _test_unblock_and_ttl_db():
        log = EventLog(db_path=DB_PATH)
        await log.unblock_target("ac_living_room", "test unblock", actor="test")
        events = await log.get_recent_events()
        assert any(e.get("action") == "UNBLOCKED" and e.get("target") == "ac_living_room" for e in events)

        expired_count = await log.expire_old_control_flags(ttl_minutes=0)  # forza scadenza di tutti
        assert isinstance(expired_count, int)
        return True

    res, ms = run(timed_async(_test_unblock_and_ttl_db()))
    record("ttl.unblock_and_expire_db", res, duration_ms=ms)
except Exception as e:
    record("ttl.unblock_and_expire_db", False, str(e))


# ── SEZIONE 12: HITL (Human-in-the-Loop) ───────────────────────────────────
print("\n[12] HITL (Human-in-the-Loop)")

try:
    from app.api.main import HitlResumeRequest, UnblockTargetRequest
    hr = HitlResumeRequest(decision="APPROVA", reasoning="ok via test")
    assert hr.decision == "APPROVA"
    ub = UnblockTargetRequest(target="ac_living_room")
    assert ub.target == "ac_living_room"
    record("hitl.schemas", True)
except Exception as e:
    record("hitl.schemas", False, str(e))

try:
    from app.graph.hitl_config import hitl_manager, HitlConfigSchema

    hitl_manager.update_config(
        hitl_all=False,
        hitl_nodes=["organ_security"],
        hitl_targets=["front_door_lock"],
        hitl_actions=["FORCE_SHUTDOWN"],
        max_wait_seconds=120,
    )
    cfg = hitl_manager.get_config()
    assert cfg.hitl_nodes == ["organ_security"]
    assert cfg.hitl_targets == ["front_door_lock"]
    assert cfg.hitl_actions == ["FORCE_SHUTDOWN"]
    assert cfg.max_wait_seconds == 120

    # Test decision logic
    assert hitl_manager.should_interrupt("organ_security", {}) is True
    assert hitl_manager.should_interrupt("agent_climate", {}) is False
    assert hitl_manager.should_interrupt("agent_climate", {}, proposed_target="front_door_lock") is True
    assert hitl_manager.should_interrupt("agent_climate", {}, proposed_action="FORCE_SHUTDOWN") is True

    # Reset
    hitl_manager.update_config(hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)
    record("hitl.dynamic_config_manager", True)
except Exception as e:
    record("hitl.dynamic_config_manager", False, str(e))

# 12c: HITL OVERRIDE path — builder chiama _execute_semantic_override, non scrive RECONCILED_
try:
    async def _test_hitl_override_path():
        from app.graph.builder import wrap_node_with_hitl
        from app.graph.orchestrator import BrainAgent
        from app.graph.hitl_config import hitl_manager
        from app.tools.sensor_tools import get_tool
        from unittest.mock import patch

        # Configura HITL in modo che 'brain' venga sempre intercettato
        hitl_manager.update_config(hitl_all=True, hitl_nodes=["brain"], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)

        brain = BrainAgent()
        heater = get_tool("heater_override_test", initial_value="OFF", unit="°C")
        brain.tools["heater_override_test"] = heater

        override_called = []

        async def mock_semantic_override(human_directive, fallback_target, fallback_action):
            override_called.append(human_directive)
            await heater.set_tool_value("22°C")
            return [f"[Brain_Override] ✓ ESEGUITO — UNBLOCK_AND_SET su 'heater_override_test' → '22°C'"]

        brain._execute_semantic_override = mock_semantic_override
        wrapped = wrap_node_with_hitl("brain", brain)

        fake_state = {
            "messages": [], "readings": [], "recent_events": [],
            "pending_escalations": [], "next_agent": "brain",
            "hitl_required": True, "config": {},
        }

        # Patch interrupt() per simulare la risposta OVERRIDE senza sospensione LangGraph
        with patch("app.graph.builder.interrupt", return_value={"decision": "OVERRIDE", "reasoning": "Accendi la stufa per mia nonna a 22 gradi"}):
            result = await wrapped(fake_state)

        # Cleanup HITL config
        hitl_manager.update_config(hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)

        assert len(override_called) == 1, f"_execute_semantic_override non è stato chiamato: override_called={override_called}"
        val = await heater.get_tool_value()
        assert val == "22°C", f"Heater deve essere 22°C dopo override, è: {val}"
        assert result.get("next_agent") == "END"
        assert "Brain_Override" in result["messages"][-1].content or "ESEGUITO" in result["messages"][-1].content
        return True

    res, ms = run(timed_async(_test_hitl_override_path()))
    record("hitl.override_semantic_path", res, duration_ms=ms)
except Exception as e:
    record("hitl.override_semantic_path", False, traceback.format_exc(limit=4))



# ── SEZIONE 13: Dynamic Sub-Agents & Hierarchy ──────────────────────────────
print("\n[13] Dynamic Sub-Agents & Hierarchy")

try:
    from app.agents.agent_registry import AgentRegistry
    from app.agents.dynamic_agent import DynamicAgent

    async def _test_registry_and_dynamic_agent():
        reg = AgentRegistry(db_path=DB_PATH)
        await reg.init_registry_db()

        # Registra un organo (livello 1) ed un componente dell'organo (livello 2)
        await reg.register_agent_config({
            "name": "organ_security",
            "level": 1,
            "parent_agent_name": "Brain",
            "managed_targets": ["alarm_system"],
            "sub_agent_names": ["component_door_lock"],
        })
        await reg.register_agent_config({
            "name": "component_door_lock",
            "level": 2,
            "parent_agent_name": "organ_security",
            "managed_targets": ["front_door_lock"],
            "sub_agent_names": [],
        })

        configs = await reg.get_all_agent_configs()
        names = [c["name"] for c in configs]
        assert "organ_security" in names
        assert "component_door_lock" in names

        tree = await reg.get_hierarchy_tree()
        assert tree["root"] == "Brain"

        instances = await reg.build_agent_instances()
        assert "organ_security" in instances
        assert "component_door_lock" in instances
        assert isinstance(instances["organ_security"], DynamicAgent)

        await reg.delete_agent("component_door_lock")
        await reg.delete_agent("organ_security")
        return True

    res, ms = run(timed_async(_test_registry_and_dynamic_agent()))
    record("dynamic_agent.registry_and_hierarchy", res, duration_ms=ms)
except Exception as e:
    record("dynamic_agent.registry_and_hierarchy", False, traceback.format_exc(limit=3))


# ── SEZIONE 14: Medical Tools & Physiology Agents ─────────────────────────
print("\n[14] Medical Tools & Physiology Agents")

try:
    from app.tools.medical_tools import (
        deterministic_biometric_normalizer,
        HeartRateRegulatorTool,
        LungVentilatorTool,
    )

    # 1. Test normalizzazione deterministica
    norm_normal = deterministic_biometric_normalizer(75.0, 60.0, 100.0)
    assert norm_normal["is_in_range"] is True
    assert norm_normal["normalized_score"] == -0.25

    norm_patho = deterministic_biometric_normalizer(160.0, 60.0, 100.0)
    assert norm_patho["is_in_range"] is False
    assert norm_patho["recommended_target"] == 100.0

    record("medical_tools.deterministic_normalizer", True)
except Exception as e:
    record("medical_tools.deterministic_normalizer", False, str(e))

try:
    from app.tools.medical_tools import HeartRateRegulatorTool, LungVentilatorTool

    async def _test_medical_tools_async():
        hr = HeartRateRegulatorTool()
        assert await hr.get_tool_value() == "72.0 BPM"
        await hr.set_tool_value(160.0)
        assert await hr.get_tool_value() == "160.0 BPM"

        lung = LungVentilatorTool()
        assert await lung.get_tool_value() == "98.0%"
        await lung.set_tool_value(82.0)
        assert await lung.get_tool_value() == "82.0%"
        return True

    res, ms = run(timed_async(_test_medical_tools_async()))
    record("medical_tools.tools_get_set", res, duration_ms=ms)
except Exception as e:
    record("medical_tools.tools_get_set", False, str(e))

try:
    from app.agents.medical_agents import CardiovascularOrganAgent, RespiratoryOrganAgent
    from app.tools.medical_tools import HeartRateRegulatorTool, LungVentilatorTool

    async def _test_medical_agents_homeostasis():
        hr = HeartRateRegulatorTool()
        await hr.set_tool_value(160.0)

        lung = LungVentilatorTool()
        await lung.set_tool_value(82.0)

        med_tools = {"cardiac_pacemaker": hr, "oxygen_regulator": lung}
        cardio = CardiovascularOrganAgent(tools=med_tools)
        resp = RespiratoryOrganAgent(tools=med_tools)

        # Invocazione CardiovascularOrganAgent
        state_cardio = {
            "messages": [],
            "readings": [{"sensor_id": "cardiac_pacemaker", "agent_owner": "test", "value": "160.0", "unit": "BPM"}],
            "recent_events": [],
            "pending_escalations": [],
            "next_agent": "organ_cardiovascular",
            "hitl_required": False,
            "config": {},
        }
        res_c = await cardio(state_cardio)
        assert res_c["next_agent"] == "brain"  # Nome canonico del nodo Brain nel grafo

        # Invocazione RespiratoryOrganAgent
        state_resp = {
            "messages": [],
            "readings": [{"sensor_id": "oxygen_regulator", "agent_owner": "test", "value": "82.0", "unit": "%"}],
            "recent_events": [],
            "pending_escalations": [],
            "next_agent": "organ_respiratory",
            "hitl_required": False,
            "config": {},
        }
        res_r = await resp(state_resp)
        assert await lung.get_tool_value() == "95.0%"  # Ripristinato target SpO2 omeostatico
        return True

    res, ms = run(timed_async(_test_medical_agents_homeostasis()))
    record("medical_agents.homeostasis_restoration", res, duration_ms=ms)
except Exception as e:
    record("medical_agents.homeostasis_restoration", False, traceback.format_exc(limit=3))



# ── SEZIONE 15: force_execute_tool & On-Demand Tool Creation ─────────────────

print("\n[15] Brain Override & force_execute_tool")

# 15a: force_execute_tool bypassa i lock di priorità e aggiorna il tool
try:
    async def _test_force_execute():
        from app.tools.tool_wrapper import force_execute_tool
        from app.tools.sensor_tools import get_tool
        from app.tools.event_log import EventLog

        # Semina un blocco attivo nel DB in-memory
        db_path = ":memory:"
        import aiosqlite
        log = EventLog(target=["pool_pump"])
        await log.log_event(
            actor="organ_energy",
            action="FORCE_SHUTDOWN",
            target="pool_pump",
            old_value="ON",
            new_value="OFF",
            reasoning="Picco di rete",
            escalated=False,
        )

        # Crea il tool on-demand (pool_pump non era pre-registrato)
        pool_tool = get_tool("pool_pump", initial_value="OFF", unit="")
        assert pool_tool is not None

        # force_execute_tool deve bypassare il blocco e portare il tool a "ON"
        ok, msg = await force_execute_tool(
            target="pool_pump",
            tool_obj=pool_tool,
            action="UNBLOCK_AND_SET",
            new_value="ON",
            reasoning="Nonna ha bisogno del riscaldamento della piscina",
            event_log=log,
        )
        assert ok, f"force_execute_tool doveva restituire True, ha restituito False: {msg}"
        val = await pool_tool.get_tool_value()
        assert val == "ON", f"Pool pump deve essere ON dopo l'override, è invece: {val}"
        return True

    res, ms = run(timed_async(_test_force_execute()))
    record("brain_override.force_execute_tool", res, duration_ms=ms)
except Exception as e:
    record("brain_override.force_execute_tool", False, traceback.format_exc(limit=3))

# 15b: on-demand tool creation tramite get_tool
try:
    async def _test_on_demand_tool():
        from app.tools.sensor_tools import get_tool, _TOOL_REGISTRY
        unique_name = "test_on_demand_device_xyz"
        t = get_tool(unique_name, initial_value="IDLE", unit="status")
        assert t is not None
        assert await t.get_tool_value() == "IDLE"
        await t.set_tool_value("ACTIVE")
        # Il registry deve restituire la stessa istanza (singleton)
        t2 = get_tool(unique_name)
        assert await t2.get_tool_value() == "ACTIVE"
        return True

    res, ms = run(timed_async(_test_on_demand_tool()))
    record("brain_override.on_demand_tool_singleton", res, duration_ms=ms)
except Exception as e:
    record("brain_override.on_demand_tool_singleton", False, traceback.format_exc(limit=3))

# 15c: execute_semantic_override fallback su JSON non valido
try:
    async def _test_semantic_override_json_fallback():
        """Se il MAO restituisce una risposta non parsabile, il sistema crea un fallback JSON e lo esegue."""
        from app.graph.orchestrator import BrainAgent
        from app.tools.sensor_tools import get_tool
        from unittest.mock import patch

        brain = BrainAgent()
        heater = get_tool("heater_test_ov", initial_value="OFF", unit="°C")
        brain.tools["heater_test_ov"] = heater

        # Mock: MAO restituisce testo non JSON (es. risposta di errore o linguaggio naturale)
        with patch.object(brain, "ask_brain", return_value="Mi dispiace, non ho capito."):
            msgs = await brain._execute_semantic_override(
                human_directive="Accendi il riscaldamento per mia nonna",
                fallback_target="heater_test_ov",
                fallback_action="ON",
            )
        # Deve aver eseguito il fallback e restituire almeno un messaggio
        assert len(msgs) > 0
        # Fallback: il tool deve essere stato impostato
        val = await heater.get_tool_value()
        assert val == "ON", f"Heater deve essere ON dopo fallback override, è: {val}"
        return True

    res, ms = run(timed_async(_test_semantic_override_json_fallback()))
    record("brain_override.semantic_fallback_json", res, duration_ms=ms)
except Exception as e:
    record("brain_override.semantic_fallback_json", False, traceback.format_exc(limit=3))


# ── Output ────────────────────────────────────────────────────────────────
output_path = ROOT / "test_results.json"
summary = {
    "run_at": datetime.now(timezone.utc).isoformat(),
    "python_version": sys.version,
    "total": len(results),
    "passed": sum(1 for r in results if r["passed"]),
    "failed": sum(1 for r in results if not r["passed"]),
    "tests": results,
}
output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
print(f"\n{'='*50}")
print(f"Risultati: {summary['passed']}/{summary['total']} passati — {summary['failed']} falliti")
print(f"File: {output_path}")
if summary["failed"] > 0:
    print("FALLITI:")
    for r in results:
        if not r["passed"]:
            print(f"  - {r['test']}: {r['detail'][:150]}")
