"""Log JSON: campi fissi, contesto della richiesta, nessun segreto."""

import io
import json
import logging
import os
import unittest
from unittest import mock

from app.core import contesto, log_json

CAMPI = ["ts", "level", "apartment_id", "correlation_id", "actor", "msg"]


def _riga(messaggio="ciao", **kw):
    flusso = io.StringIO()
    gestore = logging.StreamHandler(flusso)
    gestore.setFormatter(log_json.FormattatoreJson())
    registro = logging.getLogger(f"prova.{id(flusso)}")
    registro.propagate = False
    registro.addHandler(gestore)
    registro.setLevel(logging.DEBUG)
    kw.pop("exc", None) or registro.info(messaggio)
    return flusso.getvalue()


class FormatoTest(unittest.TestCase):
    def test_una_riga_json_con_i_campi_del_contratto(self):
        with mock.patch.dict(os.environ, {"LANGBRAIN_APARTMENT_ID": "ap01"}):
            with contesto.contesto("cid-1", "user:anna@example.com"):
                testo = _riga("Comando applicato")
        self.assertEqual(testo.count("\n"), 1)
        dati = json.loads(testo)
        self.assertEqual(list(dati), CAMPI)
        self.assertEqual((dati["apartment_id"], dati["correlation_id"], dati["actor"], dati["msg"], dati["level"]),
                         ("ap01", "cid-1", "user:anna@example.com", "Comando applicato", "INFO"))
        self.assertRegex(dati["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_fuori_da_una_richiesta_contesto_vuoto_e_apartment_vuoto(self):
        with mock.patch.dict(os.environ, {"LANGBRAIN_APARTMENT_ID": ""}):
            dati = json.loads(_riga("x"))
        self.assertEqual((dati["apartment_id"], dati["correlation_id"], dati["actor"]), ("", None, None))

    def test_i_caratteri_italiani_restano_leggibili(self):
        self.assertIn("perché è già così", _riga("perché è già così"))

    def test_l_eccezione_finisce_nel_messaggio_e_resta_una_riga(self):
        flusso = io.StringIO()
        gestore = logging.StreamHandler(flusso)
        gestore.setFormatter(log_json.FormattatoreJson())
        registro = logging.getLogger("prova.eccezione")
        registro.propagate = False
        registro.addHandler(gestore)
        try:
            raise ValueError("guasto")
        except ValueError:
            registro.exception("errore")
        self.assertEqual(flusso.getvalue().count("\n"), 1)
        self.assertIn("ValueError: guasto", json.loads(flusso.getvalue())["msg"])


class SegretiTest(unittest.TestCase):
    def test_la_chiave_api_non_compare_mai(self):
        chiave = "chiave-segretissima-123456"
        with mock.patch.dict(os.environ, {"API_KEY": chiave, "OPENROUTER_API_KEY": "or-key-abcdef-999"}):
            testo = _riga(f"Header X-API-Key: {chiave} e {os.environ['OPENROUTER_API_KEY']}")
        self.assertNotIn(chiave, testo)
        self.assertNotIn("or-key-abcdef-999", testo)
        self.assertIn(log_json.MASCHERA, testo)

    def test_la_password_del_database_non_compare(self):
        with mock.patch.dict(os.environ, {"LANGBRAIN_DB_URL": "postgresql://u:pwd-del-db-77@h/d"}):
            testo = _riga("connessione a postgresql://u:pwd-del-db-77@h/d")
        self.assertNotIn("pwd-del-db-77", testo)

    def test_il_segreto_nel_traceback_e_oscurato(self):
        chiave = "chiave-nel-traceback-9999"
        flusso = io.StringIO()
        gestore = logging.StreamHandler(flusso)
        gestore.setFormatter(log_json.FormattatoreJson())
        registro = logging.getLogger("prova.traceback")
        registro.propagate = False
        registro.addHandler(gestore)
        with mock.patch.dict(os.environ, {"API_KEY": chiave}):
            try:
                raise RuntimeError(f"token rifiutato {chiave}")
            except RuntimeError:
                registro.exception("fallita")
        self.assertNotIn(chiave, flusso.getvalue())

    def test_valori_corti_non_sono_segreti(self):
        with mock.patch.dict(os.environ, {"LANGBRAIN_REQUIRE_API_KEY": "1"}):
            self.assertIn("1", json.loads(_riga("valore 1"))["msg"])


class ConfiguraTest(unittest.TestCase):
    def setUp(self):
        self.radice = logging.getLogger()
        self.gestori = list(self.radice.handlers)
        self.livello = self.radice.level
        self.addCleanup(self._ripristina)

    def _ripristina(self):
        self.radice.handlers = self.gestori
        self.radice.setLevel(self.livello)

    def test_predefinito_non_cambia_nulla(self):
        with mock.patch.dict(os.environ, {"LANGBRAIN_LOG_FORMAT": ""}):
            self.assertFalse(log_json.configura_log())
            self.assertIsNone(log_json.configurazione_uvicorn())
        self.assertEqual(self.radice.handlers, self.gestori)

    def test_con_json_i_log_di_uvicorn_passano_dalla_radice_in_json(self):
        flusso = io.StringIO()
        with mock.patch.dict(os.environ, {"LANGBRAIN_LOG_FORMAT": "json"}):
            self.assertTrue(log_json.configura_log(stream=flusso))
            self.assertTrue(log_json.configura_log(stream=flusso))  # idempotente: un solo gestore nostro
            self.assertEqual(sum(1 for g in self.radice.handlers if getattr(g, "_langbrain_json", False)), 1)
            logging.getLogger("uvicorn.error").info("avviato")
        riga = json.loads(flusso.getvalue().strip().splitlines()[-1])
        self.assertEqual((list(riga), riga["msg"]), (CAMPI, "avviato"))


if __name__ == "__main__":
    unittest.main()
