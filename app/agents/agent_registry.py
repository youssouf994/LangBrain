"""
Registry ed Abilitatore Dinamico di Agenti Gerarchici.
Consente all'utente di definire, creare, elencare e rimuovere N sotto-agenti a runtime.
Supporta una struttura gerarchica ad albero a N livelli:
  Cervello (Brain - Livello 0) -> Organi (Livello 1) -> Componenti dell'Organo (Livello 2) -> N Sotto-Agente (Livello N)
"""

import json
import logging
import os
from typing import Any
import aiosqlite

from app.agents.base_agent import BaseAgent
from app.agents.dynamic_agent import DynamicAgent
from app.db.database import DB_PATH
from app.tools.sensor_tools import get_default_iot_tools

logger = logging.getLogger(__name__)


class AgentRegistry:
    """
    Registro Singleton per il provisioning dinamico di sotto-agenti a N livelli.
    Memorizza la configurazione degli agenti su SQLite e li istanzia on-demand.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._instances: dict[str, BaseAgent] = {}

    async def init_registry_db(self) -> None:
        """Inizializza la tabella `agents_registry` su SQLite se non esiste."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS agents_registry (
                    name TEXT PRIMARY KEY,
                    level INTEGER NOT NULL DEFAULT 1,
                    parent_agent_name TEXT,
                    managed_targets TEXT NOT NULL,
                    sub_agent_names TEXT,
                    system_prompt_template TEXT,
                    conflict_window_minutes INTEGER DEFAULT 30,
                    priority_weight REAL DEFAULT 1.0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

        # Semina l'agente clima di default se la tabella è vuota
        all_agents = await self.get_all_agent_configs()
        if not all_agents:
            await self.register_agent_config({
                "name": "agent_climate",
                "level": 1,
                "parent_agent_name": "Brain",
                "managed_targets": ["ac_living_room", "heater_bedroom"],
                "sub_agent_names": [],
                "system_prompt_template": "Sei l'agente esperto di Clima...",
                "conflict_window_minutes": 30,
                "priority_weight": 0.001,
            })

    async def register_agent_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Registra o aggiorna un nuovo sotto-agente nel DB e nel registro in memoria."""
        name = config.get("name")
        if not name:
            raise ValueError("Il campo 'name' è obbligatorio per un agente.")

        level = int(config.get("level", 1))
        parent = config.get("parent_agent_name", "Brain" if level > 0 else None)
        targets = json.dumps(config.get("managed_targets", []))
        subs = json.dumps(config.get("sub_agent_names", []))
        prompt = config.get("system_prompt_template", "")
        window = int(config.get("conflict_window_minutes", 30))
        weight = float(config.get("priority_weight", 1.0))

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO agents_registry 
                       (name, level, parent_agent_name, managed_targets, sub_agent_names, system_prompt_template, conflict_window_minutes, priority_weight)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET
                       level=excluded.level, parent_agent_name=excluded.parent_agent_name,
                       managed_targets=excluded.managed_targets, sub_agent_names=excluded.sub_agent_names,
                       system_prompt_template=excluded.system_prompt_template,
                       conflict_window_minutes=excluded.conflict_window_minutes, priority_weight=excluded.priority_weight""",
                    (name, level, parent, targets, subs, prompt, window, weight),
                )
                await db.commit()
        except aiosqlite.OperationalError:
            await self.init_registry_db()
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO agents_registry 
                       (name, level, parent_agent_name, managed_targets, sub_agent_names, system_prompt_template, conflict_window_minutes, priority_weight)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET
                       level=excluded.level, parent_agent_name=excluded.parent_agent_name,
                       managed_targets=excluded.managed_targets, sub_agent_names=excluded.sub_agent_names,
                       system_prompt_template=excluded.system_prompt_template,
                       conflict_window_minutes=excluded.conflict_window_minutes, priority_weight=excluded.priority_weight""",
                    (name, level, parent, targets, subs, prompt, window, weight),
                )
                await db.commit()

        # Invalida l'istanza in memoria per forza ricreazione al prossimo get
        self._instances.pop(name, None)
        logger.info(f"[AgentRegistry] Agente '{name}' (Livello {level}, Padre: '{parent}') registrato con successo.")
        return config

    async def get_all_agent_configs(self) -> list[dict[str, Any]]:
        """Restituisce le configurazioni di tutti gli agenti registrati."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT * FROM agents_registry ORDER BY level ASC, priority_weight DESC") as cursor:
                    rows = await cursor.fetchall()
                    out = []
                    for r in rows:
                        item = dict(r)
                        item["managed_targets"] = json.loads(item["managed_targets"])
                        item["sub_agent_names"] = json.loads(item["sub_agent_names"]) if item["sub_agent_names"] else []
                        out.append(item)
                    return out
        except aiosqlite.OperationalError:
            await self.init_registry_db()
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT * FROM agents_registry ORDER BY level ASC, priority_weight DESC") as cursor:
                    rows = await cursor.fetchall()
                    out = []
                    for r in rows:
                        item = dict(r)
                        item["managed_targets"] = json.loads(item["managed_targets"])
                        item["sub_agent_names"] = json.loads(item["sub_agent_names"]) if item["sub_agent_names"] else []
                        out.append(item)
                    return out

    async def delete_agent(self, name: str) -> bool:
        """Rimuove un agente registrato."""
        async with aiosqlite.connect(self.db_path) as db:
            res = await db.execute("DELETE FROM agents_registry WHERE name = ?", (name,))
            await db.commit()
            deleted = res.rowcount > 0
        self._instances.pop(name, None)
        return deleted

    async def build_agent_instances(self, tools: dict[str, Any] | None = None) -> dict[str, BaseAgent]:
        """
        Istanzia tutti gli agenti dinamici registrati pronti per l'inserimento nei nodi LangGraph.
        """
        shared_tools = tools or get_default_iot_tools()
        configs = await self.get_all_agent_configs()

        instances: dict[str, BaseAgent] = {}
        for cfg in configs:
            name = cfg["name"]
            # Se è l'agente climate standard nativo
            if name == "agent_climate":
                from app.agents.agent_climate import ClimateAgent
                instances[name] = ClimateAgent(tools=shared_tools)
            else:
                instances[name] = DynamicAgent(
                    name=name,
                    managed_targets=cfg["managed_targets"],
                    parent_agent_name=cfg["parent_agent_name"],
                    sub_agent_names=cfg["sub_agent_names"],
                    level=cfg["level"],
                    system_prompt_template=cfg.get("system_prompt_template") or cfg.get("system_prompt"),
                    user_prompt_template=cfg.get("user_prompt_template") or cfg.get("user_prompt"),
                    conflict_window_minutes=cfg.get("conflict_window_minutes", 30),
                    priority_weight=cfg.get("priority_weight", 1.0),
                    tools=shared_tools,
                )
        return instances

    async def get_hierarchy_tree(self) -> dict[str, Any]:
        """
        Costruisce e restituisce l'albero gerarchico visualizzabile:
        Brain (Cervello) -> Organi -> Componenti dell'Organo
        """
        configs = await self.get_all_agent_configs()
        nodes_by_parent: dict[str | None, list[dict]] = {}

        for cfg in configs:
            parent = cfg.get("parent_agent_name") or "Brain"
            nodes_by_parent.setdefault(parent, []).append(cfg)

        def build_branch(agent_name: str, level: int) -> dict[str, Any]:
            children_configs = nodes_by_parent.get(agent_name, [])
            return {
                "name": agent_name,
                "level": level,
                "children": [build_branch(child["name"], child["level"]) for child in children_configs],
            }

        return {
            "root": "Brain",
            "title": "Gerarchia IoT: Cervello -> Organi -> Componenti dell'Organo",
            "tree": build_branch("Brain", 0),
        }
