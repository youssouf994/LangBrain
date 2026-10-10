import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage
from langgraph.types import Command

from app.agents.base_agent import BaseAgent
from app.core.errori_llm import (
    ErroreLLM, ISTRUZIONE_ERRORE_STANDARD, classifica_errore, interpreta_risposta_standard, oscura_segreti,
    risposta_standard,
)
from app.graph.builder import build_graph
from app.graph.hitl_config import hitl_manager
from app.graph.orchestrator import BrainAgent
from app.MAO import model_access_object as modulo_mao
from app.MAO.model_access_object import Mao
from app.tools.sensor_tools import IoTDeviceTool


class ErroreConStato(Exception):
    def __init__(self, stato, messaggio="errore del provider"):
        super().__init__(messaggio)
        self.status_code = stato


def risposta(contenuto, finish_reason="stop"):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = contenuto
    r.choices[0].finish_reason = finish_reason
    return r


class ClassificazioneErroriTest(unittest.TestCase):
    def test_codici_per_stato_http(self):
        casi = {401: "CHIAVE_NON_VALIDA", 403: "CHIAVE_NON_VALIDA", 402: "CREDITI_ESAURITI", 429: "LIMITE_RICHIESTE",
                404: "MODELLO_NON_TROVATO", 500: "ERRORE_PROVIDER", 503: "ERRORE_PROVIDER"}
        for stato, atteso in casi.items():
            with self.subTest(stato=stato):
                self.assertEqual(classifica_errore(ErroreConStato(stato))[0], atteso)

    def test_chiave_errata_di_google_e_riconosciuta_anche_con_stato_400(self):
        errore = ErroreConStato(400, "API key not valid. Please pass a valid API key.")
        self.assertEqual(classifica_errore(errore)[0], "CHIAVE_NON_VALIDA")
        self.assertEqual(classifica_errore(ErroreConStato(400, "richiesta malformata"))[0], "ERRORE_IMPREVISTO")

    def test_contesto_superato_e_timeout(self):
        self.assertEqual(classifica_errore(ErroreConStato(400, "maximum context length is 8192"))[0], "CONTESTO_SUPERATO")
        self.assertEqual(classifica_errore(TimeoutError("lento"))[0], "PROVIDER_NON_RAGGIUNGIBILE")
        self.assertEqual(classifica_errore(ConnectionError("rifiutata"))[0], "PROVIDER_NON_RAGGIUNGIBILE")

    def test_ogni_codice_ha_un_suggerimento_per_l_operatore(self):
        errore = ErroreLLM("CREDITI_ESAURITI", "crediti insufficienti", provider="openrouter", modello="qwen")
        self.assertIn("ricarica", errore.suggerimento.casefold())
        self.assertEqual(errore.come_dizionario()["provider"], "openrouter")

    def test_le_chiavi_api_non_compaiono_nei_messaggi(self):
        # Chiavi finte costruite a pezzi: nessuna stringa nel sorgente ha il formato di una chiave vera, così gli
        # scanner di segreti (gitleaks, GitHub push protection) non le segnalano.
        chiave_openai = "sk" + "-" + "abcdef1234567890" + "XYZ"
        chiave_google = "AI" + "za" + "SyA1234567890abcdefgh"
        bearer = "Bearer " + "abcdefghijkl1234567890"
        messaggio = f"Incorrect API key provided: {chiave_openai} e {chiave_google} ({bearer})"

        oscurato = oscura_segreti(messaggio)

        for segreto in (chiave_openai, chiave_google, "abcdefghijkl1234567890"):
            self.assertNotIn(segreto, oscurato)
            self.assertNotIn(segreto, ErroreLLM("CHIAVE_NON_VALIDA", messaggio).messaggio)
        self.assertIn("***", oscurato)

    def test_risposta_standard_e_riconoscimento(self):
        testo = risposta_standard(ErroreLLM("RISPOSTA_TRONCATA", "interrotta"))
        self.assertTrue(testo.startswith("LLM_ERRORE"))
        self.assertIn("RISPOSTA_TRONCATA", interpreta_risposta_standard(testo))
        self.assertEqual(interpreta_risposta_standard("llm_errore: troppo lungo"), "troppo lungo")
        self.assertIsNone(interpreta_risposta_standard("DECISIONE: ACTION"))
        self.assertIsNone(interpreta_risposta_standard(None))


class MaoErroriTest(unittest.IsolatedAsyncioTestCase):
    async def crea_mao(self, **variabili):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "chiave-di-prova", **variabili}):
            mao = Mao()
        self.addAsyncCleanup(mao.aclose)
        mao.providers["openrouter"]["enabled"] = True
        create = AsyncMock()
        self.enterContext(patch.object(mao.providers["openrouter"]["client"].chat.completions, "create", create))
        return mao, create

    async def test_risposta_troncata_raddoppia_i_token_fino_a_ottenere_il_testo(self):
        mao, create = await self.crea_mao(MAO_MAX_TOKENS_MINIMO="100", MAO_MAX_TOKENS_LIMITE="800")
        create.side_effect = [risposta("parziale", "length"), risposta("completa", "length"), risposta("finale", "stop")]

        testo = await mao.call_model("s", "u", max_tokens=10, provider="openrouter", rigenera_se_troncata=True)

        self.assertEqual(testo, "finale")
        self.assertEqual([c.kwargs["max_tokens"] for c in create.await_args_list], [100, 200, 400])

    async def test_risposta_sempre_troncata_solleva_errore_con_il_limite_raggiunto(self):
        mao, create = await self.crea_mao(MAO_MAX_TOKENS_MINIMO="100", MAO_MAX_TOKENS_LIMITE="200")
        create.return_value = risposta("parziale", "length")

        with self.assertRaises(ErroreLLM) as ctx:
            await mao.call_model("s", "u", provider="openrouter", rigenera_se_troncata=True)

        self.assertEqual(ctx.exception.codice, "RISPOSTA_TRONCATA")
        self.assertEqual(create.await_args_list[-1].kwargs["max_tokens"], 200)

    async def test_senza_rigenerazione_i_token_richiesti_sono_rispettati_e_la_risposta_parziale_restituita(self):
        mao, create = await self.crea_mao(MAO_MAX_TOKENS_MINIMO="100")
        create.return_value = risposta("pr", "length")

        testo = await mao.call_model("s", "u", max_tokens=30, provider="openrouter")

        self.assertEqual(testo, "pr")
        self.assertEqual(create.await_args.kwargs["max_tokens"], 30)

    async def test_valori_predefiniti_dei_token_sono_adeguati_ai_modelli_con_ragionamento(self):
        with patch.dict(os.environ, {}, clear=False):
            for nome in ("MAO_MAX_TOKENS_MINIMO", "MAO_MAX_TOKENS_LIMITE"):
                os.environ.pop(nome, None)
            mao = Mao()
        self.addAsyncCleanup(mao.aclose)
        self.assertEqual((mao.max_tokens_minimo, mao.max_tokens_limite), (8192, 32768))

    async def test_risposta_vuota_e_un_errore(self):
        mao, create = await self.crea_mao()
        create.return_value = risposta(None)
        with self.assertRaises(ErroreLLM) as ctx:
            await mao.call_model("s", "u", provider="openrouter")
        self.assertEqual(ctx.exception.codice, "RISPOSTA_VUOTA")

    async def test_errore_del_provider_e_classificato_e_non_ripiega_su_altri_modelli(self):
        mao, create = await self.crea_mao()
        create.side_effect = ErroreConStato(402, "Insufficient credits")

        with self.assertRaises(ErroreLLM) as ctx:
            await mao.call_model("s", "u", provider="openrouter")

        errore = ctx.exception
        self.assertEqual((errore.codice, errore.provider), ("CREDITI_ESAURITI", "openrouter"))
        self.assertIn("Nessun provider LLM disponibile", errore.messaggio)
        self.assertEqual(create.await_count, 1)

    async def test_con_mao_fallback_provano_anche_i_modelli_di_ripiego(self):
        mao, create = await self.crea_mao(MAO_FALLBACK="1")
        create.side_effect = ErroreConStato(429, "limite")

        with self.assertRaises(ErroreLLM):
            await mao.call_model("s", "u", provider="openrouter", fallback_on_error=False)

        self.assertGreater(create.await_count, 1)

    async def test_provider_senza_chiave_e_segnalato_con_il_suo_codice(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "nessuna"}):
            mao = Mao()
        self.addAsyncCleanup(mao.aclose)
        with self.assertRaises(ErroreLLM) as ctx:
            await mao.call_model("s", "u", provider="openrouter")
        self.assertEqual(ctx.exception.codice, "CHIAVE_NON_CONFIGURATA")

    async def test_errore_del_provider_scelto_non_e_coperto_dal_successo_di_un_altro(self):
        mao, create = await self.crea_mao()
        create.side_effect = ErroreConStato(401, "chiave rifiutata")
        mao.providers["google_studio"]["enabled"] = True
        google = AsyncMock(return_value=risposta("risposta di google"))
        with patch.object(mao.providers["google_studio"]["client"].chat.completions, "create", google):
            with self.assertRaises(ErroreLLM):
                await mao.call_model("s", "u", provider="openrouter")
        google.assert_not_awaited()


class RicaricaEnvTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self._cartella.cleanup)
        self.file_env = os.path.join(self._cartella.name, ".env")
        self.scrivi("OPENROUTER_MODEL=modello-a\nDEFAULT_PROVIDER=openrouter\n")
        self.enterContext(patch.dict(os.environ, {"OPENROUTER_MODEL": "modello-a", "DEFAULT_PROVIDER": "google_studio"}))
        self.enterContext(patch.object(modulo_mao, "_PERCORSO_ENV", self.file_env))
        self.enterContext(patch.object(modulo_mao, "_snapshot_env", modulo_mao.dotenv_values(self.file_env)))
        self.enterContext(patch.object(modulo_mao, "_ultimo_mtime_env", modulo_mao._mtime_env()))

    def scrivi(self, contenuto):
        with open(self.file_env, "w") as f:
            f.write(contenuto)
        stat = os.stat(self.file_env)
        os.utime(self.file_env, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    def test_applica_solo_le_variabili_cambiate_nel_file(self):
        self.scrivi("OPENROUTER_MODEL=modello-b\nDEFAULT_PROVIDER=openrouter\n")

        self.assertTrue(modulo_mao.ricarica_env_se_modificato())

        self.assertEqual(os.environ["OPENROUTER_MODEL"], "modello-b")
        # invariata nel file: la variabile impostata a mano nell'ambiente non viene sovrascritta
        self.assertEqual(os.environ["DEFAULT_PROVIDER"], "google_studio")

    def test_file_non_modificato_non_cambia_nulla(self):
        self.assertFalse(modulo_mao.ricarica_env_se_modificato())

    async def test_un_mao_gia_creato_adotta_la_nuova_configurazione_alla_chiamata_successiva(self):
        mao = Mao()
        self.addAsyncCleanup(mao.aclose)
        self.assertEqual(mao.providers["openrouter"]["model"], "modello-a")

        self.scrivi("OPENROUTER_MODEL=modello-b\nDEFAULT_PROVIDER=openrouter\n")
        await mao._aggiorna_configurazione_se_cambiata()

        self.assertEqual(mao.providers["openrouter"]["model"], "modello-b")


class ChiaveDalleImpostazioniTest(unittest.IsolatedAsyncioTestCase):
    """Una chiave scritta dalla pagina delle impostazioni vale alla chiamata successiva, senza riavviare."""

    def setUp(self):
        self._cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self._cartella.cleanup)
        self.file_env = os.path.join(self._cartella.name, ".env")
        self.enterContext(patch.dict(os.environ, {"MISTRAL_API_KEY": "", "LANGBRAIN_ENV_FILE": self.file_env}))
        self.enterContext(patch.object(modulo_mao, "_snapshot_env", {}))
        self.enterContext(patch.object(modulo_mao, "_ultimo_mtime_env", None))

    async def test_la_chiave_salvata_dalle_impostazioni_abilita_il_provider(self):
        from app.core import settings_editor
        with open(self.file_env, "w") as f:
            f.write("DEFAULT_PROVIDER=mistral\n")
        self.enterContext(patch.object(modulo_mao, "_PERCORSO_ENV", self.file_env))
        mao = Mao()
        self.addAsyncCleanup(mao.aclose)
        self.assertFalse(mao.providers["mistral"]["enabled"])

        settings_editor.save({"env:MISTRAL_API_KEY": "chiave-di-prova-1234567890"})
        await mao._aggiorna_configurazione_se_cambiata()

        self.assertTrue(mao.providers["mistral"]["enabled"])

    async def test_un_env_creato_dopo_l_avvio_viene_trovato(self):
        from app.core import settings_editor
        self.enterContext(patch.object(modulo_mao, "_PERCORSO_ENV", ""))  # all'avvio non c'era alcun .env
        mao = Mao()
        self.addAsyncCleanup(mao.aclose)
        self.assertFalse(mao.providers["mistral"]["enabled"])

        settings_editor.save({"env:MISTRAL_API_KEY": "chiave-di-prova-1234567890"})  # crea il file
        await mao._aggiorna_configurazione_se_cambiata()

        self.assertEqual(modulo_mao._PERCORSO_ENV, self.file_env)
        self.assertTrue(mao.providers["mistral"]["enabled"])


class AskBrainTest(unittest.IsolatedAsyncioTestCase):
    def crea_agente(self):
        class Agente(BaseAgent):
            async def process(self, *a):
                return {}

        return Agente("agente_llm_prova", ["lampada"], 30, 1.0)

    async def test_il_prompt_di_sistema_include_l_istruzione_standard_e_chiede_token_adeguati(self):
        agente = self.crea_agente()
        agente.mao.call_model = AsyncMock(return_value="DECISIONE: NONE")

        await agente.ask_brain("prompt originale", "utente")

        argomenti = agente.mao.call_model.await_args
        self.assertTrue(argomenti.args[0].startswith("prompt originale"))
        self.assertIn(ISTRUZIONE_ERRORE_STANDARD, argomenti.args[0])
        self.assertTrue(argomenti.kwargs["rigenera_se_troncata"])

    async def test_errore_llm_non_diventa_mai_una_risposta(self):
        agente = self.crea_agente()
        agente.mao.call_model = AsyncMock(side_effect=ErroreLLM("CREDITI_ESAURITI", "crediti insufficienti"))
        with self.assertRaises(ErroreLLM) as ctx:
            await agente.ask_brain("s", "u")
        self.assertEqual(ctx.exception.codice, "CREDITI_ESAURITI")

    async def test_eccezione_imprevista_viene_classificata(self):
        agente = self.crea_agente()
        agente.mao.call_model = AsyncMock(side_effect=ErroreConStato(429, "troppe richieste"))
        with self.assertRaises(ErroreLLM) as ctx:
            await agente.ask_brain("s", "u")
        self.assertEqual(ctx.exception.codice, "LIMITE_RICHIESTE")

    async def test_risposta_standard_del_modello_diventa_errore(self):
        agente = self.crea_agente()
        agente.mao.call_model = AsyncMock(return_value="LLM_ERRORE: contesto troppo lungo")
        with self.assertRaises(ErroreLLM) as ctx:
            await agente.ask_brain("s", "u")
        self.assertEqual(ctx.exception.codice, "RISPOSTA_NON_UTILIZZABILE")
        self.assertIn("troppo lungo", ctx.exception.messaggio)


class OverrideSenzaModelloTest(unittest.IsolatedAsyncioTestCase):
    async def test_senza_risposta_valida_dell_llm_non_viene_eseguito_nessun_comando(self):
        serratura = IoTDeviceTool("front_door_lock", initial_value="UNLOCKED")
        brain = BrainAgent(tools=[serratura])
        brain.ask_brain = AsyncMock(side_effect=ErroreLLM("LIMITE_RICHIESTE", "troppe richieste"))
        brain.event_log.mark_resolved = AsyncMock()
        brain.event_log.log_event = AsyncMock()

        with self.assertRaises(ErroreLLM):
            await brain._execute_semantic_override("Blocca la porta", "front_door_lock", "UNBLOCK_AND_SET")

        self.assertEqual(await serratura.get_tool_value(), "UNLOCKED")
        brain.event_log.log_event.assert_not_awaited()


    async def test_una_risposta_che_non_e_un_json_di_comandi_non_esegue_nessun_comando(self):
        for risposta_llm in ("Non saprei cosa fare.", '["testo", 3]', '"solo una stringa"'):
            with self.subTest(risposta=risposta_llm):
                serratura = IoTDeviceTool("front_door_lock", initial_value="UNLOCKED")
                brain = BrainAgent(tools=[serratura])
                brain.ask_brain = AsyncMock(return_value=risposta_llm)
                brain.event_log.mark_resolved = AsyncMock()
                brain.event_log.log_event = AsyncMock()

                with self.assertRaises(ErroreLLM) as ctx:
                    await brain._execute_semantic_override("Blocca la porta", "front_door_lock", "UNBLOCK_AND_SET")

                self.assertEqual(ctx.exception.codice, "RISPOSTA_NON_UTILIZZABILE")
                self.assertEqual(await serratura.get_tool_value(), "UNLOCKED")
                brain.event_log.log_event.assert_not_awaited()


class DecisioneNonRiconosciutaTest(unittest.IsolatedAsyncioTestCase):
    def test_lettura_tollerante_del_markdown(self):
        letti = {
            "DECISIONE: ACTION\nMOTIVAZIONE: x": "ACTION",
            "**DECISIONE: ACTION**": "ACTION",
            "**DECISIONE:** ACTION": "ACTION",
            "decisione: `escalate`": "ESCALATE",
            "Ecco:\n_DECISIONE_: NONE": "NONE",
            "DECISIONE: **LOCK** front_door_lock": None,
            "Nessuna decisione": None,
            "DECISIONE: ACTIONABLE": None,
            "": None,
        }
        for risposta_llm, atteso in letti.items():
            with self.subTest(risposta=risposta_llm):
                self.assertEqual(BaseAgent.estrai_decisione(risposta_llm, ("ACTION", "ESCALATE", "NONE")), atteso)

    def crea_agente(self, risposta_llm, stato="UNLOCKED"):
        from app.agents.dynamic_agent import DynamicAgent

        tool = IoTDeviceTool("lampada", stato)
        agente = DynamicAgent("agente_decisione", ["lampada"], tools={"lampada": tool})
        agente.event_log.get_recent_events = AsyncMock(return_value=[])
        agente.event_log.log_event = AsyncMock()
        agente.ask_brain = AsyncMock(return_value=risposta_llm)
        return agente, tool

    async def test_una_decisione_non_riconoscibile_ferma_il_grafo_invece_di_assumere_nessuna_azione(self):
        agente, tool = self.crea_agente("DECISIONE: **LOCK** lampada\nMOTIVAZIONE: chiudere")

        with self.assertRaises(ErroreLLM) as ctx:
            await agente.process({"config": {}}, [], [], [])

        self.assertEqual(ctx.exception.codice, "RISPOSTA_NON_UTILIZZABILE")
        self.assertIn("LOCK", ctx.exception.messaggio)
        self.assertEqual(await tool.get_tool_value(), "UNLOCKED")

    async def test_la_decisione_con_markdown_viene_eseguita(self):
        agente, tool = self.crea_agente("**DECISIONE:** ACTION\nMOTIVAZIONE: accendere", stato="OFF")
        await agente.process({"config": {}}, [], [], [])
        self.assertEqual(await tool.get_tool_value(), "ON")

    async def test_un_conflitto_forza_l_escalation_anche_se_la_risposta_e_illeggibile(self):
        agente, _ = self.crea_agente("risposta illeggibile")
        conflitto = {"actor": "altro", "action": "FORCE_SHUTDOWN", "target": "lampada", "new_value": "OFF",
                     "timestamp": "2999-01-01 00:00:00"}

        risultato = await agente.process({"config": {}}, [conflitto], [], [])

        self.assertEqual(risultato["next_agent"], "brain")
        self.assertEqual(len(risultato["pending_escalations"]), 1)

    async def test_il_brain_non_scambia_una_risposta_illeggibile_per_un_rifiuto(self):
        tool = IoTDeviceTool("lampada", "OFF")
        brain = BrainAgent(tools=[tool])
        brain.ask_brain = AsyncMock(return_value="Non so cosa fare")
        brain.event_log.log_event = AsyncMock()
        brain.event_log.mark_resolved = AsyncMock()
        escalation = BaseAgent.create_escalation.__get__(crea_agente_prova())("lampada", "ON", "richiesta")

        with self.assertRaises(ErroreLLM):
            await brain.process(
                {"messages": [], "readings": [], "recent_events": [], "next_agent": "brain", "hitl_required": False,
                 "config": {}, "pending_escalations": [escalation]}, [], [], [escalation],
            )

        brain.event_log.log_event.assert_not_awaited()


def crea_agente_prova():
    class Agente(BaseAgent):
        async def process(self, *a):
            return {}

    return Agente("agente_prova_decisione", ["lampada"], 30, 1.0)


class AgenteConLlmInguastoTest(BaseAgent):
    """Agente di prova che solleva ErroreLLM per le prime `guasti` esecuzioni."""

    def __init__(self, guasti):
        super().__init__("agente_llm_guasto", ["lampada"], 30, 1.0)
        self.guasti = guasti
        self.esecuzioni = 0

    async def process(self, state, recent_events, relevant_readings, agent_escalations):
        self.esecuzioni += 1
        if self.esecuzioni <= self.guasti:
            raise ErroreLLM("CREDITI_ESAURITI", "Insufficient credits", provider="openrouter", modello="qwen/qwen3.8-27b:free")
        return {"next_agent": "END", "messages": [AIMessage(content="[agente_llm_guasto] decisione presa")]}


class PausaHitlPerErroreLlmTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        hitl_manager.update_config(hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)
        eventi = patch("app.tools.event_log.EventLog.get_recent_events", AsyncMock(return_value=[]))
        eventi.start()
        self.addCleanup(eventi.stop)
        self.config = {"configurable": {"thread_id": "pausa-llm"}}

    def costruisci(self, guasti):
        agente = AgenteConLlmInguastoTest(guasti)
        grafo, _ = build_graph(custom_agent_instances={"agente_llm_guasto": agente})
        stato = {
            "messages": [], "readings": [], "recent_events": [], "pending_escalations": [],
            "next_agent": "agente_llm_guasto", "hitl_required": False, "config": {},
        }
        return grafo, agente, stato

    async def test_il_grafo_si_ferma_con_causa_e_suggerimento_per_l_operatore(self):
        grafo, agente, stato = self.costruisci(guasti=1)

        await grafo.ainvoke(stato, config=self.config)
        istantanea = await grafo.aget_state(self.config)

        self.assertEqual(istantanea.next, ("agente_llm_guasto",))
        payload = istantanea.tasks[0].interrupts[0].value
        self.assertEqual(payload["type"], "llm_failure_human_intervention")
        self.assertEqual(payload["errore"]["codice"], "CREDITI_ESAURITI")
        self.assertEqual(payload["errore"]["modello"], "qwen/qwen3.8-27b:free")
        self.assertIn("ricarica", payload["errore"]["suggerimento"].casefold())
        self.assertEqual(agente.esecuzioni, 1)

    async def test_dopo_la_risoluzione_l_operatore_riprende_e_l_agente_riprova(self):
        grafo, agente, stato = self.costruisci(guasti=1)
        await grafo.ainvoke(stato, config=self.config)

        risultato = await grafo.ainvoke(Command(resume={"decision": "RETRY", "reasoning": "crediti ricaricati"}), config=self.config)

        self.assertEqual(risultato["messages"][-1].content, "[agente_llm_guasto] decisione presa")
        self.assertEqual((await grafo.aget_state(self.config)).next, ())
        self.assertEqual(agente.esecuzioni, 2)

    async def test_la_decisione_predefinita_dell_api_approva_equivale_a_riprovare(self):
        grafo, agente, stato = self.costruisci(guasti=1)
        await grafo.ainvoke(stato, config=self.config)
        risultato = await grafo.ainvoke(Command(resume={"decision": "APPROVA"}), config=self.config)
        self.assertEqual(risultato["messages"][-1].content, "[agente_llm_guasto] decisione presa")

    async def test_l_operatore_puo_rinunciare_e_il_ciclo_termina_senza_decisioni(self):
        grafo, agente, stato = self.costruisci(guasti=5)
        await grafo.ainvoke(stato, config=self.config)

        risultato = await grafo.ainvoke(Command(resume={"decision": "RESPINGI"}), config=self.config)

        self.assertIn("annullato dall'operatore", risultato["messages"][-1].content)
        self.assertEqual((await grafo.aget_state(self.config)).next, ())

    async def test_se_il_problema_persiste_il_grafo_si_ferma_di_nuovo(self):
        grafo, agente, stato = self.costruisci(guasti=99)
        await grafo.ainvoke(stato, config=self.config)

        risultato = await grafo.ainvoke(Command(resume={"decision": "RETRY"}), config=self.config)
        istantanea = await grafo.aget_state(self.config)

        self.assertIn("__interrupt__", risultato)
        self.assertEqual(istantanea.tasks[0].interrupts[0].value["type"], "llm_failure_human_intervention")


class StatoInterruptApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_is_interrupted_riconosce_anche_una_seconda_pausa_con_next_vuoto(self):
        from app.api.main import get_graph_state

        istantanea = MagicMock()
        istantanea.next = ()
        istantanea.values = {}
        istantanea.tasks = [MagicMock(id="t1", interrupts=["pausa"])]
        istantanea.tasks[0].name = "agente_llm_guasto"
        grafo = MagicMock()
        grafo.aget_state = AsyncMock(return_value=istantanea)

        with patch("app.api.main._graph", grafo):
            risposta = await get_graph_state("thread")

        self.assertTrue(risposta["is_interrupted"])


class GestoriApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_errore_llm_diventa_503_con_causa_e_suggerimento(self):
        import json
        from app.api.main import gestisci_errore_llm

        risposta_http = await gestisci_errore_llm(
            None, ErroreLLM("CREDITI_ESAURITI", "crediti insufficienti", provider="openrouter")
        )

        corpo = json.loads(risposta_http.body)
        self.assertEqual(risposta_http.status_code, 503)
        self.assertEqual(corpo["errore_llm"]["codice"], "CREDITI_ESAURITI")
        self.assertIn("suggerimento", corpo["errore_llm"])

    async def test_proxy_llm_risponde_503_con_il_messaggio_dell_errore(self):
        from fastapi import HTTPException
        from app.api.main import LlmProxyRequest, invoke_llm

        finto = MagicMock()
        finto.default_provider = "openrouter"
        finto.call_model = AsyncMock(side_effect=ErroreLLM("LIMITE_RICHIESTE", "troppe richieste"))
        finto.aclose = AsyncMock()

        with patch("app.MAO.model_access_object.Mao", return_value=finto):
            with self.assertRaises(HTTPException) as ctx:
                await invoke_llm(LlmProxyRequest(system_prompt="s", user_prompt="u"))

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("troppe richieste", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
