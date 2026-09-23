import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.main import app
from test_ruoli import blocca_i_gestori

CHIAVE = "chiave-di-prova-123"


class ChiaveApiTest(unittest.TestCase):
    """Non avvia il lifespan. I gestori sono sostituiti da stub: un'autenticazione rotta non può eseguire operazioni vere."""

    def setUp(self):
        blocca_i_gestori(self)
        self.client = TestClient(app)

    def con_chiave(self):
        return patch.dict(os.environ, {"API_KEY": CHIAVE})

    def test_senza_api_key_configurata_l_api_resta_aperta(self):
        with patch.dict(os.environ, {"API_KEY": ""}):
            self.assertEqual(self.client.get("/hitl/config").status_code, 200)

    def test_endpoint_protetto_rifiuta_richieste_senza_chiave(self):
        with self.con_chiave():
            risposta = self.client.get("/hitl/config")
        self.assertEqual(risposta.status_code, 401)
        self.assertEqual(risposta.headers["www-authenticate"], "ApiKey")

    def test_endpoint_protetto_rifiuta_una_chiave_errata(self):
        with self.con_chiave():
            self.assertEqual(self.client.get("/hitl/config", headers={"X-API-Key": "sbagliata"}).status_code, 401)

    def test_endpoint_protetto_accetta_la_chiave_corretta(self):
        with self.con_chiave():
            self.assertEqual(self.client.get("/hitl/config", headers={"X-API-Key": CHIAVE}).status_code, 200)

    def test_reset_di_sistema_e_protetto(self):
        with self.con_chiave():
            self.assertEqual(self.client.delete("/system/reset").status_code, 401)
            self.assertEqual(self.client.delete("/system/reset", headers={"X-API-Key": "sbagliata"}).status_code, 401)

    def test_la_root_resta_pubblica_per_l_healthcheck(self):
        with self.con_chiave():
            self.assertEqual(self.client.get("/").status_code, 200)

    def test_tutti_gli_endpoint_tranne_la_root_richiedono_la_chiave(self):
        with self.con_chiave():
            for rotta in app.routes:
                metodi = getattr(rotta, "methods", None)
                if not metodi or rotta.path in ("/", "/demo", "/energia") or not hasattr(rotta, "dependant"):
                    continue
                for metodo in metodi - {"HEAD", "OPTIONS"}:
                    percorso = rotta.path.replace("{device_id}", "x").replace("{agent_name}", "x").replace("{target}", "x").replace("{entita_id}", "x")
                    self.assertEqual(
                        self.client.request(metodo, percorso).status_code, 401, f"{metodo} {rotta.path} non protetto"
                    )

    def test_la_documentazione_swagger_resta_raggiungibile(self):
        with self.con_chiave():
            self.assertEqual(self.client.get("/docs").status_code, 200)
            schema = self.client.get("/openapi.json").json()
        self.assertIn("APIKeyHeader", schema["components"]["securitySchemes"])


if __name__ == "__main__":
    unittest.main()
