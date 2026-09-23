import os
import unittest
from unittest.mock import patch

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.main import app
from app.core.ruoli import (
    PERMESSI, ROTTE_PUBBLICHE, Ruolo, autenticazione_attiva, avvisi_configurazione, ruolo_della_chiave, ruolo_minimo,
)

T, M, P = "chiave-tirocinante", "chiave-medico", "chiave-primario"
TUTTE = {"API_KEY": "", "API_KEY_TIROCINANTE": T, "API_KEY_MEDICO_DI_GUARDIA": M, "API_KEY_PRIMARIO": P}


def intestazione(chiave):
    return {"X-API-Key": chiave}


def percorso_di_prova(template):
    percorso = template.replace("{device_id}", "x").replace("{agent_name}", "x").replace("{target}", "x").replace("{entita_id}", "x")
    # Lo stream della simulazione non finisce da solo: nei test si chiude dopo il primo messaggio.
    return percorso + "?limite=1" if percorso == "/energia/stream" else percorso


class MatriceDeiPermessiTest(unittest.TestCase):
    def rotte(self):
        return {
            (metodo, r.path)
            for r in app.routes if isinstance(r, APIRoute)
            for metodo in r.methods if metodo not in ("HEAD", "OPTIONS")
        }

    def test_ogni_endpoint_e_classificato(self):
        non_classificati = {rotta for rotta in self.rotte() if rotta not in PERMESSI and rotta not in ROTTE_PUBBLICHE}
        self.assertEqual(non_classificati, set(), "Endpoint senza ruolo minimo: verrebbero riservati al primario")

    def test_la_matrice_non_contiene_endpoint_inesistenti(self):
        self.assertEqual(set(PERMESSI) - self.rotte(), set())

    def test_i_tre_livelli_sono_tutti_usati_e_ordinati(self):
        self.assertEqual(set(PERMESSI.values()), {Ruolo.TIROCINANTE, Ruolo.MEDICO_DI_GUARDIA, Ruolo.PRIMARIO})
        self.assertLess(Ruolo.TIROCINANTE, Ruolo.MEDICO_DI_GUARDIA)
        self.assertLess(Ruolo.MEDICO_DI_GUARDIA, Ruolo.PRIMARIO)
        self.assertEqual(Ruolo.MEDICO_DI_GUARDIA.nome, "medico_di_guardia")

    def test_un_endpoint_non_classificato_richiede_il_primario(self):
        self.assertEqual(ruolo_minimo("POST", "/nuovo/endpoint"), Ruolo.PRIMARIO)

    def test_le_operazioni_distruttive_o_di_configurazione_sono_riservate_al_primario(self):
        for rotta in [("DELETE", "/system/reset"), ("POST", "/hitl/config"), ("POST", "/agents/create"),
                      ("DELETE", "/agents/{agent_name}"), ("POST", "/llm/invoke"), ("POST", "/events/seed-conflict")]:
            self.assertEqual(PERMESSI[rotta], Ruolo.PRIMARIO, rotta)

    def test_le_letture_sono_aperte_al_tirocinante(self):
        for (metodo, percorso), ruolo in PERMESSI.items():
            if metodo == "GET":
                self.assertEqual(ruolo, Ruolo.TIROCINANTE, percorso)


def blocca_i_gestori(test):
    """
    Sostituisce il gestore di ogni endpoint con uno stub innocuo, tranne
    `/graph/resume` (il cui controllo sull'OVERRIDE sta dentro il gestore; senza grafo compilato risponde 500 senza effetti).
    Così un 403 dimostra che il gestore NON è stato eseguito, e un'autorizzazione rotta non può cancellare dati veri.
    """
    async def stub(*args, **kwargs):
        return {"gestore": "eseguito"}

    for rotta in app.routes:
        if isinstance(rotta, APIRoute) and rotta.path != "/graph/resume":
            originale = rotta.dependant.call
            rotta.dependant.call = stub
            test.addCleanup(setattr, rotta.dependant, "call", originale)


def autorizzazione_superata(risposta):
    """Né 401 né 403. Con i gestori sostituiti, 200 è lo stub e 422 è la validazione del corpo che segue l'autorizzazione."""
    return risposta.status_code not in (401, 403)


class AutorizzazionePerRuoloTest(unittest.TestCase):
    """Gli endpoint negati non eseguono nulla; quelli ammessi superano l'autorizzazione e arrivano allo stub (o alla validazione del corpo)."""

    def setUp(self):
        self.enterContext(patch.dict(os.environ, TUTTE))
        self.enterContext(patch("app.api.main._graph", None))
        blocca_i_gestori(self)
        self.client = TestClient(app)

    def chiama(self, metodo, percorso, chiave, **kwargs):
        return self.client.request(metodo, percorso, headers=intestazione(chiave) if chiave else None, **kwargs)

    def test_un_ruolo_inferiore_al_minimo_riceve_403_su_ogni_endpoint(self):
        for (metodo, template), minimo in PERMESSI.items():
            inferiori = [(T, Ruolo.TIROCINANTE), (M, Ruolo.MEDICO_DI_GUARDIA)]
            for chiave, ruolo in inferiori:
                if ruolo >= minimo:
                    continue
                with self.subTest(rotta=f"{metodo} {template}", ruolo=ruolo.nome):
                    risposta = self.chiama(metodo, percorso_di_prova(template), chiave, json={})
                    self.assertEqual(risposta.status_code, 403)
                    self.assertIn(minimo.nome, risposta.json()["detail"])

    def test_un_ruolo_sufficiente_raggiunge_il_gestore_su_ogni_endpoint(self):
        chiavi = {Ruolo.TIROCINANTE: T, Ruolo.MEDICO_DI_GUARDIA: M, Ruolo.PRIMARIO: P}
        for (metodo, template), minimo in PERMESSI.items():
            if template == "/graph/resume":
                continue
            for ruolo, chiave in chiavi.items():
                if ruolo < minimo:
                    continue
                with self.subTest(rotta=f"{metodo} {template}", ruolo=ruolo.nome):
                    risposta = self.chiama(metodo, percorso_di_prova(template), chiave, json={})
                    self.assertTrue(autorizzazione_superata(risposta), risposta.text)

    def test_il_tirocinante_legge_ma_non_scrive(self):
        self.assertTrue(autorizzazione_superata(self.chiama("GET", "/hitl/config", T)))
        self.assertEqual(self.chiama("POST", "/tools", T, json={}).status_code, 403)
        self.assertEqual(self.chiama("POST", "/graph/run", T, json={}).status_code, 403)

    def test_il_medico_di_guardia_esegue_le_procedure_ordinarie_ma_non_la_configurazione(self):
        self.assertTrue(autorizzazione_superata(self.chiama("POST", "/tools", M, json={})))
        self.assertTrue(autorizzazione_superata(self.chiama("POST", "/graph/run", M, json={})))
        self.assertEqual(self.chiama("POST", "/hitl/config", M, json={}).status_code, 403)
        self.assertEqual(self.chiama("DELETE", "/system/reset", M).status_code, 403)
        self.assertEqual(self.chiama("POST", "/agents/create", M, json={}).status_code, 403)

    def test_il_primario_supera_l_autorizzazione_di_ogni_livello(self):
        for metodo, percorso in [("GET", "/hitl/config"), ("POST", "/tools"), ("POST", "/hitl/config"), ("DELETE", "/system/reset")]:
            self.assertTrue(autorizzazione_superata(self.chiama(metodo, percorso, P, json={})), f"{metodo} {percorso}")

    def test_l_override_richiede_il_primario_anche_se_la_ripresa_e_del_medico(self):
        # senza grafo compilato l'endpoint risponde 500 dopo aver superato l'autorizzazione
        self.assertEqual(self.chiama("POST", "/graph/resume", M, json={"decision": "APPROVA"}).status_code, 500)
        self.assertEqual(self.chiama("POST", "/graph/resume", M, json={"decision": "RETRY"}).status_code, 500)
        override_medico = self.chiama("POST", "/graph/resume", M, json={"decision": "override", "reasoning": "x"})
        self.assertEqual(override_medico.status_code, 403)
        self.assertIn("primario", override_medico.json()["detail"])
        self.assertEqual(self.chiama("POST", "/graph/resume", P, json={"decision": "OVERRIDE"}).status_code, 500)
        self.assertEqual(self.chiama("POST", "/graph/resume", T, json={"decision": "APPROVA"}).status_code, 403)

    def test_senza_chiave_o_con_chiave_errata_la_risposta_e_401(self):
        self.assertEqual(self.chiama("GET", "/hitl/config", None).status_code, 401)
        self.assertEqual(self.chiama("GET", "/hitl/config", "chiave-sbagliata").status_code, 401)

    def test_la_root_resta_pubblica(self):
        self.assertTrue(autorizzazione_superata(self.chiama("GET", "/", None)))

    def test_la_documentazione_resta_raggiungibile_e_dichiara_l_header(self):
        self.assertEqual(self.client.get("/docs").status_code, 200)
        self.assertIn("APIKeyHeader", self.client.get("/openapi.json").json()["components"]["securitySchemes"])


class CompatibilitaEConfigurazioneTest(unittest.TestCase):
    def ambiente(self, **variabili):
        base = {"API_KEY": "", "API_KEY_TIROCINANTE": "", "API_KEY_MEDICO_DI_GUARDIA": "", "API_KEY_PRIMARIO": ""}
        return patch.dict(os.environ, {**base, **variabili})

    def test_la_chiave_unica_storica_vale_come_primario(self):
        with self.ambiente(API_KEY="ciao"), patch("app.api.main._graph", None):
            blocca_i_gestori(self)
            self.assertEqual(ruolo_della_chiave("ciao"), Ruolo.PRIMARIO)
            client = TestClient(app)
            self.assertTrue(autorizzazione_superata(client.get("/hitl/config", headers=intestazione("ciao"))))
            self.assertTrue(autorizzazione_superata(client.delete("/system/reset", headers=intestazione("ciao"))))

    def test_senza_nessuna_chiave_l_api_e_aperta_e_vale_come_primario(self):
        with self.ambiente(), patch("app.api.main._graph", None):
            blocca_i_gestori(self)
            self.assertFalse(autenticazione_attiva())
            self.assertTrue(autorizzazione_superata(TestClient(app).get("/hitl/config")))
            self.assertIn("aperti", avvisi_configurazione()[0])

    def test_un_ruolo_senza_chiave_non_puo_accedere(self):
        with self.ambiente(API_KEY_TIROCINANTE=T):
            blocca_i_gestori(self)
            self.assertIsNone(ruolo_della_chiave(M))
            self.assertEqual(TestClient(app).get("/hitl/config", headers=intestazione(M)).status_code, 401)
            avvisi = " ".join(avvisi_configurazione())
            self.assertIn("medico_di_guardia", avvisi)
            self.assertIn("primario", avvisi)

    def test_la_stessa_chiave_su_piu_ruoli_vale_il_piu_alto_con_avviso(self):
        with self.ambiente(API_KEY_TIROCINANTE="stessa", API_KEY_PRIMARIO="stessa", API_KEY_MEDICO_DI_GUARDIA="m"):
            self.assertEqual(ruolo_della_chiave("stessa"), Ruolo.PRIMARIO)
            self.assertTrue(any("più ruoli" in a for a in avvisi_configurazione()))

    def test_configurazione_completa_senza_avvisi(self):
        with self.ambiente(**{k: v for k, v in TUTTE.items()}):
            self.assertEqual(avvisi_configurazione(), [])
            self.assertEqual(ruolo_della_chiave(T), Ruolo.TIROCINANTE)
            self.assertEqual(ruolo_della_chiave(M), Ruolo.MEDICO_DI_GUARDIA)
            self.assertIsNone(ruolo_della_chiave(""))
            self.assertIsNone(ruolo_della_chiave(None))


if __name__ == "__main__":
    unittest.main()
