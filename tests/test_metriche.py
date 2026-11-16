"""Metriche Prometheus (`app/core/metriche.py`, `GET /metrics`) e propagazione della traccia W3C (`app/core/contesto.py`)."""

import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.core import contesto, metriche, multiprocesso
from app.core.metriche import MiddlewareMetriche


class FormatoTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        metriche.azzera()

    async def test_contatori_indicatori_e_istogrammi_in_formato_prometheus(self):
        metriche.incrementa("langbrain_llm_calls_total", provider="mistral", model="m", outcome="ok")
        metriche.incrementa("langbrain_llm_calls_total", 2, provider="mistral", model="m", outcome="ok")
        metriche.imposta("langbrain_device_online", 1, device="luce")
        metriche.osserva("langbrain_llm_call_duration_seconds", 0.03, provider="mistral")
        metriche.osserva("langbrain_llm_call_duration_seconds", 7.0, provider="mistral")
        testo = await metriche.esporta()
        self.assertIn("# TYPE langbrain_llm_calls_total counter", testo)
        self.assertIn('langbrain_llm_calls_total{model="m",outcome="ok",provider="mistral"} 3', testo)
        self.assertIn('langbrain_device_online{device="luce"} 1', testo)
        self.assertIn("# TYPE langbrain_llm_call_duration_seconds histogram", testo)
        self.assertIn('langbrain_llm_call_duration_seconds_bucket{provider="mistral",le="0.05"} 1', testo)
        self.assertIn('langbrain_llm_call_duration_seconds_bucket{provider="mistral",le="10"} 2', testo)
        self.assertIn('langbrain_llm_call_duration_seconds_bucket{provider="mistral",le="+Inf"} 2', testo)
        self.assertIn('langbrain_llm_call_duration_seconds_count{provider="mistral"} 2', testo)
        self.assertIn('langbrain_llm_call_duration_seconds_sum{provider="mistral"} 7.03', testo)
        self.assertTrue(testo.endswith("\n"))

    async def test_i_secchi_sono_cumulativi(self):
        for valore in (0.001, 0.02, 0.3):
            metriche.osserva("langbrain_graph_node_duration_seconds", valore, node="n")
        righe = {r.split(" ")[0]: float(r.split(" ")[1]) for r in (await metriche.esporta()).splitlines()
                 if r.startswith("langbrain_graph_node_duration_seconds_bucket")}
        valori = list(righe.values())
        self.assertEqual(valori, sorted(valori))
        self.assertEqual(valori[-1], 3)

    async def test_i_valori_delle_etichette_sono_protetti(self):
        metriche.incrementa("langbrain_x_total", chi='a"b\\c\nd')
        self.assertIn('langbrain_x_total{chi="a\\"b\\\\c\\nd"} 1', await metriche.esporta())

    async def test_un_collettore_che_fallisce_non_rompe_la_lettura(self):
        async def rotto():
            raise RuntimeError("guasto")

        metriche.registra_collettore(rotto)
        self.addCleanup(metriche._collettori.remove, rotto)
        metriche.incrementa("langbrain_x_total")
        self.assertIn("langbrain_x_total 1", await metriche.esporta())

    async def test_la_somma_tra_processi_somma_contatori_e_istogrammi_e_prende_il_massimo_degli_indicatori(self):
        metriche.incrementa("langbrain_x_total", 2, a="1")
        metriche.imposta("langbrain_device_online", 0, device="d")
        metriche.osserva("langbrain_graph_node_duration_seconds", 0.2, node="n")
        altro = json.loads(json.dumps(metriche.istantanea()))   # come arriva dal database
        dati = metriche.somma([metriche.istantanea(), altro])
        self.assertEqual(dati["contatori"][("langbrain_x_total", (("a", "1"),))], 4)
        self.assertEqual(dati["indicatori"][("langbrain_device_online", (("device", "d"),))], 0)
        riga = dati["istogrammi"][("langbrain_graph_node_duration_seconds", (("node", "n"),))]
        self.assertEqual((riga[0], round(riga[1], 3)), (2, 0.4))
        altro["indicatori"][0][2] = 1
        dati = metriche.somma([metriche.istantanea(), altro])
        self.assertEqual(dati["indicatori"][("langbrain_device_online", (("device", "d"),))], 1)


class PubblicazioneTraProcessiTest(unittest.IsolatedAsyncioTestCase):
    async def test_le_istantanee_degli_altri_processi_si_sommano_a_quella_del_processo(self):
        metriche.azzera()
        metriche.incrementa("langbrain_x_total", 1)
        altro = {"contatori": [["langbrain_x_total", [], 5.0]], "indicatori": [], "istogrammi": []}
        with patch.dict("os.environ", {"LANGBRAIN_SHARED_STATE": "1"}):
            await multiprocesso.scrivi(metriche.PREFISSO_CHIAVE + "altro-processo", altro)
            await multiprocesso.scrivi(metriche.PREFISSO_CHIAVE + "scaduto", {"contatori": [["langbrain_x_total", [], 100.0]],
                                                                               "indicatori": [], "istogrammi": []})
            motore = await multiprocesso._motore()
            from sqlalchemy import update
            from app.db import schema
            async with motore.begin() as db:   # un processo morto da tempo non conta più
                await db.execute(update(schema.runtime_state).where(schema.runtime_state.c.key == metriche.PREFISSO_CHIAVE + "scaduto")
                                 .values(updated_at=0.0))
            testo = await metriche.esporta()
            await metriche.pubblica_istantanea()
        self.assertIn("langbrain_x_total 6", testo)
        self.assertIn("langbrain_workers 2", testo)


class HttpTest(unittest.TestCase):
    def setUp(self):
        metriche.azzera()
        from app.api.main import app
        self.client = TestClient(app)

    def test_metrics_risponde_in_testo_prometheus_e_conta_le_richieste(self):
        self.client.get("/effectors/non_elencato_xyz")
        self.client.get("/rotta/che/non/esiste")
        r = self.client.get("/metrics")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assertIn('langbrain_http_requests_total{method="GET",route="/effectors/{device_id}",status="', r.text)
        self.assertIn('route="non_trovata",status="404"', r.text)
        self.assertIn("langbrain_http_request_duration_seconds_count", r.text)
        self.assertIn("langbrain_database_up 1", r.text)
        self.assertIn('langbrain_build_info{version="', r.text)
        self.assertNotIn('route="/metrics"', r.text)   # chi legge le metriche non le gonfia

    def test_la_rotta_ha_un_ruolo_e_richiede_la_chiave_quando_e_configurata(self):
        from app.core.ruoli import Ruolo, ruolo_minimo
        self.assertEqual(ruolo_minimo("GET", "/metrics"), Ruolo.TIROCINANTE)
        with patch.dict("os.environ", {"API_KEY_TIROCINANTE": "chiave-lettura-prova"}):
            self.assertEqual(self.client.get("/metrics").status_code, 401)
            self.assertEqual(self.client.get("/metrics", headers={"X-API-Key": "chiave-lettura-prova"}).status_code, 200)

    def test_si_spegne_con_langbrain_metrics_0(self):
        with patch.dict("os.environ", {"LANGBRAIN_METRICS": "0"}):
            self.client.get("/effectors/non_elencato_xyz")
        self.assertNotIn("langbrain_http_requests_total", self.client.get("/metrics").text)


class TracciaTest(unittest.TestCase):
    def setUp(self):
        from app.api.main import app
        self.client = TestClient(app)
        self.trace = "4bf92f3577b34da6a3ce929d0e0e4736"

    def test_senza_traceparent_ne_nasce_uno_nuovo_e_il_correlation_id_resta_un_uuid(self):
        r = self.client.get("/")
        self.assertRegex(r.headers["traceparent"], r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")
        self.assertRegex(r.headers["x-correlation-id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-")

    def test_un_traceparent_valido_mantiene_la_traccia_con_un_nuovo_span_e_diventa_correlation_id(self):
        r = self.client.get("/", headers={"traceparent": f"00-{self.trace}-00f067aa0ba902b7-01"})
        versione, trace, span, flag = r.headers["traceparent"].split("-")
        self.assertEqual((versione, trace, flag), ("00", self.trace, "01"))
        self.assertNotEqual(span, "00f067aa0ba902b7")
        self.assertEqual(r.headers["x-correlation-id"], self.trace)

    def test_il_correlation_id_esplicito_ha_la_precedenza_ma_la_traccia_resta(self):
        r = self.client.get("/", headers={"traceparent": f"00-{self.trace}-00f067aa0ba902b7-01", "X-Correlation-Id": "mio-id"})
        self.assertEqual(r.headers["x-correlation-id"], "mio-id")
        self.assertIn(self.trace, r.headers["traceparent"])

    def test_un_traceparent_non_valido_si_ignora(self):
        for valore in ("rotto", f"01-{self.trace}-00f067aa0ba902b7-01", f"00-{'0' * 32}-00f067aa0ba902b7-01", f"00-{self.trace}-{'0' * 16}-01"):
            with self.subTest(valore=valore):
                r = self.client.get("/", headers={"traceparent": valore})
                self.assertNotEqual(r.headers["traceparent"].split("-")[1], "0" * 32)
                self.assertNotEqual(r.headers["x-correlation-id"], valore)

    def test_la_chiamata_in_uscita_continua_la_traccia(self):
        self.assertIsNone(contesto.traceparent_uscente())
        with contesto.contesto("c", None, (self.trace, "1" * 16)):
            uscente = contesto.traceparent_uscente()
        self.assertRegex(uscente, rf"^00-{self.trace}-[0-9a-f]{{16}}-01$")
        self.assertNotIn("1" * 16, uscente)

    def test_un_fatto_interno_ha_una_traccia_propria(self):
        with contesto.fatto_interno():
            primo = contesto.trace_id_corrente()
        with contesto.fatto_interno():
            self.assertNotEqual(primo, contesto.trace_id_corrente())
        self.assertIsNone(contesto.trace_id_corrente())


if __name__ == "__main__":
    unittest.main()
