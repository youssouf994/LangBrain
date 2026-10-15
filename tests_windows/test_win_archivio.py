"""Archivio: scelta di SQLite o Postgres dall'URL, traduzione di sslmode, un motore per database, nessuna password nei log."""

import os
import tempfile
import unittest
from unittest import mock

from sqlalchemy import text

from app.core import archivio


class UrlTest(unittest.TestCase):
    def test_file_sqlite(self):
        url = archivio.url_database("dati/x.db")
        self.assertTrue(url.startswith("sqlite+aiosqlite:///"))
        self.assertNotIn("\\", url)

    def test_url_postgres_usa_asyncpg(self):
        self.assertEqual(archivio.url_database("postgres://u:p@h:5432/d"), "postgresql+asyncpg://u:p@h:5432/d")
        self.assertEqual(archivio.url_database("postgresql://u:p@h/d"), "postgresql+asyncpg://u:p@h/d")

    def test_sslmode_diventa_ssl(self):
        url = archivio.url_database("postgresql://u:p@h/d?sslmode=require")
        self.assertIn("ssl=require", url)
        self.assertNotIn("sslmode", url)
        self.assertIn("sslmode=require", archivio.url_per_psycopg("postgresql://u:p@h/d?sslmode=require"))

    def test_database_url_vale_solo_per_il_file_predefinito(self):
        from app.db.database import DB_PATH
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://u:p@h/d"}):
            self.assertTrue(archivio.e_postgres())
            self.assertTrue(archivio.e_postgres(DB_PATH))
            self.assertFalse(archivio.e_postgres("altro_file.db"))

    def test_password_mascherata(self):
        self.assertNotIn("segreta", archivio.url_mascherato("postgresql://u:segreta@h/d"))

    def test_database_non_supportato(self):
        with self.assertRaises(ValueError):
            archivio.url_database("mysql://u:p@h/d")


class MotoreSqliteTest(unittest.IsolatedAsyncioTestCase):
    async def test_motore_riusato_e_file_cancellabile(self):
        cartella = tempfile.mkdtemp()
        percorso = os.path.join(cartella, "t.db")
        self.assertIs(archivio.motore(percorso), archivio.motore(percorso))
        async with archivio.motore(percorso).begin() as conn:
            await conn.execute(text("CREATE TABLE t (a INTEGER)"))
            await conn.execute(text("INSERT INTO t (a) VALUES (:a)"), {"a": 7})
        async with archivio.motore(percorso).connect() as conn:
            self.assertEqual((await conn.execute(text("SELECT a FROM t"))).scalar_one(), 7)
        await archivio.chiudi_motori()
        os.remove(percorso)  # senza connessioni rimaste aperte non c'è WinError 32
        os.rmdir(cartella)
