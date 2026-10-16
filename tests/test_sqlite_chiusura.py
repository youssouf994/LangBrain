"""`connetti`: come `sqlite3.connect` nel `with` (commit/rollback), più la chiusura all'uscita."""

import os
import sqlite3
import tempfile
import unittest

from sqlite_chiusura import ConnessioneChiusaAllUscita, connetti


class ConnettiTest(unittest.TestCase):
    def setUp(self):
        self.cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self.cartella.cleanup)  # su Windows fallisce se il file è rimasto aperto
        self.percorso = os.path.join(self.cartella.name, "t.db")

    def test_all_uscita_del_with_la_connessione_e_chiusa(self):
        with connetti(self.percorso) as db:
            self.assertIsInstance(db, ConnessioneChiusaAllUscita)
            db.execute("CREATE TABLE t (x INTEGER)")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")  # Cannot operate on a closed database

    def test_commit_all_uscita_normale(self):
        with connetti(self.percorso) as db:
            db.execute("CREATE TABLE t (x INTEGER)")
            db.execute("INSERT INTO t VALUES (1)")
        with connetti(self.percorso) as db:
            self.assertEqual(db.execute("SELECT x FROM t").fetchall(), [(1,)])

    def test_rollback_e_chiusura_se_c_e_un_errore(self):
        with connetti(self.percorso) as db:
            db.execute("CREATE TABLE t (x INTEGER)")
        with self.assertRaises(ValueError):
            with connetti(self.percorso) as db:
                db.execute("INSERT INTO t VALUES (2)")
                raise ValueError("a metà")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")
        with connetti(self.percorso) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM t").fetchone(), (0,))

    def test_il_file_si_puo_cancellare_subito(self):
        with connetti(self.percorso) as db:
            db.execute("CREATE TABLE t (x INTEGER)")
        os.remove(self.percorso)  # su Windows darebbe WinError 32 con una connessione ancora aperta
        self.assertFalse(os.path.exists(self.percorso))

    def test_le_opzioni_di_sqlite3_connect_passano(self):
        with connetti(self.percorso, timeout=5) as db:
            self.assertEqual(db.execute("SELECT 1").fetchone(), (1,))


if __name__ == "__main__":
    unittest.main()
