import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from anthropic import AsyncAnthropic

from app.core.configurazione import costruisci_configurazione, imposta_configurazione
from app.core.errori_llm import ErroreLLM
from app.MAO.model_access_object import Mao, normalizza_provider

CHIAVI = {
    "GEMINI_API_KEY": "chiave-google", "OPENROUTER_API_KEY": "chiave-openrouter", "MISTRAL_API_KEY": "chiave-mistral",
}


def usa_configurazione(test, **llm):
    imposta_configurazione(costruisci_configurazione({"llm": llm}))
    test.addCleanup(imposta_configurazione, None)


def risposta(contenuto):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = contenuto
    r.choices[0].finish_reason = "stop"
    return r


class ProviderMistralTest(unittest.IsolatedAsyncioTestCase):
    async def crea_mao(self, **variabili):
        ambiente = {**CHIAVI, **variabili}
        for nome in ("MISTRAL_MODEL", "MISTRAL_BASE_URL", "MAO_FALLBACK"):
            if nome not in ambiente:
                self.enterContext(patch.dict(os.environ, {}, clear=False))
                os.environ.pop(nome, None)
        with patch.dict(os.environ, ambiente):
            mao = Mao()
        self.addAsyncCleanup(mao.aclose)
        return mao

    async def test_il_provider_mistral_e_registrato_con_url_e_modello_economico_di_default(self):
        mao = await self.crea_mao()
        cfg = mao.providers["mistral"]
        self.assertEqual(str(cfg["client"].base_url).rstrip("/"), "https://api.mistral.ai/v1")
        self.assertEqual(cfg["model"], "ministral-8b-latest")
        self.assertTrue(cfg["enabled"])

    async def test_modello_e_url_si_configurano_dall_ambiente(self):
        mao = await self.crea_mao(MISTRAL_MODEL="mistral-small-latest", MISTRAL_BASE_URL="https://proxy.example/v1")
        self.assertEqual(mao.providers["mistral"]["model"], "mistral-small-latest")
        self.assertEqual(str(mao.providers["mistral"]["client"].base_url).rstrip("/"), "https://proxy.example/v1")

    async def test_senza_chiave_o_con_segnaposto_il_provider_e_escluso(self):
        for valore in ("nessuna", "", "replace-with-your-mistral-api-key"):
            with self.subTest(valore=valore):
                mao = await self.crea_mao(MISTRAL_API_KEY=valore)
                self.assertFalse(mao.providers["mistral"]["enabled"])

    async def test_le_chiamate_a_mistral_usano_il_suo_client_e_l_alias(self):
        mao = await self.crea_mao()
        create = AsyncMock(return_value=risposta("ciao da mistral"))
        with patch.object(mao.providers["mistral"]["client"].chat.completions, "create", create):
            for nome in ("mistral", "mistralai"):
                self.assertEqual(await mao.call_model("s", "u", provider=nome), "ciao da mistral")
        self.assertEqual(create.await_args.kwargs["model"], "ministral-8b-latest")

    async def test_ai_modelli_di_ripiego_di_mistral_si_arriva_solo_con_il_ripiego_attivo(self):
        mao = await self.crea_mao(MAO_FALLBACK="1")
        create = AsyncMock(side_effect=ErroreLLM("LIMITE_RICHIESTE", "troppe richieste"))
        with patch.object(mao.providers["mistral"]["client"].chat.completions, "create", create):
            with self.assertRaises(ErroreLLM):
                await mao.call_model("s", "u", provider="mistral", fallback_on_error=False)
        self.assertEqual([c.kwargs["model"] for c in create.await_args_list], ["ministral-8b-latest", "ministral-3b-latest"])


class CatenaDiRipiegoTest(unittest.IsolatedAsyncioTestCase):
    async def crea_mao(self, **variabili):
        with patch.dict(os.environ, {**CHIAVI, **variabili}):
            mao = Mao()
        self.addAsyncCleanup(mao.aclose)
        return mao

    def esecutore(self, esiti):
        """Simula _execute_chat: esiti = {provider: ErroreLLM | testo}. Registra l'ordine dei tentativi."""
        tentativi = []

        async def _finto(**kwargs):
            tentativi.append(kwargs["provider_key"])
            esito = esiti[kwargs["provider_key"]]
            if isinstance(esito, Exception):
                raise esito
            return esito

        return AsyncMock(side_effect=_finto), tentativi

    async def test_il_ripiego_acceso_prova_i_provider_nell_ordine_configurato_fino_al_successo(self):
        usa_configurazione(self, fallback=True, ordine_provider=["google_studio", "mistral", "openrouter", "local"])
        mao = await self.crea_mao()
        finto, tentativi = self.esecutore({
            "openrouter": ErroreLLM("CREDITI_ESAURITI", "crediti"),
            "google_studio": ErroreLLM("LIMITE_RICHIESTE", "quota"),
            "mistral": "risposta di mistral",
        })

        with patch.object(mao, "_execute_chat", finto):
            risultato = await mao.call_model("s", "u", provider="openrouter")

        self.assertEqual(risultato, "risposta di mistral")
        self.assertEqual(tentativi, ["openrouter", "google_studio", "mistral"])

    async def test_l_ordine_dei_provider_viene_dalla_configurazione(self):
        usa_configurazione(self, fallback=True, ordine_provider=["mistral", "google_studio"])
        mao = await self.crea_mao()
        finto, tentativi = self.esecutore({"openrouter": ErroreLLM("LIMITE_RICHIESTE", "x"), "mistral": "ok"})
        with patch.object(mao, "_execute_chat", finto):
            await mao.call_model("s", "u", provider="openrouter")
        self.assertEqual(tentativi, ["openrouter", "mistral"])

    async def test_senza_ripiego_si_prova_solo_il_provider_scelto(self):
        usa_configurazione(self, fallback=False)
        mao = await self.crea_mao()
        finto, tentativi = self.esecutore({"openrouter": ErroreLLM("CREDITI_ESAURITI", "crediti"), "mistral": "ok"})

        with patch.object(mao, "_execute_chat", finto), self.assertRaises(ErroreLLM) as ctx:
            await mao.call_model("s", "u", provider="openrouter")

        self.assertEqual(tentativi, ["openrouter"])
        self.assertEqual(ctx.exception.codice, "CREDITI_ESAURITI")

    async def test_la_variabile_d_ambiente_ha_la_precedenza_sul_file(self):
        usa_configurazione(self, fallback=True)
        spento = await self.crea_mao(MAO_FALLBACK="0")
        acceso_dal_file = await self.crea_mao(MAO_FALLBACK="")
        self.assertFalse(spento.fallback)
        self.assertTrue(acceso_dal_file.fallback)

        usa_configurazione(self, fallback=False)
        acceso_da_env = await self.crea_mao(MAO_FALLBACK="1")
        self.assertTrue(acceso_da_env.fallback)

    async def test_i_provider_senza_chiave_vengono_saltati_nel_ripiego(self):
        usa_configurazione(self, fallback=True, ordine_provider=["google_studio", "mistral"])
        mao = await self.crea_mao(GEMINI_API_KEY="nessuna", GOOGLE_API_KEY="")
        finto, tentativi = self.esecutore({"openrouter": ErroreLLM("LIMITE_RICHIESTE", "x"), "mistral": "ok"})
        with patch.object(mao, "_execute_chat", finto):
            self.assertEqual(await mao.call_model("s", "u", provider="openrouter"), "ok")
        self.assertEqual(tentativi, ["openrouter", "mistral"])

    async def test_se_falliscono_tutti_l_errore_riportato_e_quello_del_provider_scelto(self):
        usa_configurazione(self, fallback=True, ordine_provider=["google_studio", "mistral"])
        mao = await self.crea_mao()
        finto, tentativi = self.esecutore({
            "openrouter": ErroreLLM("CREDITI_ESAURITI", "crediti", provider="openrouter"),
            "google_studio": ErroreLLM("LIMITE_RICHIESTE", "quota"),
            "mistral": ErroreLLM("CHIAVE_NON_VALIDA", "chiave"),
        })

        with patch.object(mao, "_execute_chat", finto), self.assertRaises(ErroreLLM) as ctx:
            await mao.call_model("s", "u", provider="openrouter")

        self.assertEqual(ctx.exception.codice, "CREDITI_ESAURITI")
        self.assertEqual(len(ctx.exception.dettagli["tentativi"]), 3)

    async def test_i_modelli_di_ripiego_vengono_dalla_configurazione_e_sono_gratuiti(self):
        usa_configurazione(self, fallback=True, modelli_di_ripiego={"openrouter": ["modello-a:free", "modello-b:free"]})
        mao = await self.crea_mao()
        self.assertEqual(mao.providers["openrouter"]["fallback_models"], ["modello-a:free", "modello-b:free"])

        create = AsyncMock(side_effect=ErroreLLM("LIMITE_RICHIESTE", "x"))
        with patch.object(mao.providers["openrouter"]["client"].chat.completions, "create", create):
            with self.assertRaises(ErroreLLM):
                await mao.call_model("s", "u", provider="openrouter", fallback_on_error=False)
        self.assertEqual(
            [c.kwargs["model"] for c in create.await_args_list],
            [mao.providers["openrouter"]["model"], "modello-a:free", "modello-b:free"],
        )

    async def test_un_errore_di_chiave_o_crediti_non_prova_altri_modelli_dello_stesso_provider(self):
        usa_configurazione(self, fallback=True, modelli_di_ripiego={"openrouter": ["modello-a:free", "modello-b:free"]})
        mao = await self.crea_mao()
        for codice in ("CHIAVE_NON_VALIDA", "CREDITI_ESAURITI", "PROVIDER_NON_RAGGIUNGIBILE"):
            with self.subTest(codice=codice):
                create = AsyncMock(side_effect=ErroreLLM(codice, "x"))
                with patch.object(mao.providers["openrouter"]["client"].chat.completions, "create", create):
                    with self.assertRaises(ErroreLLM):
                        await mao.call_model("s", "u", provider="openrouter", fallback_on_error=False)
                self.assertEqual(create.await_count, 1)

    async def test_un_errore_specifico_del_modello_prova_il_modello_di_ripiego(self):
        usa_configurazione(self, fallback=True, modelli_di_ripiego={"openrouter": ["modello-a:free"]})
        mao = await self.crea_mao()
        create = AsyncMock(side_effect=[ErroreLLM("LIMITE_RICHIESTE", "x"), risposta("dal ripiego")])
        with patch.object(mao.providers["openrouter"]["client"].chat.completions, "create", create):
            self.assertEqual(await mao.call_model("s", "u", provider="openrouter", fallback_on_error=False), "dal ripiego")
        self.assertEqual(create.await_args_list[1].kwargs["model"], "modello-a:free")

    async def test_i_modelli_di_ripiego_predefiniti_non_contengono_modelli_a_pagamento(self):
        usa_configurazione(self, fallback=True)
        mao = await self.crea_mao()
        for modello in mao.providers["openrouter"]["fallback_models"]:
            self.assertTrue(modello.endswith(":free"), modello)
        self.assertNotIn("openrouter/auto", mao.providers["openrouter"]["fallback_models"])


if __name__ == "__main__":
    unittest.main()


class ProviderAnthropicTest(unittest.IsolatedAsyncioTestCase):
    def crea_mao(self, **env):
        base = {"ANTHROPIC_API_KEY": "chiave-anthropic", **env}
        os.environ.pop("ANTHROPIC_MODEL", None)
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        with patch.dict(os.environ, base):
            mao = Mao()
        self.addAsyncCleanup(mao.aclose)
        return mao

    @staticmethod
    def messaggio(testo, stop_reason="end_turn"):
        blocco = MagicMock(type="text")
        blocco.text = testo
        return MagicMock(content=[blocco], stop_reason=stop_reason)

    async def test_il_provider_anthropic_usa_il_client_nativo_con_modello_di_default(self):
        cfg = self.crea_mao().providers["anthropic"]
        self.assertIsInstance(cfg["client"], AsyncAnthropic)
        self.assertEqual(str(cfg["client"].base_url).rstrip("/"), "https://api.anthropic.com")
        self.assertEqual(cfg["model"], "claude-haiku-5-5")
        self.assertTrue(cfg["enabled"])

    async def test_senza_chiave_o_con_il_segnaposto_e_disabilitato(self):
        for valore in ("nessuna", "", "replace-with-your-anthropic-api-key"):
            with self.subTest(valore=valore):
                self.assertFalse(self.crea_mao(ANTHROPIC_API_KEY=valore).providers["anthropic"]["enabled"])

    async def test_la_chiamata_usa_messages_api_con_system_separato_e_l_alias_claude(self):
        mao = self.crea_mao()
        self.assertEqual(normalizza_provider("Claude"), "anthropic")
        create = AsyncMock(return_value=self.messaggio("ciao da claude"))
        with patch.object(mao.providers["anthropic"]["client"].messages, "create", create):
            self.assertEqual(await mao.call_model("sistema", "utente", provider="claude", max_tokens=100), "ciao da claude")
        kwargs = create.call_args.kwargs
        self.assertEqual((kwargs["system"], kwargs["messages"], kwargs["max_tokens"]),
                         ("sistema", [{"role": "user", "content": "utente"}], 100))

    async def test_risposta_troncata_raddoppia_i_token(self):
        mao = self.crea_mao()
        create = AsyncMock(side_effect=[self.messaggio("tagl", "max_tokens"), self.messaggio("completa")])
        with patch.object(mao.providers["anthropic"]["client"].messages, "create", create):
            risultato = await mao.call_model("s", "u", provider="anthropic", max_tokens=10, rigenera_se_troncata=True)
        self.assertEqual(risultato, "completa")
        self.assertEqual([c.kwargs["max_tokens"] for c in create.call_args_list], [mao.max_tokens_minimo, mao.max_tokens_minimo * 2])
