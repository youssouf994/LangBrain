"""
Scenari di dati riproducibili, per demo, smoke test e test dei prossimi punti della checklist.

Uno scenario azzera il database e lo ripopola con una gerarchia di agenti coerente con `configurazione.toml`, uno
storico di eventi realistico (tutto più vecchio di 5 ore, quindi fuori dalle finestre degli agenti: non interferisce
con i cicli) e, a seconda dello scenario, un conflitto attivo. Gli stati dei tool non stanno nel database: partono dai
valori predefiniti di `get_default_iot_tools()`.

Scenari:
  vuoto            database azzerato, senza agenti né eventi (nemmeno l'agente clima di default)
  base             gerarchia + storico, nessun conflitto attivo
  conflitto_porta  base + blocco manuale recente su front_door_lock (attiva l'escalation di component_door_lock)
  finestra_aperta  base + FORCE_SHUTDOWN recente su ac_living_room (il conflitto dimostrativo storico)

Gerarchia dello scenario base:
  Brain
  ├── agent_climate          (0.001)  ac_living_room, heater_bedroom          agente nativo
  ├── organ_security         (500)    front_door_lock, alarm_system
  │   ├── component_door_lock (200)   front_door_lock
  │   └── component_alarm     (200)   alarm_system
  └── organ_home             (50)     living_room_lights
      └── component_lights    (10)    living_room_lights
"""

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, insert, select, text

from app.agents.agent_registry import AgentRegistry
from app.core import archivio
from app.core.modelli_agenti import assicura_tabella as assicura_tabella_modelli, imposta_modello
from app.db import schema
from app.db.database import Database
from app.graph.timer_hitl import GestoreTimerHitl
from app.MAO.model_access_object import PROVIDER_NOTI, normalizza_provider

logger = logging.getLogger(__name__)

TABELLE_DA_SVUOTARE = ("events", "signals", "readings", "node_runs", "agents_registry", "agent_models", "hitl_scadenze", "checkpoints", "writes")

# (nome, padre, target, peso, prompt)
AGENTI_BASE = [
    ("agent_climate", "Brain", ["ac_living_room", "heater_bedroom"], 0.001, "Sei l'agente esperto di Clima..."),
    ("organ_security", "Brain", ["front_door_lock", "alarm_system"], 500.0,
     "Sei l'Organo di Sicurezza. Coordini i componenti serratura e allarme e fai escalation al Brain per i conflitti critici."),
    ("component_door_lock", "organ_security", ["front_door_lock"], 200.0, "Sei il Componente Serratura. Gestisci la porta principale."),
    ("component_alarm", "organ_security", ["alarm_system"], 200.0, "Sei il Componente Allarme. Gestisci l'allarme antintrusione."),
    ("organ_home", "Brain", ["living_room_lights"], 50.0, "Sei l'Organo Casa. Coordini le luci del soggiorno."),
    ("component_lights", "organ_home", ["living_room_lights"], 10.0, "Sei il Componente Luci. Gestisci le luci del soggiorno."),
]

# (ore fa, attore, azione, target, valore precedente, nuovo valore, motivazione, escalated)
STORICO_BASE = [
    (26, "organ_security", "DYNAMIC_ACTION", "alarm_system", "DISARMED", "ARMED", "Armato alla chiusura serale.", 0),
    (25, "user_manual", "SECURITY_LOCK", "front_door_lock", "LOCKED", "LOCKED", "Blocco manuale notturno della porta.", 0),
    (24, "user_manual", "RESOLVED_SECURITY_LOCK", "front_door_lock", "LOCKED", "LOCKED", "Blocco notturno concluso al mattino.", 0),
    (6, "component_lights", "TOOL_ERROR_DYNAMIC_ACTION", "living_room_lights", "OFF", "FAILED",
     "Guasto del dispositivo 'living_room_lights': timeout controller Zigbee", 0),
    (6, "component_lights", "RESOLVED_ESCALATION_PROPOSED", "living_room_lights", "OFF", "ON",
     "Guasto risolto con troubleshooting: nuovo tentativo riuscito.", 0),
    (5, "agent_climate", "TURN_ON_AC", "ac_living_room", "OFF", "22.5°C", "Attivazione consigliata: temperatura oltre la soglia.", 0),
]

SCENARI = ("vuoto", "base", "conflitto_porta", "finestra_aperta")


async def azzera_database(db_path: str) -> None:
    """
    Elimina tutte le tabelle note (dati applicativi, agenti, modelli, scadenze HITL, checkpoint). Le tabelle vanno
    eliminate e non solo svuotate: un database con uno schema vecchio o parziale non si potrebbe reinizializzare.
    Vanno ricreate con `Database.init_db()` e gli altri `assicura_tabella`; `crea_scenario` lo fa.
    """
    async with archivio.motore(db_path).begin() as db:
        for tabella in TABELLE_DA_SVUOTARE:
            if tabella in ('checkpoints', 'writes') and db.dialect.name == 'postgresql':
                continue  # su Postgres le tabelle del checkpointer hanno altri nomi e le gestisce LangGraph
            # Nomi fissi della nostra lista (non input esterno); l'identificatore è comunque quotato dal dialetto.
            await db.execute(text(f"DROP TABLE IF EXISTS {db.dialect.identifier_preparer.quote(tabella)}"))


def crea_backup(db_path: str) -> Path | None:
    """Copia il database in `<nome>.backup-<data>.db` accanto all'originale (il suffisso .db lo lascia fuori da git)."""
    origine = Path(db_path)
    if not origine.exists() or origine.stat().st_size == 0:
        return None
    destinazione = origine.with_name(f"{origine.stem}.backup-{datetime.now():%Y%m%d-%H%M%S}.db")
    shutil.copy2(origine, destinazione)
    return destinazione


async def _inserisci_evento(db, ore_fa: float, attore, azione, target, precedente, nuovo, motivo, escalated) -> None:
    quando = schema.adesso_utc() - timedelta(seconds=int(ore_fa * 3600))
    await db.execute(insert(schema.events).values(
        actor=attore, action=azione, target=target, old_value=precedente, new_value=nuovo, reasoning=motivo,
        timestamp=quando, escalated=escalated))


async def crea_scenario(
    db_path: str, scenario: str = "base", modelli: dict[str, tuple[str, str | None]] | None = None
) -> dict:
    """
    Azzera il database e crea lo scenario indicato. `modelli` associa a un agente (anche 'Brain') un provider e un
    modello propri, es. {"organ_security": ("mistral", "ministral-8b-latest")}. Restituisce un riepilogo.
    """
    if scenario not in SCENARI:
        raise ValueError(f"Scenario '{scenario}' sconosciuto: usa uno tra {list(SCENARI)}.")
    modelli = modelli or {}
    for agente, (provider, _) in modelli.items():
        if normalizza_provider(provider) not in PROVIDER_NOTI:
            raise ValueError(f"Provider '{provider}' per '{agente}' sconosciuto: usa uno tra {list(PROVIDER_NOTI)}.")

    # Tabelle eliminate e ricreate con lo schema attuale; l'eventuale seed dimostrativo di init_db viene cancellato
    await azzera_database(db_path)
    await Database(db_path).init_db()
    registro = AgentRegistry(db_path=db_path)
    await registro._assicura_tabella()
    await assicura_tabella_modelli(db_path)
    await GestoreTimerHitl(lambda: None, db_path).assicura_tabella()
    async with archivio.motore(db_path).begin() as db:
        await db.execute(delete(schema.events))

    if scenario == "vuoto":
        return {"scenario": scenario, "db": db_path, "agenti": [], "eventi": 0, "modelli": {}}

    for nome, padre, target, peso, prompt in AGENTI_BASE:
        await registro.register_agent_config({
            "name": nome, "parent_agent_name": padre, "managed_targets": target,
            "priority_weight": peso, "system_prompt_template": prompt,
        })
    for agente, (provider, modello) in modelli.items():
        await imposta_modello(agente, normalizza_provider(provider), modello, db_path)

    async with archivio.motore(db_path).begin() as db:
        for evento in STORICO_BASE:
            await _inserisci_evento(db, *evento)
        if scenario == "conflitto_porta":
            await _inserisci_evento(
                db, 1 / 60, "user_manual", "SECURITY_LOCK", "front_door_lock", "LOCKED", "LOCKED",
                "Blocco manuale: porta in verifica.", 0,
            )
        elif scenario == "finestra_aperta":
            await _inserisci_evento(
                db, 1 / 60, "agent_security", "FORCE_SHUTDOWN", "ac_living_room", "22.5°C", "OFF",
                "Simulazione: finestra aperta!", 0,
            )
        eventi = (await db.execute(select(func.count()).select_from(schema.events))).scalar_one()

    return {
        "scenario": scenario, "db": db_path, "agenti": [a[0] for a in AGENTI_BASE], "eventi": eventi,
        "modelli": {k: {"provider": normalizza_provider(v[0]), "model": v[1]} for k, v in modelli.items()},
    }
