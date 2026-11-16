"""
Migrazioni dello schema (Alembic): l'unico modo in cui lo schema del database cambia.

  - `aggiorna_schema(connessione)`: porta il database all'ultima revisione (da `Database.init_db`, dentro il blocco di avvio).
  - `LANGBRAIN_MIGRAZIONI=manuale`: l'applicazione non migra da sola; all'avvio verifica che il database sia già all'ultima
    revisione (`alembic upgrade head`, di solito come passo del deploy) e si ferma con un messaggio chiaro se non lo è.
    Predefinito: `auto`.
  - `revisione_corrente` / `revisione_attesa`: per i log e i test.

Un database creato prima di Alembic (nessuna tabella `alembic_version`) viene preso in carico dalla revisione 0001, che fa
ciò che faceva l'auto-migrazione di prima. Regole per scrivere le revisioni: `docs/MIGRAZIONI.md`.
"""

import logging
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.core import archivio

logger = logging.getLogger(__name__)

RADICE = Path(__file__).resolve().parents[2]
VARIABILE_MODO = "LANGBRAIN_MIGRAZIONI"
MODO_AUTO = "auto"
MODO_MANUALE = "manuale"


class ErroreMigrazioni(RuntimeError):
    """Lo schema non è all'ultima revisione e l'applicazione non deve (o non può) migrarlo."""


def modo() -> str:
    grezzo = (os.getenv(VARIABILE_MODO) or MODO_AUTO).strip().lower()
    scelto = {"manual": MODO_MANUALE, "manuale": MODO_MANUALE, "auto": MODO_AUTO, "automatico": MODO_AUTO}.get(grezzo)
    if scelto is None:
        raise ErroreMigrazioni(f"{VARIABILE_MODO}='{grezzo}' non è valido: usa 'auto' (predefinito) oppure 'manuale'.")
    return scelto


def _configurazione(connessione=None) -> Config:
    configurazione = Config()
    configurazione.set_main_option("script_location", str(RADICE / "migrations"))
    if connessione is not None:
        configurazione.attributes["connection"] = connessione
    return configurazione


def revisione_attesa() -> str:
    """L'ultima revisione disponibile nel codice."""
    return ScriptDirectory.from_config(_configurazione()).get_current_head()


def revisione_corrente(connessione_sincrona) -> str | None:
    """La revisione in cui si trova il database (None se Alembic non l'ha ancora preso in carico)."""
    return MigrationContext.configure(connessione_sincrona).get_current_revision()


def aggiorna_schema(connessione_sincrona) -> str:
    """
    Porta il database all'ultima revisione e restituisce quella raggiunta; da usare con `await conn.run_sync(aggiorna_schema)`.
    Con `LANGBRAIN_MIGRAZIONI=manuale` non cambia nulla: verifica e solleva `ErroreMigrazioni` se mancano revisioni.
    """
    attesa = revisione_attesa()
    if modo() == MODO_MANUALE:
        corrente = revisione_corrente(connessione_sincrona)
        if corrente != attesa:
            raise ErroreMigrazioni(
                f"Il database è alla revisione {corrente or 'nessuna'} ma il codice richiede la {attesa}, e "
                f"{VARIABILE_MODO}=manuale: esegui `alembic upgrade head` prima di avviare l'applicazione.")
        return corrente
    archivio._blocca_schema(connessione_sincrona)  # Postgres: una migrazione alla volta, anche tra processi
    prima = revisione_corrente(connessione_sincrona)
    if prima != attesa:
        command.upgrade(_configurazione(connessione_sincrona), "head")
        logger.info("Schema del database migrato: %s -> %s.", prima or "nessuna", attesa)
    return attesa
