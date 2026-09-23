"""
Agenti del dominio energia e sistema nervoso: ciclo completo del grafo con modello finto (nessun LLM reale),
arco riflesso, approvazione dell'operatore, stimoli, modalità automatica con tetto ai cicli, errori del modello, API.
"""

import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agents.base_agent import BaseAgent
from app.core.errori_llm import ErroreLLM
from app.db.database import Database
from app.simulazione.esecutore import esecutore
from app.simulazione.sistema_nervoso import SistemaNervoso

RISPOSTE_CRISI = {
    "componente_carichi": "DECISIONE: ATTIVA\nMOTIVAZIONE: carico non servito in Sicilia.",
    "componente_accumuli": "**DECISIONE:** ATTIVA\nMOTIVAZIONE: margine basso.",
    "organo_rete_elettrica": "DECISIONE: ATTIVA\nMOTIVAZIONE: serve import.",
}


def modello_finto(risposte: dict[str, str], chiamate: list[str], brain: str = "DECISIONE: APPROVA\nMOTIVAZIONE: ok"):
    async def ask_brain(self, sistema, utente, **kw):
        chiamate.append(self.name)
        if self.name == "Brain":
            return brain
        return risposte.get(self.name, "DECISIONE: NESSUNA\nMOTIVAZIONE: niente da fare.")
    return ask_brain


def prepara_crisi_sicilia():
    """Ondata di calore e Sicilia isolata: compare carico non servito."""
    esecutore.configura(inizio="2026-07-10T00:00", durata_ore=240)
    sim = esecutore.simulatore()
    sim.avanza(40)
    sim.inietta_evento("ondata_calore", durata_ore=96)
    for entita in ("linea:CALA-SICI", "centrale:priolo_gargallo", "centrale:flotta_gas_sicilia"):
        sim.inietta_evento("guasto", entita, 96)
    sn = SistemaNervoso()
    while not sn.stimolo(sim) and not sim.finita:
        sim.avanza(1)
    return sim, sn


async def attendi(sn: SistemaNervoso) -> None:
    for _ in range(400):
        if sn.stato != "in_esecuzione":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("il ciclo del grafo non termina")


class SistemaNervosoTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Database().init_db()

    async def test_crisi_elettrica_sale_al_brain_che_chiede_all_operatore(self):
        sim, sn = prepara_crisi_sicilia()
        self.assertIn("carico non servito", sn.stimolo(sim))
        chiamate: list[str] = []
        with patch.object(BaseAgent, "ask_brain", modello_finto(RISPOSTE_CRISI, chiamate)):
            sn.avvia_ciclo(sn.stimolo(sim))
            await attendi(sn)
            self.assertEqual(sn.stato, "in_attesa_operatore")
            self.assertIn("rete_el_interrompibili", sn.richiesta["prompt"])
            self.assertEqual(sim.leve["rete_el_interrompibili"], "OFF")  # il componente non la aziona da solo
            self.assertEqual(sim.leve["rete_el_accumuli_riserva"], "ON")
            self.assertEqual(sim.leve["rete_el_import_emergenza"], "ON")

            sn.decidi("APPROVA", "prova")
            await attendi(sn)
        self.assertEqual(sn.stato, "pronto")
        self.assertEqual(sim.leve["rete_el_interrompibili"], "ON")
        # Rete gas visitata dopo l'approvazione, senza chiamate al modello (arco riflesso); l'operatore ha deciso per il Brain.
        self.assertEqual(chiamate, ["componente_accumuli", "componente_carichi", "organo_rete_elettrica"])
        self.assertEqual(
            sn.nodi_visitati,
            ["brain", "organo_rete_elettrica", "componente_accumuli", "componente_carichi", "organo_rete_gas",
             "componente_stoccaggi", "componente_consumi_gas"],
        )
        self.assertEqual([p["tipo"] for p in sn.passi].count("pausa"), 1)  # l'operatore è interpellato una volta sola
        self.assertEqual(sn.passi[-1]["tipo"], "fine")
        await sn.chiudi()

    async def test_il_rifiuto_dell_operatore_lascia_la_leva_spenta(self):
        sim, sn = prepara_crisi_sicilia()
        with patch.object(BaseAgent, "ask_brain", modello_finto(RISPOSTE_CRISI, [])):
            sn.avvia_ciclo("prova")
            await attendi(sn)
            sn.decidi("RESPINGI", "non ora")
            await attendi(sn)
        self.assertEqual(sn.stato, "pronto")
        self.assertEqual(sim.leve["rete_el_interrompibili"], "OFF")
        await sn.chiudi()

    async def test_arco_riflesso_senza_anomalie_nessuna_chiamata(self):
        esecutore.configura(inizio="2026-05-10T00:00", durata_ore=48)
        esecutore.simulatore().avanza(3)
        sn, chiamate = SistemaNervoso(), []
        with patch.object(BaseAgent, "ask_brain", modello_finto({}, chiamate)):
            sn.avvia_ciclo("prova")
            await attendi(sn)
        self.assertEqual(sn.stato, "pronto")
        self.assertEqual(chiamate, [])
        self.assertEqual(len(sn.nodi_visitati), 7)
        self.assertTrue(all("arco riflesso" in (p["testo"] or "") for p in sn.passi if p["tipo"] == "nodo" and p["nodo"].startswith("componente")))
        await sn.chiudi()

    async def test_errore_del_modello_ferma_il_grafo_e_l_operatore_rinuncia(self):
        sim, sn = prepara_crisi_sicilia()

        async def guasto(self, *a, **kw):
            raise ErroreLLM("CREDITI_ESAURITI", "crediti finiti")
        with patch.object(BaseAgent, "ask_brain", guasto):
            sn.avvia_ciclo("prova")
            await attendi(sn)
            self.assertEqual(sn.stato, "in_attesa_operatore")
            self.assertEqual(sn.richiesta["type"], "llm_failure_human_intervention")
            sn.decidi("RESPINGI")
            await attendi(sn)
        self.assertEqual(sn.stato, "pronto")
        await sn.chiudi()

    async def test_escalate_con_leva_accesa_chiede_la_misura_successiva(self):
        esecutore.configura(inizio="2026-01-15T00:00", durata_ore=48)
        sim = esecutore.simulatore()
        sim.avanza(2)
        sim.imposta_leva("rete_gas_gnl_spot", "ON")
        sn, chiamate = SistemaNervoso(), []
        with patch.object(BaseAgent, "ask_brain", modello_finto(
                {"organo_rete_gas": "DECISIONE: ESCALATE\nMOTIVAZIONE: gas non servito nonostante il GNL spot."}, chiamate)):
            sn.avvia_ciclo("prova")
            await attendi(sn)
        self.assertEqual(sn.stato, "pronto")
        self.assertEqual(sim.leve["rete_gas_gnl_spot"], "ON")  # la propria leva non viene mai proposta spenta
        self.assertEqual(sim.leve["rete_gas_stoccaggio_strategico"], "ON")  # misura successiva, approvata dal Brain
        self.assertIn("Chiede al padre rete_gas_stoccaggio_strategico = ON", " ".join(p["testo"] or "" for p in sn.passi))
        self.assertEqual(chiamate.count("Brain"), 1)
        await sn.chiudi()

    async def test_escalate_con_tutte_le_misure_accese_e_una_segnalazione(self):
        esecutore.configura(inizio="2026-01-15T00:00", durata_ore=48)
        sim = esecutore.simulatore()
        sim.avanza(2)
        for leva in ("rete_gas_gnl_spot", "rete_gas_stoccaggio_strategico", "rete_gas_interrompibili"):
            sim.imposta_leva(leva, "ON")
        sn, chiamate = SistemaNervoso(), []
        with patch.object(BaseAgent, "ask_brain", modello_finto(
                {"organo_rete_gas": "DECISIONE: ESCALATE\nMOTIVAZIONE: situazione grave."}, chiamate)):
            sn.avvia_ciclo("prova")
            await attendi(sn)
        self.assertEqual(sn.stato, "pronto")
        self.assertNotIn("Brain", chiamate)  # nessuna richiesta da valutare
        self.assertIn("Segnalazione al padre", " ".join(p["testo"] or "" for p in sn.passi))
        await sn.chiudi()

    async def test_vecchi_eventi_off_o_rejected_non_bloccano_le_leve(self):
        from app.tools.event_log import EventLog
        esecutore.configura(inizio="2026-01-15T00:00", durata_ore=48)
        sim = esecutore.simulatore()
        sim.avanza(2)
        sim.inietta_evento("crisi_gas", "ingresso:tarvisio", 48)  # l'organo gas deve ragionare (niente arco riflesso)
        sim.avanza(1)  # le anomalie si calcolano a ogni passo
        log = EventLog()
        await log.log_event("Brain", "REJECTED_OFF", "rete_gas_gnl_spot", "ON", "REJECTED", "rifiuto di un ciclo vecchio")
        await log.log_event("operatore", "LEVA_OFF", "rete_gas_gnl_spot", "ON", "OFF", "comando vecchio")
        sn, chiamate = SistemaNervoso(), []
        with patch.object(BaseAgent, "ask_brain", modello_finto({"organo_rete_gas": "DECISIONE: ATTIVA\nMOTIVAZIONE: ingresso in crisi."}, chiamate)):
            sn.avvia_ciclo("prova")
            await attendi(sn)
        self.assertEqual(sim.leve["rete_gas_gnl_spot"], "ON")
        # Azionata direttamente dall'organo: nessun blocco, nessun passaggio dal Brain.
        self.assertIn("Leva rete_gas_gnl_spot: OFF -> ON", " ".join(p["testo"] or "" for p in sn.passi))
        self.assertNotIn("Brain", chiamate)
        await sn.chiudi()

    async def test_il_rifiuto_del_brain_e_un_veto_e_si_puo_richiedere(self):
        esecutore.configura(inizio="2026-01-15T00:00", durata_ore=48)
        sim = esecutore.simulatore()
        sim.avanza(2)
        sim.inietta_evento("crisi_gas", "ingresso:tarvisio", 48)  # l'organo gas deve ragionare (niente arco riflesso)
        sim.avanza(1)  # le anomalie si calcolano a ogni passo
        risposte = {"organo_rete_gas": "DECISIONE: ESCALATE\nMOTIVAZIONE: serve il GNL spot."}
        sn, chiamate = SistemaNervoso(), []
        with patch.object(BaseAgent, "ask_brain", modello_finto(risposte, chiamate, brain="DECISIONE: RESPINGI\nMOTIVAZIONE: non serve.")):
            sn.avvia_ciclo("primo")
            await attendi(sn)
        self.assertEqual(sim.leve["rete_gas_gnl_spot"], "OFF")
        self.assertEqual(sim.veto("rete_gas_gnl_spot", "ON")["autore"], "Brain")

        # Il ciclo dopo l'organo non è bloccato: torna a chiedere, e il Brain sa che è una richiesta ripetuta.
        risposte["organo_rete_gas"] = "DECISIONE: ATTIVA\nMOTIVAZIONE: la pressione scende."
        with patch.object(BaseAgent, "ask_brain", modello_finto(risposte, chiamate)):
            sn.avvia_ciclo("secondo")
            await attendi(sn)
        testi = " ".join(p["testo"] or "" for p in sn.passi if p["ciclo"] == 2)
        self.assertIn("Chiede al padre rete_gas_gnl_spot = ON", testi)
        self.assertNotIn("bloccato", testi)
        self.assertEqual(sim.leve["rete_gas_gnl_spot"], "ON")
        self.assertIsNone(sim.veto("rete_gas_gnl_spot"))  # l'approvazione cancella il veto
        await sn.chiudi()

    async def test_il_comando_dell_operatore_non_viene_ribaltato(self):
        esecutore.configura(inizio="2026-01-15T00:00", durata_ore=48)
        sim = esecutore.simulatore()
        sim.avanza(2)
        sim.inietta_evento("crisi_gas", "ingresso:tarvisio", 48)  # l'organo gas deve ragionare (niente arco riflesso)
        sim.avanza(1)  # le anomalie si calcolano a ogni passo
        sim.imposta_veto("rete_gas_gnl_spot", "ON", autore="operatore")
        sim.imposta_veto("rete_gas_stoccaggio_strategico", "ON", autore="operatore")
        risposte = {"organo_rete_gas": "DECISIONE: ATTIVA\nMOTIVAZIONE: vorrei il GNL.",
                    "componente_stoccaggi": "DECISIONE: ESCALATE\nMOTIVAZIONE: serve aiuto."}
        sn = SistemaNervoso()
        with patch.object(BaseAgent, "ask_brain", modello_finto(risposte, [])):
            sn.avvia_ciclo("prova")
            await attendi(sn)
            if sn.stato == "in_attesa_operatore":
                sn.decidi("RESPINGI")
                await attendi(sn)
        testi = " ".join(p["testo"] or "" for p in sn.passi)
        self.assertEqual(sim.leve["rete_gas_gnl_spot"], "OFF")
        self.assertEqual(sim.leve["rete_gas_stoccaggio_strategico"], "OFF")
        self.assertIn("l'operatore l'ha fissata", testi)
        # Lo stoccaggio salta le leve fissate dall'operatore e chiede la misura dopo (gli interrompibili del gas).
        self.assertIn("Chiede al padre rete_gas_interrompibili = ON", testi)
        await sn.chiudi()

    async def test_stimoli_nuove_anomalie_e_rientro(self):
        sim, sn = prepara_crisi_sicilia()
        self.assertTrue(sn.stimolo(sim).startswith("Nuove anomalie"))
        sn._firma_gestita = frozenset(a["id"] for a in sn._anomalie_rilevanti(sim))
        self.assertIsNone(sn.stimolo(sim))
        # Tutto rientrato ma una leva è rimasta accesa: serve un ciclo per spegnerla.
        sim.anomalie = []
        sim.leve["rete_gas_gnl_spot"] = "ON"
        self.assertIn("rete_gas_gnl_spot", sn.stimolo(sim))

    async def test_modalita_automatica_rispetta_il_tetto_dei_cicli(self):
        sim, sn = prepara_crisi_sicilia()
        chiamate: list[str] = []
        with patch.object(BaseAgent, "ask_brain", modello_finto({}, chiamate)):
            sn.configura(automatico=True, intervallo_minimo_s=1, cicli_automatici_massimi=1)
            sn.avvia_sorveglianza()
            for _ in range(100):
                if sn.cicli:
                    break
                await asyncio.sleep(0.05)
            await attendi(sn)
            # Una nuova anomalia non basta a superare il tetto
            sn._firma_gestita = frozenset()
            await asyncio.sleep(1.5)
        self.assertEqual(sn.cicli_automatici, 1)
        self.assertEqual(sn.cicli, 1)
        self.assertTrue(sn.ultimo_stimolo["automatico"])
        await sn.chiudi()

    def test_parametri_non_validi(self):
        sn = SistemaNervoso()
        with self.assertRaises(ValueError):
            sn.configura(intervallo_minimo_s=0)
        with self.assertRaises(ValueError):
            sn.configura(cicli_automatici_massimi=-1)
        with self.assertRaises(ValueError):
            sn.decidi("APPROVA")


class AutomatismiTest(unittest.TestCase):
    """Gli apparati della rete: verifiche e indicazioni deterministiche."""

    FATTI_GAS_TRANQUILLI = {
        "gas_non_servito": False, "pressione_minima_bar": 64.5, "inverno": True, "ingressi_in_crisi": 2,
        "stoccaggi_pct": 68.6, "obiettivo_stoccaggi_pct": 67.9, "stoccaggi_scarto_dall_obiettivo_punti": 0.7,
        "stoccaggi_sotto_obiettivo": False, "pressione_sotto_46": False, "pressione_sotto_50": False,
        "pressione_sotto_52": False, "pressione_sopra_55": True, "pressione_sopra_58": True, "pressione_sopra_60": True,
    }

    def test_ingressi_in_crisi_con_stoccaggi_sopra_l_obiettivo_non_accendono_il_gnl(self):
        from app.tools.strumenti_energia import indicazione
        # Il caso del 2 gennaio: due ingressi in crisi, stoccaggi al 68,6% contro 67,9%, pressione 64,5 bar.
        self.assertEqual(indicazione("rete_gas_gnl_spot", self.FATTI_GAS_TRANQUILLI, accesa=False)[0], "NESSUNA")
        sotto = {**self.FATTI_GAS_TRANQUILLI, "stoccaggi_sotto_obiettivo": True}
        self.assertEqual(indicazione("rete_gas_gnl_spot", sotto, accesa=False)[0], "ATTIVA")
        # Già acceso con crisi in corso: si tiene; senza crisi e con pressione alta: si spegne.
        self.assertEqual(indicazione("rete_gas_gnl_spot", self.FATTI_GAS_TRANQUILLI, accesa=True)[0], "NESSUNA")
        rientrato = {**self.FATTI_GAS_TRANQUILLI, "ingressi_in_crisi": 0}
        self.assertEqual(indicazione("rete_gas_gnl_spot", rientrato, accesa=True)[0], "DISATTIVA")
        critico = {**self.FATTI_GAS_TRANQUILLI, "gas_non_servito": True}
        self.assertEqual(indicazione("rete_gas_gnl_spot", critico, accesa=True)[0], "ESCALATE")

    def test_margine_e_soglie_elettriche(self):
        from app.tools.strumenti_energia import indicazione
        fatti = {"carico_non_servito": False, "margine_riserva_pct": 8.0, "ondata_in_corso": False,
                 "anomalie_elettriche": True, "margine_sotto_3": False, "margine_sotto_10": True, "margine_sotto_15": True,
                 "margine_sotto_25": True, "margine_sopra_3": True, "margine_sopra_10": False, "margine_sopra_15": False,
                 "margine_sopra_25": False}
        self.assertEqual(indicazione("rete_el_import_emergenza", fatti, accesa=False)[0], "ATTIVA")
        self.assertEqual(indicazione("rete_el_accumuli_riserva", fatti, accesa=False)[0], "ATTIVA")
        self.assertEqual(indicazione("rete_el_interrompibili", fatti, accesa=False)[0], "NESSUNA")  # serve sotto il 3%
        self.assertEqual(indicazione("rete_el_interrompibili", {**fatti, "carico_non_servito": True}, accesa=False)[0], "ATTIVA")

    def test_i_sensori_riportano_le_verifiche(self):
        import json as _json
        from app.tools.strumenti_energia import SensoreRete
        esecutore.configura(inizio="2026-01-02T00:00", durata_ore=48)
        esecutore.simulatore().avanza(3)
        gas = _json.loads(asyncio.run(SensoreRete("sensore_rete_gas").get_tool_value()))
        verifiche = gas["verifiche"]
        self.assertTrue(verifiche["inverno"])
        self.assertEqual(verifiche["stoccaggi_sotto_obiettivo"], verifiche["stoccaggi_pct"] < verifiche["obiettivo_stoccaggi_pct"])
        self.assertAlmostEqual(verifiche["stoccaggi_scarto_dall_obiettivo_punti"],
                               verifiche["stoccaggi_pct"] - verifiche["obiettivo_stoccaggi_pct"], places=1)
        elettrica = _json.loads(asyncio.run(SensoreRete("sensore_rete_elettrica").get_tool_value()))
        self.assertIn("margine_sotto_10", elettrica["verifiche"])

    def test_i_prompt_riportano_le_regole_della_tabella(self):
        from app.agents.agenti_energia import GERARCHIA_ENERGIA, AgenteEnergia
        from app.tools.strumenti_energia import strumenti_energia, testo_regole
        strumenti = strumenti_energia()
        for ruolo in GERARCHIA_ENERGIA:
            with self.subTest(agente=ruolo.nome):
                self.assertIn(testo_regole(ruolo.leva), AgenteEnergia(ruolo, strumenti).system_prompt)


class ScostamentoTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Database().init_db()

    async def test_lo_scostamento_dall_automatismo_viene_registrato(self):
        from app.tools.event_log import EventLog
        esecutore.configura(inizio="2026-01-15T00:00", durata_ore=48)
        sim = esecutore.simulatore()
        sim.avanza(2)
        sim.inietta_evento("crisi_gas", "ingresso:tarvisio", 48)
        sim.avanza(1)
        sn = SistemaNervoso()
        # Stoccaggi sopra l'obiettivo a metà gennaio: l'automatismo indica NESSUNA, il modello finto accende lo stesso.
        with patch.object(BaseAgent, "ask_brain", modello_finto({"organo_rete_gas": "DECISIONE: ATTIVA\nMOTIVAZIONE: crisi invernale."}, [])):
            sn.avvia_ciclo("prova")
            await attendi(sn)
        testi = " ".join(p["testo"] or "" for p in sn.passi)
        self.assertIn("Si discosta dall'automatismo, che indicava NESSUNA", testi)
        eventi = await EventLog(target=["rete_gas_gnl_spot"]).get_recent_events()
        self.assertTrue(any(e["action"] == "SCOSTAMENTO_AUTOMATISMO" and e["new_value"] == "ATTIVA" for e in eventi))
        await sn.chiudi()


class ApiSistemaNervosoTest(unittest.TestCase):
    def setUp(self):
        from app.api.main import app
        self.client = self.enterContext(TestClient(app))
        esecutore.configura(durata_ore=48)

    def test_stato_e_albero(self):
        rete = self.client.get("/energia/rete").json()
        self.assertEqual(rete["albero_agenti"][0]["nome"], "brain")
        self.assertEqual(len(rete["albero_agenti"]), 7)
        self.assertIn("rete_el_interrompibili", rete["leve"])
        stato = self.client.get("/energia/stato").json()
        self.assertEqual(stato["sistema_nervoso"]["stato"], "pronto")
        self.assertEqual(stato["istantanea"]["leve"]["rete_gas_gnl_spot"], "OFF")

    def test_leva_manuale(self):
        r = self.client.post("/energia/leva", json={"leva": "rete_gas_gnl_spot", "valore": "ON"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(esecutore.simulatore().leve["rete_gas_gnl_spot"], "ON")
        self.assertEqual(r.json()["veto"]["valore_vietato"], "OFF")  # gli agenti non lo spengono per qualche ora simulata
        self.assertEqual(self.client.post("/energia/leva", json={"leva": "inesistente", "valore": "ON"}).status_code, 422)
        self.assertEqual(self.client.post("/energia/leva", json={"leva": "rete_gas_gnl_spot", "valore": "FORSE"}).status_code, 422)
        eventi = self.client.get("/events").json()
        self.assertTrue(any(e["target"] == "rete_gas_gnl_spot" and e["actor"] == "operatore" for e in eventi.get("events", eventi)))

    def test_decisione_senza_richiesta_e_configurazione_non_valida(self):
        self.assertEqual(self.client.post("/energia/sistema-nervoso/decisione", json={"decisione": "APPROVA"}).status_code, 409)
        self.assertEqual(self.client.post("/energia/sistema-nervoso/configura", json={"intervallo_minimo_s": 0}).status_code, 422)
        r = self.client.post("/energia/sistema-nervoso/configura", json={"automatico": False, "cicli_automatici_massimi": 3})
        self.assertEqual(r.json()["cicli_automatici_massimi"], 3)


if __name__ == "__main__":
    unittest.main()
