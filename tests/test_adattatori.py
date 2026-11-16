"""Adattatore HTTP per dispositivi veri (`app/tools/adattatori.py`) e scelta dell'adattatore da `configurazione.toml`."""

import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.core import contesto, metriche
from app.core.configurazione import ErroreConfigurazione, costruisci_configurazione, imposta_configurazione
from app.core.risultati import comanda_tool, leggi_tool
from app.tools import effectors
from app.tools.adattatori import EffettoreHttp

OPZIONI = {"url": "http://gateway.test/devices/serratura", "token_env": "GATEWAY_TOKEN_PROVA"}


def trasporto(gestore):
    return httpx.MockTransport(gestore)


def effettore(gestore, **extra):
    return EffettoreHttp("serratura", {**OPZIONI, **extra}, transport=trasporto(gestore))


class LetturaTest(unittest.IsolatedAsyncioTestCase):
    async def test_legge_il_campo_value_di_un_json(self):
        e = effettore(lambda r: httpx.Response(200, json={"value": "LOCKED"}))
        self.assertEqual(await e.get_tool_value(), "LOCKED")
        self.assertTrue(e.online)
        self.assertIsNotNone(e.ultimo_contatto)

    async def test_accetta_anche_uno_scalare_json_o_un_testo(self):
        self.assertEqual(await effettore(lambda r: httpx.Response(200, json=21.5)).get_tool_value(), 21.5)
        self.assertEqual(await effettore(lambda r: httpx.Response(200, text="OPEN")).get_tool_value(), "OPEN")

    async def test_campo_valore_configurabile_e_assente(self):
        e = effettore(lambda r: httpx.Response(200, json={"state": "ON"}), campo_valore="state")
        self.assertEqual(await e.get_tool_value(), "ON")
        r = await leggi_tool(effettore(lambda r: httpx.Response(200, json={"altro": 1})), "serratura")
        self.assertEqual((r["success"], r["error_type"]), (False, "BAD_RESPONSE"))

    async def test_invia_il_token_dall_ambiente_e_mai_senza(self):
        viste = []
        e = effettore(lambda r: viste.append(dict(r.headers)) or httpx.Response(200, json={"value": 1}))
        with patch.dict(os.environ, {"GATEWAY_TOKEN_PROVA": "segreto-di-prova"}):
            await e.get_tool_value()
        os.environ.pop("GATEWAY_TOKEN_PROVA", None)
        await e.get_tool_value()
        self.assertEqual(viste[0]["authorization"], "Bearer segreto-di-prova")
        self.assertNotIn("authorization", viste[1])

    async def test_propaga_correlation_id_e_traceparent(self):
        viste = []
        e = effettore(lambda r: viste.append(dict(r.headers)) or httpx.Response(200, json={"value": 1}))
        with contesto.contesto("corr-1", None, ("a" * 32, "b" * 16)):
            await e.get_tool_value()
        self.assertEqual(viste[0]["x-correlation-id"], "corr-1")
        self.assertRegex(viste[0]["traceparent"], r"^00-" + "a" * 32 + r"-[0-9a-f]{16}-01$")
        self.assertNotEqual(viste[0]["traceparent"].split("-")[2], "b" * 16)   # un nuovo span nella stessa traccia

    async def test_errori_strutturati_per_il_padre(self):
        casi = {
            "http 500": (lambda r: httpx.Response(500), "DEVICE_ERROR"),
            "http 404": (lambda r: httpx.Response(404), "READ_REJECTED"),
        }
        for nome, (gestore, atteso) in casi.items():
            with self.subTest(nome):
                r = await leggi_tool(effettore(gestore), "serratura")
                self.assertEqual((r["success"], r["status"], r["error_type"]), (False, "TOOL_ERROR", atteso))

    async def test_rete_giu_e_timeout(self):
        def giu(richiesta):
            raise httpx.ConnectError("rete giù")

        def lento(richiesta):
            raise httpx.ReadTimeout("lento")

        self.assertEqual((await leggi_tool(effettore(giu), "s"))["error_type"], "DEVICE_UNREACHABLE")
        self.assertEqual((await leggi_tool(effettore(lento), "s"))["error_type"], "TIMEOUT")

    async def test_una_lettura_si_ripete_una_volta(self):
        n = []

        def instabile(richiesta):
            n.append(1)
            if len(n) == 1:
                raise httpx.ReadTimeout("una volta")
            return httpx.Response(200, json={"value": "OK"})

        self.assertEqual(await effettore(instabile).get_tool_value(), "OK")
        self.assertEqual(len(n), 2)


class ComandoTest(unittest.IsolatedAsyncioTestCase):
    async def test_il_comando_porta_valore_command_id_e_chiave_di_idempotenza(self):
        viste = []

        def gestore(r):
            viste.append((r.method, str(r.url), json.loads(r.content), r.headers["idempotency-key"]))
            return httpx.Response(204)

        self.assertTrue(await effettore(gestore).set_tool_value("UNLOCKED"))
        metodo, url, corpo, chiave = viste[0]
        self.assertEqual((metodo, url, corpo["value"]), ("POST", OPZIONI["url"], "UNLOCKED"))
        self.assertEqual(corpo["command_id"], chiave)

    async def test_url_comando_separato(self):
        viste = []
        e = effettore(lambda r: viste.append(str(r.url)) or httpx.Response(200), url_comando="http://gateway.test/cmd")
        await e.set_tool_value("ON")
        self.assertEqual(viste, ["http://gateway.test/cmd"])

    async def test_un_rifiuto_4xx_non_e_un_guasto(self):
        e = effettore(lambda r: httpx.Response(409), soglia_errori=1)
        for _ in range(3):
            r = await comanda_tool(e, "serratura", "UNLOCKED")
            self.assertEqual((r["success"], r["error_type"]), (False, "COMMAND_REJECTED"))
        self.assertTrue(e.online)   # il gateway risponde: l'interruttore resta chiuso

    async def test_il_comando_si_ripete_solo_se_la_richiesta_non_e_partita(self):
        chiavi = []

        def non_connette(r):
            chiavi.append(r.headers["idempotency-key"])
            raise httpx.ConnectError("rifiutata")

        r = await comanda_tool(effettore(non_connette), "serratura", "ON")
        self.assertEqual((r["success"], r["error_type"]), (False, "DEVICE_UNREACHABLE"))
        self.assertEqual(len(chiavi), 2)
        self.assertEqual(chiavi[0], chiavi[1])   # stesso command_id sul secondo tentativo

        chiamate = []

        def timeout(r):
            chiamate.append(1)
            raise httpx.ReadTimeout("forse eseguito")

        r = await comanda_tool(effettore(timeout), "serratura", "ON")
        self.assertEqual((r["error_type"], len(chiamate)), ("TIMEOUT", 1))   # mai ripetuto alla cieca

    async def test_comando_riuscito_per_la_pipeline_standard(self):
        r = await comanda_tool(effettore(lambda r: httpx.Response(200)), "serratura", "LOCKED")
        self.assertTrue(r["success"])


class InterruttoreDispositivoTest(unittest.IsolatedAsyncioTestCase):
    async def test_dopo_la_soglia_il_dispositivo_non_viene_piu_chiamato(self):
        n = []
        e = effettore(lambda r: n.append(1) or httpx.Response(503), soglia_errori=2, pausa_secondi=30)
        for _ in range(2):
            self.assertEqual((await leggi_tool(e, "s"))["error_type"], "DEVICE_ERROR")
        r = await leggi_tool(e, "s")
        self.assertEqual((r["error_type"], len(n)), ("DEVICE_CIRCUIT_OPEN", 2))
        self.assertGreater(r["details"]["riprova_tra_secondi"], 0)
        self.assertFalse(e.online)
        await e.aggiorna_metriche()
        self.assertIn('langbrain_device_circuit_state{device="serratura"} 2', await metriche.esporta())

    async def test_le_operazioni_sono_misurate(self):
        metriche.azzera()
        await leggi_tool(effettore(lambda r: httpx.Response(200, json=1)), "serratura")
        await comanda_tool(effettore(lambda r: httpx.Response(500)), "serratura", "x")
        testo = await metriche.esporta()
        self.assertIn('langbrain_device_operations_total{device="serratura",operation="read",outcome="ok"} 1', testo)
        self.assertIn('langbrain_device_operations_total{device="serratura",operation="command",outcome="DEVICE_ERROR"} 1', testo)


class ConfigurazioneAdattatoreTest(unittest.TestCase):
    def test_il_dispositivo_con_adattatore_http_riceve_un_effettore_http(self):
        nome = "lampada_prova_adattatore"
        imposta_configurazione(costruisci_configurazione({"dispositivi": {nome: {
            "adattatore": "http", "url": "http://x/l", "timeout_secondi": 2, "soglia_errori": 4, "unita": "%"}}}))
        self.addCleanup(imposta_configurazione, None)
        self.addCleanup(effettori_pulisci, nome)
        tool = effectors.get_tool(nome)
        self.assertIsInstance(tool, EffettoreHttp)
        self.assertEqual((tool.timeout, tool.interruttore.soglia, tool.unit, tool.simulato), (2.0, 4, "%", False))

    def test_senza_adattatore_resta_simulato(self):
        nome = "lampada_simulata_prova"
        imposta_configurazione(costruisci_configurazione({"dispositivi": {nome: {"valori": ["ON", "OFF"]}}}))
        self.addCleanup(imposta_configurazione, None)
        self.addCleanup(effettori_pulisci, nome)
        self.assertTrue(effectors.get_tool(nome).simulato)

    def test_nomi_inglesi(self):
        c = costruisci_configurazione({"effectors": {"lamp": {"adapter": "http", "url": "https://x", "command_url": "https://x/c",
                                                             "timeout_seconds": 3, "error_threshold": 2, "pause_seconds": 5,
                                                             "value_field": "v"}}})
        o = c.regole[0].opzioni_adattatore
        self.assertEqual((c.regole[0].adattatore, o["url_comando"], o["timeout_secondi"], o["soglia_errori"], o["pausa_secondi"], o["campo_valore"]),
                         ("http", "https://x/c", 3.0, 2, 5.0, "v"))

    def test_configurazioni_non_valide(self):
        non_valide = [
            {"adattatore": "mqtt", "url": "http://x"},
            {"adattatore": "http"},
            {"adattatore": "http", "url": "ftp://x"},
            {"adattatore": "http", "url": "http://x", "timeout_secondi": 0},
            {"adattatore": "http", "url": "http://x", "soglia_errori": 0},
            {"adattatore": "http", "url": "http://x", "token_env": ""},
        ]
        for regola in non_valide:
            with self.subTest(regola=regola), self.assertRaises(ErroreConfigurazione):
                costruisci_configurazione({"dispositivi": {"d": regola}})
        with self.assertRaises(ErroreConfigurazione):   # un pattern non può avere un adattatore solo
            costruisci_configurazione({"dispositivi": {"luci_*": {"adattatore": "http", "url": "http://x"}}})


def effettori_pulisci(nome):
    effectors._TOOL_REGISTRY.pop(nome, None)


if __name__ == "__main__":
    unittest.main()
