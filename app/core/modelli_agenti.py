"""
Modello LLM specifico per agente, con ereditarietà lungo la gerarchia.

Ogni agente (compreso il Brain) può avere un provider e un modello propri, impostati via API e salvati nella tabella
`agent_models`. Se un agente non ne ha, usa quello del padre, poi quello del nonno e così via fino al Brain; se nessuno
li ha impostati vale il provider predefinito globale (`DEFAULT_PROVIDER` e il modello di quel provider nel `.env`).

Un'impostazione è un'unità: `provider` è obbligatorio e `model` facoltativo (se manca si usa il modello di default di
quel provider). Il modello di un padre non viene mai applicato a un provider diverso.

La risoluzione legge il database a ogni chiamata (poche righe, pochi millisecondi rispetto a una chiamata LLM), così
una modifica via API ha effetto immediato e non serve invalidare cache.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import SQLAlchemyError

from app.core import archivio
from app.db import schema
from app.db.database import DB_PATH

logger = logging.getLogger(__name__)

NOME_BRAIN = "Brain"
ORIGINE_PREDEFINITO = "predefinito"

modelli = schema.agent_models
registro = schema.agents_registry


@dataclass(frozen=True)
class ModelloRisolto:
    provider: str | None
    model: str | None
    origine: str
    """Nome dell'agente da cui deriva l'impostazione, oppure 'predefinito' se non ce n'è nessuna nella catena."""

    @property
    def impostato(self) -> bool:
        return self.provider is not None


def nome_canonico_brain(nome: str) -> str:
    return NOME_BRAIN if nome.casefold() == NOME_BRAIN.casefold() else nome


async def assicura_tabella(db_path: str = DB_PATH) -> None:
    async with archivio.motore(db_path).begin() as db:
        await db.run_sync(lambda c: modelli.create(c, checkfirst=True))


def _del_nome(nome: str):
    """Condizione sul nome dell'agente, senza distinguere maiuscole e minuscole (uguale su SQLite e Postgres)."""
    return func.lower(modelli.c.agent_name) == nome.casefold()


async def imposta_modello(nome: str, provider: str, modello: str | None, db_path: str = DB_PATH) -> None:
    """Imposta (o sostituisce) provider e modello propri dell'agente."""
    await assicura_tabella(db_path)
    nome = nome_canonico_brain(nome)
    async with archivio.motore(db_path).begin() as db:
        await db.execute(delete(modelli).where(_del_nome(nome)))
        await db.execute(insert(modelli).values(agent_name=nome, provider=provider, model=modello))


async def rimuovi_modello(nome: str, db_path: str = DB_PATH) -> bool:
    """Toglie l'impostazione propria: l'agente torna a ereditare. Restituisce False se non ce n'era una."""
    await assicura_tabella(db_path)
    async with archivio.motore(db_path).begin() as db:
        risultato = await db.execute(delete(modelli).where(_del_nome(nome)))
        return risultato.rowcount > 0


async def leggi_modello(nome: str, db_path: str = DB_PATH) -> tuple[str, str | None] | None:
    """Impostazione propria dell'agente (senza ereditarietà), oppure None."""
    await assicura_tabella(db_path)
    async with archivio.motore(db_path).connect() as db:
        riga = (await db.execute(select(modelli.c.provider, modelli.c.model).where(_del_nome(nome)))).first()
    return (riga[0], riga[1]) if riga else None


async def risolvi_modello(nome: str, db_path: str = DB_PATH) -> ModelloRisolto:
    """
    Modello effettivo dell'agente: la propria impostazione, altrimenti quella del padre, e così via fino al Brain.
    Non solleva mai: se il database non è leggibile si usa il predefinito globale.
    """
    predefinito = ModelloRisolto(None, None, ORIGINE_PREDEFINITO)
    try:
        async with archivio.motore(db_path).connect() as db:
            corrente = nome_canonico_brain(nome)
            visitati: set[str] = set()
            while corrente.casefold() not in visitati:
                visitati.add(corrente.casefold())
                try:
                    riga = (await db.execute(
                        select(modelli.c.agent_name, modelli.c.provider, modelli.c.model).where(_del_nome(corrente))
                    )).first()
                except SQLAlchemyError:
                    return predefinito  # tabella non ancora creata
                if riga:
                    return ModelloRisolto(riga[1], riga[2], riga[0])
                if corrente.casefold() == NOME_BRAIN.casefold():
                    break
                padre = None
                try:
                    riga_padre = (await db.execute(
                        select(registro.c.parent_agent_name).where(registro.c.name == corrente))).first()
                    padre = riga_padre[0] if riga_padre else None
                except SQLAlchemyError:
                    await db.rollback()  # su Postgres un errore invalida la transazione in corso
                corrente = nome_canonico_brain(padre or NOME_BRAIN)
    except Exception as e:
        logger.warning("[ModelliAgenti] Risoluzione del modello di '%s' non riuscita (%s): uso il predefinito.", nome, e)
    return predefinito
