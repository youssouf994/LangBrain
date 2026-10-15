"""
Registry ed Abilitatore Dinamico di Agenti Gerarchici.
Consente all'utente di definire, creare, elencare e rimuovere N sotto-agenti a runtime.
Supporta una struttura gerarchica ad albero a N livelli:
  Brain (Livello 0) -> Organ (Livello 1) -> Tissue (Livello 2) -> Cell (Livello 3 e oltre)

Catena di comando: un agente che non sa risolvere una situazione la passa al proprio padre,
fino al Brain. La routine si esaurisce ai livelli bassi; al Brain arrivano solo le anomalie.

Regole di coerenza, verificate a ogni scrittura sull'intero albero:
  - `parent_agent_name` è l'UNICA fonte di verità della gerarchia: `sub_agent_names` e `level`
    sono sempre derivati dal registro e non possono essere in contrasto con esso.
  - il padre deve esistere già (creazione dall'alto verso il basso) e non possono esistere cicli;
  - i nomi sono univoci ignorando maiuscole/minuscole e non possono usare nomi riservati;
  - un target può appartenere a più agenti solo se questi sono in relazione antenato/discendente;
  - un agente con figli non può essere eliminato finché i figli non vengono rimossi.
"""

import asyncio
import json
import logging
import re
from typing import Any
from sqlalchemy import MetaData, delete, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.agents.agenti_nativi import crea_agente_nativo
from app.agents.base_agent import BaseAgent
from app.agents.dynamic_agent import DynamicAgent
from app.core import archivio
from app.core.constants import nome_livello
from app.db import schema
from app.db.database import DB_PATH
from app.tools.effectors import default_effectors

logger = logging.getLogger(__name__)

NOME_BRAIN = "Brain"
NOMI_RISERVATI = {"brain", "end"}
TARGET_JOLLY = "all"
# Agenti nativi con logica propria, che non delegano ai figli registrati.
AGENTI_NATIVI_SENZA_FIGLI = {"agent_climate"}

# Solo la tabella del registro, per la migrazione additiva delle sue colonne.
_SOLO_REGISTRO = MetaData()
schema.agents_registry.to_metadata(_SOLO_REGISTRO)

_PATTERN_NOME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class ErroreGerarchia(ValueError):
    """La configurazione richiesta violerebbe la coerenza della gerarchia. Nulla viene scritto sul DB."""


class AgenteConFigliError(ErroreGerarchia):
    """L'agente da eliminare ha ancora figli registrati."""

    def __init__(self, nome: str, figli: list[str]):
        self.nome = nome
        self.figli = figli
        super().__init__(
            f"L'agente '{nome}' ha ancora figli registrati {figli}: eliminali prima di eliminare il padre."
        )


def _valida_nome(nome: Any) -> None:
    if not isinstance(nome, str) or not _PATTERN_NOME.fullmatch(nome):
        raise ErroreGerarchia(
            f"Nome agente {nome!r} non valido: usa da 1 a 64 caratteri tra lettere, cifre, '_' e '-', "
            "iniziando con una lettera."
        )
    if nome.casefold() in NOMI_RISERVATI:
        raise ErroreGerarchia(f"Il nome '{nome}' è riservato al sistema.")


def _valida_target(nome: str, target: Any) -> list[str]:
    if not isinstance(target, list) or not all(isinstance(t, str) and t.strip() for t in target):
        raise ErroreGerarchia(f"'managed_targets' dell'agente '{nome}' deve essere una lista di stringhe non vuote.")
    return list(dict.fromkeys(target))


def _calcola_figli(configs: list[dict[str, Any]]) -> dict[str, list[str]]:
    """
    Deriva i figli di ogni agente da `parent_agent_name`.
    I figli sono ordinati per priorità decrescente e poi per nome, così l'ordine di delega è deterministico.
    """
    nomi = {c["name"].casefold(): c["name"] for c in configs}
    figli: dict[str, list[str]] = {}
    ordinati = sorted(configs, key=lambda c: (-float(c.get("priority_weight") or 0.0), c["name"]))
    for cfg in ordinati:
        padre = nomi.get(str(cfg.get("parent_agent_name") or "").casefold())
        if padre and padre != cfg["name"]:
            figli.setdefault(padre, []).append(cfg["name"])
    return figli


def valida_registro(configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Valida l'intero albero e restituisce le configurazioni normalizzate, con
    `parent_agent_name` canonico, `level` e `sub_agent_names` derivati.
    Solleva ErroreGerarchia alla prima violazione.
    """
    # 1. Nomi e target
    nomi: dict[str, str] = {}
    target_per_agente: dict[str, list[str]] = {}
    for cfg in configs:
        nome = cfg["name"]
        _valida_nome(nome)
        if nome.casefold() in nomi:
            raise ErroreGerarchia(
                f"Nome agente duplicato: '{nome}' coincide con '{nomi[nome.casefold()]}' "
                "(il confronto ignora maiuscole/minuscole)."
            )
        nomi[nome.casefold()] = nome
        target_per_agente[nome] = _valida_target(nome, cfg.get("managed_targets", []))

    # 2. Padri: devono esistere, non essere l'agente stesso e poter delegare
    padre_di: dict[str, str] = {}
    for cfg in configs:
        nome = cfg["name"]
        richiesto = str(cfg.get("parent_agent_name") or NOME_BRAIN).strip() or NOME_BRAIN
        if richiesto.casefold() == NOME_BRAIN.casefold():
            padre = NOME_BRAIN
        elif richiesto.casefold() in nomi:
            padre = nomi[richiesto.casefold()]
        else:
            raise ErroreGerarchia(
                f"Il padre '{richiesto}' dell'agente '{nome}' non è registrato: "
                "registra prima il padre e poi i suoi figli."
            )
        if padre == nome:
            raise ErroreGerarchia(f"L'agente '{nome}' non può essere padre di sé stesso.")
        if padre in AGENTI_NATIVI_SENZA_FIGLI:
            raise ErroreGerarchia(
                f"L'agente nativo '{padre}' non delega ai figli registrati: '{nome}' non sarebbe mai raggiungibile."
            )
        padre_di[nome] = padre

    # 3. Cicli e livelli: la catena degli antenati deve terminare al Brain
    antenati: dict[str, list[str]] = {}
    for nome in padre_di:
        catena: list[str] = []
        visitati = {nome}
        corrente = padre_di[nome]
        while corrente != NOME_BRAIN:
            if corrente in visitati:
                percorso = " -> ".join([nome, *catena, corrente])
                raise ErroreGerarchia(f"Ciclo nella gerarchia (agente -> padre): {percorso}.")
            visitati.add(corrente)
            catena.append(corrente)
            corrente = padre_di[corrente]
        antenati[nome] = catena

    # 4. Target condivisi: ammessi solo tra antenato e discendente
    gestori: dict[str, list[str]] = {}
    for nome, targets in target_per_agente.items():
        for target in targets:
            if target != TARGET_JOLLY:
                gestori.setdefault(target, []).append(nome)
    for target, agenti in gestori.items():
        for i, a in enumerate(agenti):
            for b in agenti[i + 1:]:
                if a not in antenati[b] and b not in antenati[a]:
                    raise ErroreGerarchia(
                        f"Il target '{target}' è gestito sia da '{a}' sia da '{b}', che non sono in relazione "
                        "antenato/discendente: due rami diversi non possono agire sullo stesso dispositivo."
                    )

    normalizzate = [
        {**cfg, "managed_targets": target_per_agente[cfg["name"]],
         "parent_agent_name": padre_di[cfg["name"]], "level": len(antenati[cfg["name"]]) + 1}
        for cfg in configs
    ]
    figli = _calcola_figli(normalizzate)
    for cfg in normalizzate:
        cfg["sub_agent_names"] = figli.get(cfg["name"], [])
    return normalizzate


class AgentRegistry:
    """
    Registro Singleton per il provisioning dinamico di sotto-agenti a N livelli.
    Memorizza la configurazione degli agenti nel database (SQLite o Postgres) e li istanzia on-demand.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._instances: dict[str, BaseAgent] = {}
        # Serializza lettura-validazione-scrittura: due registrazioni concorrenti non devono
        # validare sulla stessa fotografia del registro e poi sovrapporsi.
        self._lock_scrittura = asyncio.Lock()

    def _motore(self):
        return archivio.motore(self.db_path)

    async def _assicura_tabella(self) -> None:
        async with self._motore().begin() as db:
            await db.run_sync(lambda c: schema.agents_registry.create(c, checkfirst=True))
            await db.run_sync(archivio.aggiungi_colonne_mancanti, _SOLO_REGISTRO)

    async def init_registry_db(self) -> None:
        """Inizializza la tabella `agents_registry` se non esiste."""
        await self._assicura_tabella()

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
        """
        Registra o aggiorna un sotto-agente nel DB.
        Solleva ErroreGerarchia se la nuova configurazione renderebbe incoerente l'albero;
        in tal caso il registro resta invariato.
        Restituisce la configurazione normalizzata (padre canonico, livello e figli derivati).
        """
        nome = config.get("name")
        if not nome:
            raise ErroreGerarchia("Il campo 'name' è obbligatorio per un agente.")

        try:
            livello_dichiarato = None if config.get("level") is None else int(config["level"])
            candidata = {
                "name": nome,
                "parent_agent_name": config.get("parent_agent_name"),
                "managed_targets": config.get("managed_targets", []),
                "system_prompt_template": config.get("system_prompt_template") or "",
                "conflict_window_minutes": int(config.get("conflict_window_minutes", 30)),
                "priority_weight": float(config.get("priority_weight", 1.0)),
            }
        except (TypeError, ValueError) as e:
            raise ErroreGerarchia(f"Valore numerico non valido nella configurazione di '{nome}': {e}") from e

        await self._assicura_tabella()
        async with self._lock_scrittura:
            esistenti = await self.get_all_agent_configs()
            altri = [c for c in esistenti if c["name"] != nome]
            normalizzate = valida_registro([*altri, candidata])
            registrata = next(c for c in normalizzate if c["name"] == nome)

            if livello_dichiarato is not None and livello_dichiarato != registrata["level"]:
                raise ErroreGerarchia(
                    f"Il livello {livello_dichiarato} di '{nome}' è incoerente con la gerarchia: "
                    f"con padre '{registrata['parent_agent_name']}' il livello è {registrata['level']} "
                    "(livello del padre + 1). Ometti 'level' per farlo derivare."
                )
            self._verifica_figli_dichiarati(registrata, config.get("sub_agent_names"), normalizzate)

            tab = schema.agents_registry
            valori = {
                "level": registrata["level"], "parent_agent_name": registrata["parent_agent_name"],
                "managed_targets": json.dumps(registrata["managed_targets"]),
                "sub_agent_names": json.dumps(registrata["sub_agent_names"]),
                "system_prompt_template": candidata["system_prompt_template"],
                "conflict_window_minutes": candidata["conflict_window_minutes"],
                "priority_weight": candidata["priority_weight"],
            }
            async with self._motore().begin() as db:
                await db.execute(
                    archivio.inserimento(self._motore(), tab).values(name=nome, **valori)
                    .on_conflict_do_update(index_elements=[tab.c.name], set_=valori)
                )
                # Livelli e figli degli altri agenti possono cambiare (es. spostamento di un sottoalbero).
                for c in normalizzate:
                    if c["name"] != nome:
                        await db.execute(update(tab).where(tab.c.name == c["name"]).values(
                            level=c["level"], parent_agent_name=c["parent_agent_name"],
                            sub_agent_names=json.dumps(c["sub_agent_names"])))

        self._instances.clear()
        logger.info(
            f"[AgentRegistry] Agente '{nome}' (Livello {registrata['level']}, "
            f"Padre: '{registrata['parent_agent_name']}') registrato con successo."
        )
        return registrata

    @staticmethod
    def _verifica_figli_dichiarati(
        registrata: dict[str, Any], dichiarati: Any, normalizzate: list[dict[str, Any]]
    ) -> None:
        """`sub_agent_names` in ingresso è accettato solo se coincide con figli già registrati."""
        if not dichiarati:
            return
        if not isinstance(dichiarati, list):
            raise ErroreGerarchia(f"'sub_agent_names' di '{registrata['name']}' deve essere una lista.")
        per_nome = {c["name"].casefold(): c for c in normalizzate}
        for figlio in dichiarati:
            cfg = per_nome.get(str(figlio).casefold())
            if cfg is None:
                raise ErroreGerarchia(
                    f"Il sotto-agente '{figlio}' dichiarato in '{registrata['name']}' non è registrato: "
                    "i figli si collegano registrandoli con 'parent_agent_name', non elencandoli nel padre."
                )
            if cfg["parent_agent_name"] != registrata["name"]:
                raise ErroreGerarchia(
                    f"Il sotto-agente '{cfg['name']}' dichiarato in '{registrata['name']}' ha come padre "
                    f"'{cfg['parent_agent_name']}': 'parent_agent_name' è l'unica fonte di verità."
                )

    async def _leggi_configs(self) -> list[dict[str, Any]]:
        tab = schema.agents_registry
        async with self._motore().connect() as db:
            righe = (await db.execute(select(tab).order_by(tab.c.level.asc(), tab.c.priority_weight.desc()))).all()
        configs = []
        for r in righe:
            item = dict(r._mapping)
            item["created_at"] = schema.come_testo(item.get("created_at"))
            item["managed_targets"] = json.loads(item["managed_targets"])
            configs.append(item)
        # I figli non si leggono dalla colonna: derivano sempre da `parent_agent_name`.
        figli = _calcola_figli(configs)
        for item in configs:
            item["sub_agent_names"] = figli.get(item["name"], [])
        return configs

    async def get_all_agent_configs(self) -> list[dict[str, Any]]:
        """Restituisce le configurazioni di tutti gli agenti registrati."""
        try:
            return await self._leggi_configs()
        except SQLAlchemyError:
            await self._assicura_tabella()
            return await self._leggi_configs()

    async def delete_agent(self, name: str) -> bool:
        """
        Rimuove un agente registrato. Restituisce False se non esiste.
        Solleva AgenteConFigliError se ha ancora figli.
        """
        await self._assicura_tabella()
        async with self._lock_scrittura:
            configs = await self.get_all_agent_configs()
            if not any(c["name"] == name for c in configs):
                return False
            figli = _calcola_figli(configs).get(name, [])
            if figli:
                raise AgenteConFigliError(name, figli)

            restanti = [c for c in configs if c["name"] != name]
            figli_derivati = _calcola_figli(restanti)
            tab = schema.agents_registry
            async with self._motore().begin() as db:
                await db.execute(delete(tab).where(tab.c.name == name))
                for c in restanti:
                    await db.execute(update(tab).where(tab.c.name == c["name"]).values(
                        sub_agent_names=json.dumps(figli_derivati.get(c["name"], []))))
        self._instances.clear()
        return True

    async def build_agent_instances(self, tools: dict[str, Any] | None = None) -> dict[str, BaseAgent]:
        """
        Istanzia tutti gli agenti dinamici registrati pronti per l'inserimento nei nodi LangGraph.
        """
        shared_tools = tools or default_effectors()
        configs = await self.get_all_agent_configs()

        instances: dict[str, BaseAgent] = {}
        for cfg in configs:
            name = cfg["name"]
            # Se è l'agente climate standard nativo
            nativo = crea_agente_nativo(name, shared_tools)
            if nativo is not None:
                instances[name] = nativo
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
                "level_name": nome_livello(level),
                "children": [build_branch(child["name"], child["level"]) for child in children_configs],
            }

        return {
            "root": "Brain",
            "title": "Hierarchy: Brain -> Organs -> Tissues -> Cells",
            "tree": build_branch("Brain", 0),
        }
