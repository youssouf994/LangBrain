"""API di osservazione e intervento: stato fisico salvato, protezione dei modelli, stato delle migrazioni."""

import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core import stato_fisico
from app.core.errori_llm import ErroreLLM
from app.core.protezione_llm import protezione


class ApiOsservabilitaTest(unittest.TestCase):
    def setUp(self):
        from app.api.main import app
        self.client = TestClient(app)
        protezione.azzera()
        self.addCleanup(protezione.azzera)
        attiva = patch.object(stato_fisico, "attiva", return_value=True)   # nei test la persistenza è spenta per configurazione
        attiva.start()
        self.addCleanup(attiva.stop)
        self.addCleanup(lambda: asyncio.run(stato_fisico.assicura_e_svuota()))

    def test_stato_noto_di_un_dispositivo_comandato(self):
        with self.client:   # il lifespan prepara il database
            asyncio.run(stato_fisico.salva("luce_api_stato", "ON", "", stato_fisico.COMANDATO))
            r = self.client.get("/effectors/luce_api_stato/state")
            self.assertEqual(r.status_code, 200)
            corpo = r.json()
            self.assertEqual((corpo["device_id"], corpo["value"], corpo["source"]), ("luce_api_stato", "ON", "commanded"))
            self.assertIn("updated_at", corpo)

    def test_stato_noto_assente_e_404(self):
        with self.client:
            self.assertEqual(self.client.get("/effectors/mai_visto_api_stato/state").status_code, 404)

    def test_elenco_degli_stati_noti(self):
        with self.client:
            asyncio.run(stato_fisico.salva("luce_api_elenco", "OFF", "", stato_fisico.COMANDATO))
            corpo = self.client.get("/system/device-state").json()
            self.assertIn("persistence", corpo)
            self.assertIn("luce_api_elenco", [d["device_id"] for d in corpo["devices"]])

    def test_protezione_riporta_interruttori_e_budget(self):
        protezione.controlla("mistral", "m1")
        for _ in range(5):
            protezione.errore("mistral", "m1", ErroreLLM("PROVIDER_NON_RAGGIUNGIBILE", "giù", provider="mistral", modello="m1"))
        corpo = self.client.get("/llm/protection").json()
        circuito = next(c for c in corpo["circuits"] if (c["provider"], c["model"]) == ("mistral", "m1"))
        self.assertEqual(circuito["state"], "aperto")
        self.assertEqual(circuito["consecutive_errors"], 5)
        budget = next(b for b in corpo["budgets"] if b["provider"] == "mistral")
        self.assertEqual(budget["requests_last_hour"], 1)

    def test_il_reset_chiude_gli_interruttori_aperti_e_si_puo_limitare_a_un_provider(self):
        for provider in ("mistral", "ollama"):
            protezione.controlla(provider, "m")
            for _ in range(5):
                protezione.errore(provider, "m", ErroreLLM("ERRORE_PROVIDER", "x", provider=provider, modello="m"))
        r = self.client.post("/llm/protection/reset", params={"provider": "mistral"})
        self.assertEqual(r.json(), {"reset": 1})
        stati = {c["provider"]: c["state"] for c in self.client.get("/llm/protection").json()["circuits"]}
        self.assertEqual(stati, {"mistral": "chiuso", "ollama": "aperto"})
        self.assertEqual(self.client.post("/llm/protection/reset").json(), {"reset": 1})
        self.assertEqual(self.client.post("/llm/protection/reset").json(), {"reset": 0})

    def test_stato_delle_migrazioni(self):
        with self.client:
            corpo = self.client.get("/system/migrations").json()
        self.assertEqual(corpo["mode"], "auto")
        self.assertTrue(corpo["up_to_date"])
        self.assertEqual(corpo["current"], corpo["expected"])

    def test_i_permessi_delle_nuove_rotte(self):
        from app.core.ruoli import PERMESSI, Ruolo
        self.assertEqual(PERMESSI[("GET", "/llm/protection")], Ruolo.TIROCINANTE)
        self.assertEqual(PERMESSI[("GET", "/system/migrations")], Ruolo.TIROCINANTE)
        self.assertEqual(PERMESSI[("POST", "/llm/protection/reset")], Ruolo.MEDICO_DI_GUARDIA)


if __name__ == "__main__":
    unittest.main()
