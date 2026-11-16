"""Migrazioni Alembic (`migrations/`, `app/db/migrazioni.py`): schema, database di prima di Alembic, modalità manuale."""

import os
import tempfile
import unittest
from unittest.mock import patch

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from app.core import archivio
from app.db import migrazioni, schema
from app.db.database import Database
from app.db.migrazioni import ErroreMigrazioni
from app.db.scenario import azzera_database
from tests.sqlite_chiusura import connetti


def _differenze(connessione_sincrona):
    """Differenze tra lo schema nel codice e quello del database: tabelle, colonne e indici (i tipi si confrontano in `env.py`)."""
    contesto = MigrationContext.configure(connessione_sincrona, opts={"compare_type": True, "include_object": _solo_nostre})
    return compare_metadata(contesto, schema.metadata)


def _solo_nostre(oggetto, nome, tipo, riflesso, confronto):
    if tipo == "index" and nome == "idx_events_target_ts":
        return False   # indice con espressione: Alembic non sa confrontarlo (stessa esclusione di migrations/env.py)
    return not (tipo == "table" and riflesso and nome not in schema.metadata.tables)


class MigrazioniTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cartella = tempfile.TemporaryDirectory(prefix="lb_mig_")
        self.addCleanup(self.cartella.cleanup)
        self.db = os.path.join(self.cartella.name, "m.db")
        self.addAsyncCleanup(archivio.chiudi_motori)

    async def revisione(self):
        async with archivio.motore(self.db).connect() as db:
            return await db.run_sync(migrazioni.revisione_corrente)

    async def test_un_database_nuovo_arriva_all_ultima_revisione_con_tutte_le_tabelle(self):
        await Database(self.db).init_db()
        self.assertEqual(await self.revisione(), migrazioni.revisione_attesa())
        async with archivio.motore(self.db).connect() as db:
            tabelle = set(await db.run_sync(lambda c: inspect(c).get_table_names()))
        self.assertTrue(set(schema.metadata.tables) <= tabelle, set(schema.metadata.tables) - tabelle)

    async def test_lo_schema_nel_codice_e_quello_delle_migrazioni_coincidono(self):
        """Se cambia `schema.py` senza una nuova revisione, questo test lo dice (nome della colonna o tabella inclusi)."""
        await Database(self.db).init_db()
        async with archivio.motore(self.db).connect() as db:
            differenze = await db.run_sync(_differenze)
        self.assertEqual(differenze, [], "Manca una revisione: `alembic revision --autogenerate -m \"...\"` (docs/MIGRAZIONI.md).")

    async def test_e_idempotente(self):
        for _ in range(3):
            await Database(self.db).init_db()
        self.assertEqual(await self.revisione(), migrazioni.revisione_attesa())

    async def test_un_database_di_prima_di_alembic_viene_preso_in_carico_senza_perdere_dati(self):
        with connetti(self.db) as db:
            db.execute("""CREATE TABLE events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
                          target TEXT NOT NULL, reasoning TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, escalated BOOLEAN DEFAULT 0)""")
            db.execute("INSERT INTO events (actor, action, target, reasoning) VALUES ('a', 'X', 't', 'prima')")
        await Database(self.db).init_db()
        self.assertEqual(await self.revisione(), migrazioni.revisione_attesa())
        with connetti(self.db) as db:
            colonne = {r[1] for r in db.execute("PRAGMA table_info(events)")}
            righe = db.execute("SELECT actor, reasoning FROM events").fetchall()
        self.assertTrue({"old_value", "new_value", "correlation_id", "outcome"} <= colonne)
        self.assertEqual(righe, [("a", "prima")])

    async def test_la_tabella_dello_stato_fisico_creata_prima_della_migrazione_non_la_rompe(self):
        async with archivio.motore(self.db).begin() as db:
            await db.run_sync(archivio.crea_tabelle, schema.metadata, [schema.device_state])
        await Database(self.db).init_db()
        self.assertEqual(await self.revisione(), migrazioni.revisione_attesa())

    async def test_modalita_manuale_non_migra_e_rifiuta_un_database_indietro(self):
        with patch.dict(os.environ, {migrazioni.VARIABILE_MODO: "manuale"}):
            with self.assertRaises(ErroreMigrazioni) as ctx:
                await Database(self.db).init_db()
            self.assertIn("alembic upgrade head", str(ctx.exception))
            async with archivio.motore(self.db).connect() as db:   # nulla è stato creato
                self.assertEqual(await db.run_sync(lambda c: inspect(c).get_table_names()), [])
        await Database(self.db).init_db()   # migrato come farebbe il passo di deploy...
        with patch.dict(os.environ, {migrazioni.VARIABILE_MODO: "manuale"}):
            await Database(self.db).init_db()   # ...ora la modalità manuale parte

    async def test_modalita_non_valida(self):
        with patch.dict(os.environ, {migrazioni.VARIABILE_MODO: "boh"}), self.assertRaises(ErroreMigrazioni):
            migrazioni.modo()

    async def test_dopo_l_azzeramento_dello_scenario_le_tabelle_si_ricreano(self):
        await Database(self.db).init_db()
        await azzera_database(self.db)
        await Database(self.db).init_db()   # senza `alembic_version` eliminata, lo crederebbe già aggiornato
        async with archivio.motore(self.db).connect() as db:
            self.assertEqual((await db.execute(text("SELECT COUNT(*) FROM events"))).scalar_one(), 0)

    async def test_le_revisioni_sono_una_catena_lineare_con_la_baseline_in_testa(self):
        from alembic.script import ScriptDirectory
        script = ScriptDirectory.from_config(migrazioni._configurazione())
        revisioni = list(script.walk_revisions())
        self.assertEqual(len(script.get_heads()), 1)
        self.assertIsNone(revisioni[-1].down_revision)
        self.assertEqual(revisioni[-1].revision, "0001")


if __name__ == "__main__":
    unittest.main()
