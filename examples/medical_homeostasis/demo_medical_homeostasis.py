"""
Dimostrazione Omeostasi Fisiologica: Simulazione di una Patologia e Risoluzione da parte degli Agenti Medicali.
Posizione: examples/medical_homeostasis/demo_medical_homeostasis.py
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

# Risoluzione dinamica della radice del progetto
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agents.agent_registry import AgentRegistry
from app.agents.medical_agents import CardiovascularOrganAgent, RespiratoryOrganAgent
from app.db.database import Database
from app.graph.builder import build_graph
from app.tools.event_log import EventLog
from app.tools.medical_tools import (
    HeartRateRegulatorTool,
    LungVentilatorTool,
    deterministic_biometric_normalizer,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("demo_medical_homeostasis")


async def main():
    logger.info("=== AVVIO DEMO PATOLOGIA MEDICA & OMEOSTASI FISIOLOGICA ===")

    # 1. Inizializzazione DB e Registri
    db = Database()
    await db.init_db()
    event_log = EventLog()

    # Istanzia i tool medici
    pacemaker = HeartRateRegulatorTool()
    ventilator = LungVentilatorTool()
    medical_tools = {
        "cardiac_pacemaker": pacemaker,
        "oxygen_regulator": ventilator,
    }

    # Istanzia gli agenti di organo fisiologico
    cardio_agent = CardiovascularOrganAgent(tools=medical_tools)
    resp_agent = RespiratoryOrganAgent(tools=medical_tools)

    custom_instances = {
        "organ_cardiovascular": cardio_agent,
        "organ_respiratory": resp_agent,
    }

    # 2. Compila il grafo con gli agenti medici
    graph, shared_tools = build_graph(custom_agent_instances=custom_instances)
    thread_config = {"configurable": {"thread_id": "medical_pathology_session"}}

    # -------------------------------------------------------------------
    # FASE 1: STATO DI SALUTE INIZIALE (OMEOSTASI NORMALE)
    # -------------------------------------------------------------------
    logger.info("\n--- [FASE 1] Stato Fisiologico Iniziale (Sano) ---")
    val_bpm = await pacemaker.get_tool_value()
    val_spo2 = await ventilator.get_tool_value()
    logger.info("Frequenza Cardiaca Attuale: %s | Normalizzazione: %s", val_bpm, pacemaker.normalize_current_state())
    logger.info("Ossigenazione SpO2 Attuale: %s | Normalizzazione: %s", val_spo2, ventilator.normalize_current_state())

    # -------------------------------------------------------------------
    # FASE 2: INSORGENZA PATOLOGIA (CRISI CARDIACA & IPOSSICA)
    # -------------------------------------------------------------------
    logger.info("\n--- [FASE 2] Insorgenza Patologia: Tachicardia Severa (160 BPM) & Ipossia (82% SpO2) ---")
    await pacemaker.set_tool_value(160.0)
    await ventilator.set_tool_value(82.0)

    logger.info("Dati Biometrici Alterati:")
    logger.info(" - Pacemaker Alterato: %s | %s", await pacemaker.get_tool_value(), pacemaker.normalize_current_state())
    logger.info(" - Ventilatore Alterato: %s | %s", await ventilator.get_tool_value(), ventilator.normalize_current_state())

    # -------------------------------------------------------------------
    # FASE 3: INTERVENTO DELL'AGENTE CARDIOVASCOLARE (ARITMIA SEVERA -> ESCALATION)
    # -------------------------------------------------------------------
    logger.info("\n--- [FASE 3] Invocazione Agente Cardiovascolare per Risoluzione Tachicardia ---")
    state_cardio = {
        "messages": [],
        "readings": [{"sensor_id": "cardiac_pacemaker", "agent_owner": "organ_cardiovascular", "value": "160.0", "unit": "BPM"}],
        "recent_events": [],
        "pending_escalations": [],
        "next_agent": "organ_cardiovascular",
        "hitl_required": False,
        "config": {},
    }

    res_cardio = await graph.ainvoke(state_cardio, config=thread_config)
    messages_cardio = res_cardio.get("messages", [])
    if messages_cardio:
        logger.info("Esito Agente Cardiovascolare: %s", messages_cardio[-1].content)

    # -------------------------------------------------------------------
    # FASE 3b: RICONCILIAZIONE BRAIN / AGENTE CARDIOVASCOLARE SU PACEMAKER
    # -------------------------------------------------------------------
    logger.info("\n--- [FASE 3b] Ripristino Omeostatico del Pacemaker Cardiaco (100 BPM) ---")
    await cardio_agent.apply_status(
        target="cardiac_pacemaker",
        action="HOMEOSTASIS_BPM_RESTORATION",
        new_value="100.0 BPM",
        reasoning="Risoluzione Aritmia Severa ed allineamento target omeostatico a 100 BPM.",
        tools_map=medical_tools,
    )

    # -------------------------------------------------------------------
    # FASE 4: INTERVENTO DELL'AGENTE RESPIRATORIO PER RISOLUZIONE IPOSSIA
    # -------------------------------------------------------------------
    logger.info("\n--- [FASE 4] Invocazione Agente Respiratorio per Risoluzione Ipossia ---")
    state_resp = {
        "messages": [],
        "readings": [{"sensor_id": "oxygen_regulator", "agent_owner": "organ_respiratory", "value": "82.0", "unit": "%"}],
        "recent_events": await event_log.get_recent_events(),
        "pending_escalations": [],
        "next_agent": "organ_respiratory",
        "hitl_required": False,
        "config": {},
    }

    res_resp = await graph.ainvoke(state_resp, config=thread_config)
    messages_resp = res_resp.get("messages", [])
    if messages_resp:
        logger.info("Esito Agente Respiratorio: %s", messages_resp[-1].content)

    # -------------------------------------------------------------------
    # FASE 5: VERIFICA POST-INTERVENTO (OMEOSTASI RIPRISTINATA)
    # -------------------------------------------------------------------
    logger.info("\n--- [FASE 5] Verifica Parametri Biometrici dopo l'Intervento degli Agenti ---")
    post_bpm = await pacemaker.get_tool_value()
    post_spo2 = await ventilator.get_tool_value()

    norm_post_bpm = pacemaker.normalize_current_state()
    norm_post_spo2 = ventilator.normalize_current_state()

    logger.info("Post-Intervento Frequenza Cardiaca: %s | In Range: %s | Score: %s", post_bpm, norm_post_bpm["is_in_range"], norm_post_bpm["normalized_score"])
    logger.info("Post-Intervento Ossigenazione SpO2: %s | In Range: %s | Score: %s", post_spo2, norm_post_spo2["is_in_range"], norm_post_spo2["normalized_score"])

    assert norm_post_bpm["is_in_range"] is True, "La frequenza cardiaca deve essere rientrata nel range omeostatico"
    assert norm_post_spo2["is_in_range"] is True, "La saturazione SpO2 deve essere rientrata nel range omeostatico"

    logger.info("\n=== PATOLOGIA RISOLTA CON SUCCESSO DAGLI AGENTI MEDICALI ===")


if __name__ == "__main__":
    asyncio.run(main())