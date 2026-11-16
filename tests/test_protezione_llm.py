"""Interruttore di circuito, budget e timeout per modello (`app/core/circuito.py`, `app/core/protezione_llm.py`, MAO)."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.core import metriche
from app.core.circuito import APERTO, CHIUSO, IN_PROVA, Interruttore
from app.core.configurazione import ErroreConfigurazione, costruisci_configurazione, imposta_configurazione
from app.core.errori_llm import ErroreLLM
from app.core.protezione_llm import ProtezioneLlm, protezione
from app.MAO.model_access_object import Mao


class Orologio:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class InterruttoreTest(unittest.TestCase):
    def setUp(self):
        self.o = Orologio()
        self.i = Interruttore(soglia=3, pausa=60, orologio=self.o)

    def test_si_apre_dopo_la_soglia_di_errori_consecutivi(self):
        for _ in range(2):
            self.i.fallimento()
        self.assertEqual(self.i.stato, CHIUSO)
        self.assertTrue(self.i.consenti())
        self.i.fallimento()
        self.assertEqual(self.i.stato, APERTO)
        self.assertFalse(self.i.consenti())

    def test_un_successo_azzera_il_conteggio(self):
        self.i.fallimento(); self.i.fallimento(); self.i.successo(); self.i.fallimento(); self.i.fallimento()
        self.assertEqual(self.i.stato, CHIUSO)

    def test_dopo_la_pausa_passa_una_sola_chiamata_di_prova(self):
        for _ in range(3):
            self.i.fallimento()
        self.o.t += 61
        self.assertEqual(self.i.stato, IN_PROVA)
        self.assertTrue(self.i.consenti())
        self.assertFalse(self.i.consenti())   # la prova è in corso: le altre aspettano

    def test_la_prova_riuscita_chiude_quella_fallita_riapre(self):
        for _ in range(3):
            self.i.fallimento()
        self.o.t += 61
        self.i.consenti(); self.i.fallimento()
        self.assertEqual(self.i.stato, APERTO)
        self.assertAlmostEqual(self.i.attesa_residua, 60, delta=0.01)
        self.o.t += 61
        self.i.consenti(); self.i.successo()
        self.assertEqual(self.i.stato, CHIUSO)

    def test_una_prova_annullata_si_puo_ripetere(self):
        for _ in range(3):
            self.i.fallimento()
        self.o.t += 61
        self.i.consenti(); self.i.annulla_prova()
        self.assertTrue(self.i.consenti())


class ProtezioneTest(unittest.TestCase):
    def setUp(self):
        self.o = Orologio()
        self.ora = [1_700_000_000.0]
        self.p = ProtezioneLlm(orologio=self.o, ora_reale=lambda: self.ora[0])

    def configura(self, **llm):
        imposta_configurazione(costruisci_configurazione({"llm": llm}))
        self.addCleanup(imposta_configurazione, None)

    def errore(self, codice):
        return ErroreLLM(codice, "x", provider="p", modello="m")

    def test_errori_del_provider_aprono_errori_della_richiesta_no(self):
        self.configura(protezione={"soglia_errori": 2, "pausa_secondi": 30})
        for _ in range(5):
            self.p.controlla("p", "m"); self.p.errore("p", "m", self.errore("RISPOSTA_TRONCATA"))
        self.p.controlla("p", "m")   # nessuna eccezione: il provider rispondeva
        self.p.errore("p", "m", self.errore("ERRORE_PROVIDER"))
        self.p.controlla("p", "m"); self.p.errore("p", "m", self.errore("PROVIDER_NON_RAGGIUNGIBILE"))
        with self.assertRaises(ErroreLLM) as ctx:
            self.p.controlla("p", "m")
        self.assertEqual(ctx.exception.codice, "CIRCUITO_APERTO")
        self.assertEqual(ctx.exception.dettagli["riprova_tra_secondi"], 30)
        self.assertTrue(ctx.exception.suggerimento)

    def test_l_interruttore_e_per_modello(self):
        self.configura(protezione={"soglia_errori": 1})
        self.p.controlla("p", "a"); self.p.errore("p", "a", self.errore("ERRORE_PROVIDER"))
        with self.assertRaises(ErroreLLM):
            self.p.controlla("p", "a")
        self.p.controlla("p", "b")   # un altro modello dello stesso provider non è toccato

    def test_budget_di_richieste_orario_e_si_libera_con_il_tempo(self):
        self.configura(budget={"richieste_ora": 2})
        self.p.controlla("p", "m"); self.p.controlla("p", "m")
        with self.assertRaises(ErroreLLM) as ctx:
            self.p.controlla("p", "m")
        self.assertEqual(ctx.exception.codice, "BUDGET_ESAURITO")
        self.ora[0] += 3601
        self.p.controlla("p", "m")

    def test_budget_di_token_giornaliero_e_per_provider(self):
        self.configura(budget={"token_giorno": 100, "altro": {"token_giorno": 0}})
        self.p.controlla("p", "m"); self.p.successo("p", "m", 60, 50)
        with self.assertRaises(ErroreLLM) as ctx:
            self.p.controlla("p", "m")
        self.assertEqual(ctx.exception.codice, "BUDGET_ESAURITO")
        self.p.controlla("altro", "m")   # 0 = nessun limite per quel provider
        self.ora[0] += 86400
        self.p.controlla("p", "m")

    def test_budget_diviso_tra_i_processi_con_stato_condiviso(self):
        self.configura(budget={"richieste_ora": 10})
        with patch.dict(os.environ, {"LANGBRAIN_WORKERS": "5"}):
            self.assertEqual(self.p.limiti("p"), {"richieste_ora": 2})

    def test_la_richiesta_respinta_non_conta_come_chiamata(self):
        self.configura(budget={"richieste_ora": 1}, protezione={"soglia_errori": 1})
        self.p.controlla("p", "m"); self.p.errore("p", "m", self.errore("ERRORE_PROVIDER"))
        for _ in range(3):
            with self.assertRaises(ErroreLLM):
                self.p.controlla("p", "m")
        self.assertEqual(self.p._richieste_ultima_ora("p"), 1)

    def test_configurazione_non_valida(self):
        for llm in ({"protezione": {"soglia_errori": 0}}, {"budget": {"richieste_ora": -1}}, {"budget": {"minuti": 3}},
                    {"timeout_secondi": {"mistral": 0}}):
            with self.subTest(llm=llm), self.assertRaises(ErroreConfigurazione):
                costruisci_configurazione({"llm": llm})

    def test_nomi_inglesi_accettati(self):
        c = costruisci_configurazione({"llm": {"protection": {"error_threshold": 7, "pause_seconds": 9},
                                                "timeout_seconds": {"mistral/ministral-8b-latest": 3},
                                                "budget": {"requests_per_hour": 5, "mistral": {"tokens_per_day": 9}}}})
        self.assertEqual((c.llm_soglia_errori, c.llm_pausa_secondi), (7, 9.0))
        self.assertEqual(c.llm_timeout, {"mistral/ministral-8b-latest": 3.0})
        self.assertEqual(c.llm_budget, {"*": {"richieste_ora": 5}, "mistral": {"token_giorno": 9}})


class MaoProtettoTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.dict(os.environ, {"MISTRAL_API_KEY": "chiave-mistral", "DEFAULT_PROVIDER": "mistral", "MAO_FALLBACK": "0",
                                                     "MISTRAL_MODEL": "ministral-8b-latest"}))
        self.mao = Mao()
        self.addAsyncCleanup(self.mao.aclose)
        imposta_configurazione(costruisci_configurazione({"llm": {"protezione": {"soglia_errori": 2, "pausa_secondi": 60}}}))
        self.addCleanup(imposta_configurazione, None)
        protezione.azzera()
        metriche.azzera()

    async def chiama(self):
        return await self.mao.call_model("sistema", "utente", provider="mistral")

    async def test_dopo_la_soglia_il_provider_non_viene_piu_chiamato(self):
        guasto = AsyncMock(side_effect=ErroreLLM("PROVIDER_NON_RAGGIUNGIBILE", "giu"))
        with patch.object(Mao, "_chiedi_modello", guasto):
            for _ in range(2):
                with self.assertRaises(ErroreLLM) as ctx:
                    await self.chiama()
                self.assertEqual(ctx.exception.codice, "PROVIDER_NON_RAGGIUNGIBILE")
            with self.assertRaises(ErroreLLM) as ctx:
                await self.chiama()
        self.assertEqual(ctx.exception.codice, "CIRCUITO_APERTO")
        self.assertEqual(guasto.await_count, 2)   # la terza non ha raggiunto il provider
        testo = await metriche.esporta()
        self.assertIn('langbrain_llm_calls_total{model="ministral-8b-latest",outcome="CIRCUITO_APERTO",provider="mistral"} 1', testo)
        self.assertIn('langbrain_llm_circuit_state{model="ministral-8b-latest",provider="mistral"} 2', testo)

    async def test_riparte_dopo_la_pausa_con_una_chiamata_di_prova(self):
        orologio = Orologio()
        protezione._orologio = orologio
        self.addCleanup(setattr, protezione, "_orologio", __import__("time").monotonic)
        with patch.object(Mao, "_chiedi_modello", AsyncMock(side_effect=ErroreLLM("ERRORE_PROVIDER", "500"))):
            for _ in range(2):
                with self.assertRaises(ErroreLLM):
                    await self.chiama()
        orologio.t += 61
        with patch.object(Mao, "_chiedi_modello", AsyncMock(return_value="ok")):
            self.assertEqual(await self.chiama(), "ok")
            self.assertEqual(await self.chiama(), "ok")   # chiuso di nuovo

    async def test_il_budget_esaurito_ferma_la_chiamata_con_un_codice_chiaro(self):
        imposta_configurazione(costruisci_configurazione({"llm": {"budget": {"mistral": {"richieste_ora": 1}}}}))
        with patch.object(Mao, "_chiedi_modello", AsyncMock(return_value="ok")) as chiedi:
            self.assertEqual(await self.chiama(), "ok")
            with self.assertRaises(ErroreLLM) as ctx:
                await self.chiama()
        self.assertEqual(ctx.exception.codice, "BUDGET_ESAURITO")
        self.assertEqual(chiedi.await_count, 1)

    async def test_il_timeout_per_modello_interrompe_una_chiamata_lenta(self):
        imposta_configurazione(costruisci_configurazione({"llm": {"timeout_secondi": {"mistral": 0.05}}}))

        async def lenta(*a, **k):
            await asyncio.sleep(5)

        with patch.object(Mao, "_chiedi_modello", lenta), self.assertRaises(ErroreLLM) as ctx:
            await self.chiama()
        self.assertEqual(ctx.exception.codice, "PROVIDER_NON_RAGGIUNGIBILE")
        self.assertIn("timeout", ctx.exception.messaggio)

    async def test_una_chiamata_riuscita_conta_token_e_durata(self):
        with patch.object(Mao, "_chiedi_modello", AsyncMock(return_value="x" * 40)):
            await self.chiama()
        testo = await metriche.esporta()
        self.assertIn('langbrain_llm_calls_total{model="ministral-8b-latest",outcome="ok",provider="mistral"} 1', testo)
        self.assertIn('langbrain_llm_tokens_estimated_total{direction="out",provider="mistral"} 10', testo)
        self.assertIn('langbrain_llm_call_duration_seconds_count{provider="mistral"} 1', testo)


if __name__ == "__main__":
    unittest.main()
