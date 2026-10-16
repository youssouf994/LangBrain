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

    def test_il_backend_predefinito_e_sqlite_anche_se_c_e_un_url(self):
        from app.db.database import DB_PATH
        with mock.patch.dict(os.environ, {"LANGBRAIN_DB_URL": "postgresql://u:p@h/d"}):
            os.environ.pop("LANGBRAIN_DB_BACKEND", None)
            self.assertEqual(archivio.backend(), "sqlite")
            self.assertFalse(archivio.e_postgres(DB_PATH))

    def test_backend_postgres_vale_solo_per_il_file_predefinito(self):
        from app.db.database import DB_PATH
        with mock.patch.dict(os.environ, {"LANGBRAIN_DB_BACKEND": "postgres", "LANGBRAIN_DB_URL": "postgresql://u:p@h/d"}):
            self.assertTrue(archivio.e_postgres())
            self.assertTrue(archivio.e_postgres(DB_PATH))
            self.assertFalse(archivio.e_postgres("altro_file.db"))
            archivio.verifica_configurazione()

    def test_backend_postgres_senza_url_rifiuta_l_avvio_con_un_messaggio_chiaro(self):
        with mock.patch.dict(os.environ, {"LANGBRAIN_DB_BACKEND": "postgres"}):
            os.environ.pop("LANGBRAIN_DB_URL", None)
            with self.assertRaisesRegex(archivio.ErroreConfigurazioneDb, "LANGBRAIN_DB_URL"):
                archivio.verifica_configurazione()

    def test_backend_sconosciuto_o_url_non_postgres_rifiutati(self):
        with mock.patch.dict(os.environ, {"LANGBRAIN_DB_BACKEND": "mysql"}):
            with self.assertRaisesRegex(archivio.ErroreConfigurazioneDb, "non è valido"):
                archivio.verifica_configurazione()
        with mock.patch.dict(os.environ, {"LANGBRAIN_DB_BACKEND": "postgres", "LANGBRAIN_DB_URL": "sqlite:///x.db"}):
            with self.assertRaisesRegex(archivio.ErroreConfigurazioneDb, "postgresql"):
                archivio.verifica_configurazione()

    def test_la_password_non_compare_nel_messaggio_di_errore_ne_nel_log(self):
        with mock.patch.dict(os.environ, {"LANGBRAIN_DB_BACKEND": "postgres", "LANGBRAIN_DB_URL": "postgresql://u:segretissima@h/d"}):
            self.assertNotIn("segretissima", archivio.url_mascherato())

    def test_schema_dell_url_si_applica_al_driver_e_a_psycopg(self):
        url = "postgresql://u:p@h/d?lb_schema=s1"
        self.assertEqual(archivio.schema_dell_url(archivio.url_database(url)), "s1")
        self.assertIn("search_path%3Ds1", archivio.url_per_psycopg(url).replace("+", "%20").replace("%3D", "%3D"))

    def test_database_url_non_piu_letta_avvisa_nel_log_senza_cambiare_database(self):
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://u:segretissima@h/d"}):
            os.environ.pop("LANGBRAIN_DB_URL", None)
            os.environ.pop("LANGBRAIN_DB_BACKEND", None)
            with self.assertLogs("app.core.archivio", level="WARNING") as log:
                archivio.verifica_configurazione()
            testo = " ".join(log.output)
            self.assertIn("LANGBRAIN_DB_BACKEND=postgres", testo)
            self.assertIn("LANGBRAIN_DB_URL", testo)
            self.assertNotIn("segretissima", testo)          # l'URL non finisce nel log
            self.assertFalse(archivio.e_postgres())            # nessun fallback silenzioso: resta SQLite

    def test_nessun_avviso_se_database_url_non_e_impostata_o_c_e_gia_il_nuovo_url(self):
        with mock.patch.dict(os.environ, {"LANGBRAIN_DB_BACKEND": "postgres", "LANGBRAIN_DB_URL": "postgresql://u:p@h/d",
                                          "DATABASE_URL": "postgresql://u:p@h/d"}):
            with self.assertNoLogs("app.core.archivio", level="WARNING"):
                archivio.verifica_configurazione()
        with mock.patch.dict(os.environ, {"DATABASE_URL": ""}):
            os.environ.pop("LANGBRAIN_DB_URL", None)
            os.environ.pop("LANGBRAIN_DB_BACKEND", None)
            with self.assertNoLogs("app.core.archivio", level="WARNING"):
                archivio.verifica_configurazione()

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
