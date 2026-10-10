"""
Simulazione energetica: motore (determinismo, bilanci, limiti fisici, eventi), esecutore in background e rotte API.
Nessun LLM e nessun database reale: la simulazione vive in memoria.
"""

import asyncio
import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.simulazione.esecutore import EsecutoreSimulazione, esecutore
from app.simulazione.motore import Simulatore
from app.simulazione.scenari import scenario_generato, scenario_italia
from app.simulazione.verifiche import verifica_invarianti

TOLLERANZA = 1e-3


def italia(**kw) -> Simulatore:
    parametri = {"seed": 42, "passo_minuti": 60, "durata_ore": 24 * 7}
    parametri.update(kw)
    return Simulatore(scenario_italia(), **parametri)


def verifica_passo(test: unittest.TestCase, sim: Simulatore, linepack_prima: float | None = None) -> None:
    """Bilanci e limiti fisici dopo un passo (vedi app.simulazione.verifiche)."""
    test.assertEqual(verifica_invarianti(sim, TOLLERANZA, linepack_prima), [], sim.tempo)


class MotoreTest(unittest.TestCase):
    def test_stesso_seed_stessa_storia(self):
        a, b = italia(), italia()
        a.avanza(10 ** 6)
        b.avanza(10 ** 6)
        self.assertEqual(a.istantanea(), b.istantanea())
        self.assertEqual(a.riepilogo(), b.riepilogo())
        self.assertEqual(list(a.storico_eventi), list(b.storico_eventi))

    def test_seed_diverso_storia_diversa(self):
        a, b = italia(), italia(seed=43)
        a.avanza(48)
        b.avanza(48)
        self.assertNotEqual(a.riepilogo()["domanda_twh"], b.riepilogo()["domanda_twh"])

    def test_bilanci_e_limiti_a_ogni_passo(self):
        for passo in (15, 60):
            sim = italia(passo_minuti=passo, durata_ore=24 * 10, inizio=datetime(2026, 7, 1))
            while not sim.finita:
                sim.avanza(1)
                verifica_passo(self, sim)

    def test_bilancio_del_gas_con_linepack(self):
        sim = italia(durata_ore=24 * 5, inizio=datetime(2026, 1, 10))
        while not sim.finita:
            prima = sum(z.linepack for z in sim.scenario.zone)
            sim.avanza(1)
            verifica_passo(self, sim, linepack_prima=prima)

    def test_crisi_gas_invernale_usa_il_linepack_e_riduce_le_centrali(self):
        # Tre giorni di crisi, poi due di ripresa in cui il linepack si ricostituisce
        sim = italia(durata_ore=24 * 5, inizio=datetime(2026, 1, 20))
        sim.inietta_evento("ondata_freddo", durata_ore=72)
        for ingresso in ("ingresso:mazara", "ingresso:passo_gries", "ingresso:tarvisio", "ingresso:melendugno", "ingresso:porto_viro"):
            sim.inietta_evento("crisi_gas", ingresso, 72)
        tipi_anomalie, centrali_ridotte = set(), False
        while not sim.finita:
            prima = sum(z.linepack for z in sim.scenario.zone)
            sim.avanza(1)
            verifica_passo(self, sim, linepack_prima=prima)
            tipi_anomalie |= {a["tipo"] for a in sim.anomalie}
            centrali_ridotte = centrali_ridotte or min(z.fattore_gas for z in sim.scenario.zone) < 1.0
        self.assertIn("pressione_gas_bassa", tipi_anomalie)
        self.assertIn("gas_non_servito", tipi_anomalie)
        self.assertTrue(centrali_ridotte)
        self.assertTrue(all(z.pressione > 60 for z in sim.scenario.zone if z.rete_gas), "il linepack non si è ricostituito")

    def test_la_riserva_bassa_si_misura_per_zona(self):
        # Sicilia isolata: il resto d'Italia ha capacità in abbondanza, ma la zona debole è la Sicilia.
        sim = italia(durata_ore=24 * 3, inizio=datetime(2026, 7, 11, 12))
        sim.inietta_evento("ondata_calore", durata_ore=72)
        for entita in ("linea:CALA-SICI", "centrale:priolo_gargallo"):
            sim.inietta_evento("guasto", entita, 72)
        riserva_bassa = False
        while not sim.finita:
            sim.avanza(1)
            if "riserva_bassa:SICI" in {a["id"] for a in sim.anomalie}:
                riserva_bassa = True
                self.assertEqual(sim.nazionale["zona_margine_minimo"], "Sicilia")
                self.assertLess(sim.nazionale["margine_riserva"], 0.10)
                self.assertGreater(sim.nazionale["margine_riserva_nazionale"], 0.25)
        self.assertTrue(riserva_bassa)

    def test_un_anno_di_italia_ha_ordini_di_grandezza_plausibili(self):
        sim = italia(durata_ore=8760)
        sim.avanza(10 ** 6)
        r = sim.riepilogo()
        self.assertTrue(sim.finita)
        self.assertEqual(len(sim.serie_giorni), 365)
        self.assertTrue(290 <= r["domanda_twh"] <= 330, r["domanda_twh"])
        self.assertTrue(55 <= r["gas_domanda_mld_m3"] <= 70, r["gas_domanda_mld_m3"])
        self.assertTrue(0.25 <= r["quota_rinnovabili_fer"] <= 0.5, r["quota_rinnovabili_fer"])
        self.assertTrue(30 <= r["produzione_twh"]["solare"] <= 55, r["produzione_twh"])
        self.assertLess(r["distacco_mwh"] / (r["domanda_twh"] * 1e6), 0.001)
        self.assertGreater(sum(r["eventi_per_tipo"].values()), 50)
        # D'estate si inietta negli stoccaggi, d'inverno si eroga
        stoccaggio = {g["giorno"][:7]: g["stoccaggio_gas"] for g in sim.serie_giorni}
        self.assertGreater(stoccaggio["2026-10"], stoccaggio["2026-04"])

    def test_rete_generata_rispetta_bilanci(self):
        sim = Simulatore(scenario_generato(12, 150, 90, seed=5), seed=3, durata_ore=24 * 3)
        while not sim.finita:
            sim.avanza(1)
            verifica_passo(self, sim)
        self.assertEqual(len(sim.scenario.zone), 12)
        self.assertEqual(len(sim.scenario.citta), 150)

    def test_la_rete_generata_e_riproducibile(self):
        a, b = scenario_generato(10, 50, 30, seed=9), scenario_generato(10, 50, 30, seed=9)
        self.assertEqual([c.potenza_mw for c in a.centrali], [c.potenza_mw for c in b.centrali])
        self.assertEqual([l.id for l in a.linee], [l.id for l in b.linee])

    def test_parametri_non_validi(self):
        with self.assertRaises(ValueError):
            italia(passo_minuti=7)
        with self.assertRaises(ValueError):
            italia(durata_ore=0)

    def test_la_durata_ferma_la_simulazione(self):
        sim = italia(durata_ore=5)
        self.assertEqual(sim.avanza(100), 5)
        self.assertTrue(sim.finita)
        self.assertEqual(sim.avanza(1), 0)


class EventiTest(unittest.TestCase):
    def test_guasto_e_ripristino_di_una_centrale(self):
        sim = italia()
        sim.avanza(1)
        evento = sim.inietta_evento("guasto", "centrale:montalto_di_castro", 12)
        sim.avanza(1)
        centrale = sim.entita["centrale:montalto_di_castro"][1]
        self.assertEqual(centrale.disponibile_mw, 0.0)
        self.assertIn("guasto:centrale:montalto_di_castro", {a["id"] for a in sim.anomalie})
        self.assertEqual(evento["tipo"], "guasto")

        sim.inietta_evento("ripristino", "centrale:montalto_di_castro")
        sim.avanza(1)
        self.assertGreater(centrale.disponibile_mw, 0.0)
        self.assertNotIn("guasto:centrale:montalto_di_castro", {a["id"] for a in sim.anomalie})

    def test_il_guasto_scade_da_solo(self):
        sim = italia()
        sim.inietta_evento("guasto", "linea:CALA-SICI", 3)
        sim.avanza(1)
        self.assertEqual(sim.entita["linea:CALA-SICI"][1].flusso_mw, 0.0)
        sim.avanza(4)
        self.assertEqual(sim.entita["linea:CALA-SICI"][1].guasto_fino, -1)
        self.assertFalse([e for e in sim.eventi_attivi if e["entita"] == "linea:CALA-SICI"])

    def test_crisi_gas_riduce_l_ingresso(self):
        sim = italia(inizio=datetime(2026, 1, 15))
        sim.inietta_evento("crisi_gas", "ingresso:mazara", 48)
        sim.avanza(3)
        ingresso = sim.entita["ingresso:mazara"][1]
        self.assertLessEqual(ingresso.portata, ingresso.portata_max * 0.3 + TOLLERANZA)

    def test_l_ondata_di_freddo_alza_la_domanda_di_gas(self):
        base, fredda = italia(inizio=datetime(2026, 2, 1)), italia(inizio=datetime(2026, 2, 1))
        fredda.inietta_evento("ondata_freddo", durata_ore=48)
        base.avanza(24)
        fredda.avanza(24)
        self.assertGreater(fredda.nazionale["gas_domanda"], base.nazionale["gas_domanda"] * 1.1)

    def test_eventi_non_validi(self):
        sim = italia()
        for tipo, entita in [("terremoto", None), ("guasto", "citta:milano"), ("guasto", "inesistente"),
                             ("crisi_gas", "centrale:turbigo"), ("ripristino", "centrale:turbigo")]:
            with self.subTest(tipo=tipo, entita=entita), self.assertRaises(ValueError):
                sim.inietta_evento(tipo, entita)

    def test_descrizione_di_un_entita(self):
        sim = italia()
        sim.avanza(3)
        d = sim.descrivi_entita("citta:roma")
        self.assertEqual(d["categoria"], "citta")
        self.assertGreater(d["valori"]["domanda_mw"], 0)
        self.assertEqual(len(d["storico_orario"]), 3)
        with self.assertRaises(KeyError):
            sim.descrivi_entita("citta:atlantide")


class EsecutoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_alla_velocita_massima_arriva_in_fondo(self):
        e = EsecutoreSimulazione()
        e.configura(durata_ore=48)
        e.imposta_velocita(0)
        e.avvia()
        for _ in range(200):
            if e.stato == "finita":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(e.stato, "finita")
        self.assertEqual(e.sim.passo, 48)
        self.assertIn("passi_al_secondo", e.stato_esecuzione()["prestazioni"])

    async def test_la_pausa_ferma_l_avanzamento(self):
        e = EsecutoreSimulazione()
        e.configura(durata_ore=8760)
        e.imposta_velocita(0)
        e.avvia()
        await asyncio.sleep(0.1)
        e.pausa()
        fermo = e.sim.passo
        await asyncio.sleep(0.1)
        self.assertEqual(e.stato, "in_pausa")
        self.assertEqual(e.sim.passo, fermo)
        self.assertGreater(fermo, 0)
        self.assertEqual(e.passo_singolo(2), 2)
        e.avvia()
        with self.assertRaises(ValueError):
            e.passo_singolo(1)
        await e.chiudi()

    async def test_la_velocita_limita_l_avanzamento(self):
        e = EsecutoreSimulazione()
        e.configura(durata_ore=8760)
        e.imposta_velocita(3600 * 20)  # 20 passi orari al secondo
        e.avvia()
        await asyncio.sleep(0.5)
        await e.chiudi()
        self.assertTrue(3 <= e.sim.passo <= 16, e.sim.passo)

    def test_parametri_non_validi(self):
        e = EsecutoreSimulazione()
        for parametri in [{"tipo": "marte"}, {"passo_minuti": 7}, {"inizio": "ieri"}, {"tipo": "generata", "zone": 1},
                          {"tipo": "generata", "citta": 10 ** 6}]:
            with self.subTest(parametri=parametri), self.assertRaises(ValueError):
                e.configura(**parametri)
        with self.assertRaises(ValueError):
            e.imposta_velocita(-1)


class ApiEnergiaTest(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app_con_lifespan()))
        esecutore.configura(durata_ore=24 * 30)

    def test_pagina_e_topologia(self):
        pagina = self.client.get("/energia")
        self.assertEqual(pagina.status_code, 200)
        self.assertIn("Content-Security-Policy", pagina.headers)
        self.assertIn("/energia/stream", pagina.text)
        rete = self.client.get("/energia/rete").json()
        self.assertEqual(rete["scenario"]["nome"], "Italia")
        self.assertEqual(rete["versione_rete"], esecutore.versione_rete)
        stato = self.client.get("/energia/stato").json()
        self.assertEqual(len(stato["istantanea"]["centrali"]), len(rete["centrali"]))
        self.assertEqual(stato["esecuzione"]["versione_rete"], rete["versione_rete"])

    def test_passo_storico_ed_entita(self):
        r = self.client.post("/energia/controllo", json={"azione": "passo", "passi": 30})
        self.assertEqual(r.json()["passo"], 30)
        storico = self.client.get("/energia/storico", params={"scala": "passi", "massimo": 10}).json()
        self.assertEqual(storico["punti_totali"], 30)
        self.assertEqual(len(storico["serie"]), 10)
        self.assertIn("produzione", storico["serie"][0])
        entita = self.client.get("/energia/entita/centrale:larderello").json()
        self.assertEqual(entita["categoria"], "centrale")
        self.assertEqual(self.client.get("/energia/entita/nulla").status_code, 404)

    def test_stream_invia_lo_stato(self):
        r = self.client.get("/energia/stream", params={"limite": 2, "frequenza": 20})
        blocchi = [json.loads(b[6:]) for b in r.text.split("\n\n") if b.startswith("data: ")]
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(len(blocchi), 2)
        self.assertIn("istantanea", blocchi[0])

    def test_avvio_e_pausa(self):
        self.assertEqual(self.client.post("/energia/controllo", json={"azione": "avvia", "velocita": 0}).json()["stato"], "in_corso")
        self.assertEqual(self.client.post("/energia/controllo", json={"azione": "pausa"}).json()["stato"], "in_pausa")
        self.assertEqual(self.client.post("/energia/controllo", json={"azione": "velocita"}).status_code, 422)

    def test_scenario_generato_ed_eventi(self):
        r = self.client.post("/energia/scenario", json={"tipo": "generata", "zone": 6, "citta": 30, "centrali": 20, "durata_ore": 48})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/energia/rete").json()["scenario"]["nome"], "Rete generata")
        self.assertEqual(self.client.post("/energia/scenario", json={"passo_minuti": 7}).status_code, 422)
        self.assertEqual(self.client.post("/energia/evento", json={"tipo": "ondata_calore"}).status_code, 200)
        self.assertEqual(self.client.post("/energia/evento", json={"tipo": "guasto", "entita": "nulla"}).status_code, 422)

    def test_permessi_dei_ruoli(self):
        chiavi = {"API_KEY_TIROCINANTE": "t-chiave", "API_KEY_MEDICO_DI_GUARDIA": "m-chiave", "API_KEY_PRIMARIO": "p-chiave"}
        with patch.dict(os.environ, chiavi):
            self.assertEqual(self.client.get("/energia").status_code, 200)  # la pagina è pubblica
            self.assertEqual(self.client.get("/energia/stato").status_code, 401)
            self.assertEqual(self.client.get("/energia/stato", headers={"X-API-Key": "t-chiave"}).status_code, 200)
            self.assertEqual(self.client.post("/energia/controllo", json={"azione": "pausa"}, headers={"X-API-Key": "t-chiave"}).status_code, 403)
            self.assertEqual(self.client.post("/energia/controllo", json={"azione": "pausa"}, headers={"X-API-Key": "m-chiave"}).status_code, 200)
            self.assertEqual(self.client.post("/energia/evento", json={"tipo": "calma_vento"}, headers={"X-API-Key": "m-chiave"}).status_code, 403)
            self.assertEqual(self.client.post("/energia/evento", json={"tipo": "calma_vento"}, headers={"X-API-Key": "p-chiave"}).status_code, 200)


def app_con_lifespan():
    from app.api.main import app
    return app


if __name__ == "__main__":
    unittest.main()
