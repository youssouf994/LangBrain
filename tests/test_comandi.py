"""`tenta_comando`: esiti strutturati, decisione registrata per ogni esito, nessuna eccezione verso chi chiama."""

import os
import tempfile
import unittest
import uuid

from app.core import archivio, comandi, contesto
from app.core.configurazione import costruisci_configurazione, imposta_configurazione
from app.core.priorita import registra_priorita
from app.core.risultati import ErroreTool
from app.db.database import Database
from app.tools.effectors import SimulatedEffector, registra_tool
from app.tools.nervous_system import NervousSystem


class ToolOffline(SimulatedEffector):
    async def set_tool_value(self, value):
        raise ErroreTool("Shelly non raggiungibile", codice="DEVICE_OFFLINE")


class ToolRifiuta(SimulatedEffector):
    async def set_tool_value(self, value):
        return False


class ToolRotto(SimulatedEffector):
    async def set_tool_value(self, value):
        raise ZeroDivisionError("bug")


class ComandiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = os.path.join(tempfile.mkdtemp(prefix="lb_cmd_"), "c.db")
        await Database(self.db).init_db()
        self.addAsyncCleanup(archivio.chiudi_motori)
        self.sfx = uuid.uuid4().hex[:6]
        self.luce = f"luce_{self.sfx}"
        registra_tool(self.luce, SimulatedEffector(self.luce, "OFF"))
        self.addCleanup(lambda: [comandi.rimuovi_regola(r) for r in comandi.regole_registrate()])

    async def _decisione(self, decision_id):
        eventi = await NervousSystem(db_path=self.db).get_recent_events()
        return next(e for e in eventi if e["event_id"] == decision_id)

    async def tenta(self, attore="user:anna@example.com", target=None, valore="ON", **extra):
        return await comandi.tenta_comando(attore, target or self.luce, valore, db_path=self.db, **extra)

    async def test_comando_applicato_scrive_una_decisione_applied(self):
        esito = await self.tenta(correlation_id="cid-1", command_id="cmd-7")
        self.assertEqual((esito.stato, esito.riuscito, esito.motivo_contratto), ("applicato", True, None))
        self.assertEqual((esito.valore_precedente, esito.valore_richiesto, esito.correlation_id), ("OFF", "ON", "cid-1"))
        d = await self._decisione(esito.decision_id)
        self.assertEqual((d["actor"], d["outcome"], d["correlation_id"], d["command_id"], d["old_value"], d["new_value"]),
                         ("user:anna@example.com", "applied", "cid-1", "cmd-7", "OFF", "ON"))
        self.assertEqual(await comandi.trova_tool(self.luce).get_tool_value(), "ON")

    async def test_il_correlation_id_arriva_dal_contesto_o_viene_generato(self):
        with contesto.contesto("cid-ctx", None):
            esito = await self.tenta()
        self.assertEqual(esito.correlation_id, "cid-ctx")
        senza = await self.tenta()
        self.assertTrue(senza.correlation_id)
        self.assertNotEqual(senza.correlation_id, "cid-ctx")

    async def test_l_attore_viene_normalizzato(self):
        esito = await self.tenta(attore="anna@example.com")
        self.assertEqual(esito.attore, "user:anna@example.com")
        self.assertEqual((await self._decisione(esito.decision_id))["actor"], "user:anna@example.com")

    async def test_valore_non_ammesso_e_fuori_limiti_senza_toccare_il_dispositivo(self):
        imposta_configurazione(costruisci_configurazione({"dispositivi": {self.luce: {"valori": ["ON", "OFF"]}}}))
        self.addCleanup(imposta_configurazione, None)
        esito = await self.tenta(valore="BLU")
        self.assertEqual((esito.stato, esito.motivo_contratto), ("fuori_limiti", "out_of_limits"))
        d = await self._decisione(esito.decision_id)
        self.assertEqual(d["outcome"], "rejected")
        self.assertEqual(await comandi.trova_tool(self.luce).get_tool_value(), "OFF")

    async def test_blocco_di_priorita_maggiore_e_un_esito_con_decisione_rejected(self):
        registra_priorita("sicurezza_x", 500.0)
        await NervousSystem(db_path=self.db).log_event("sicurezza_x", "FORCE_SHUTDOWN", self.luce, "ON", "OFF", "intrusione")
        esito = await self.tenta()
        self.assertEqual((esito.stato, esito.motivo_contratto), ("bloccato_da_priorita", "blocked_by_priority"))
        self.assertIn("sicurezza_x", esito.motivo)
        d = await self._decisione(esito.decision_id)
        self.assertEqual((d["outcome"], d["action"]), ("rejected", "REJECTED_SET_VALUE"))
        self.assertEqual(await comandi.trova_tool(self.luce).get_tool_value(), "OFF")

    async def test_il_brain_non_e_bloccato(self):
        registra_priorita("sicurezza_x", 500.0)
        await NervousSystem(db_path=self.db).log_event("sicurezza_x", "FORCE_SHUTDOWN", self.luce, "ON", "OFF", "intrusione")
        self.assertEqual((await self.tenta(attore="brain")).stato, "applicato")

    async def test_blocco_da_regola_registra_la_regola_come_attore_della_decisione(self):
        comandi.registra_regola("window_open_off", lambda r: "Finestra aperta: il clima resta spento." if r.valore == "ON" else None)
        esito = await self.tenta()
        self.assertEqual((esito.stato, esito.motivo_contratto, esito.regola), ("bloccato_da_regola", "blocked_by_rule", "window_open_off"))
        d = await self._decisione(esito.decision_id)
        self.assertEqual((d["actor"], d["outcome"]), ("rule:window_open_off", "rejected"))
        self.assertIn("Finestra aperta", d["reasoning"])
        self.assertEqual((await self.tenta(valore="OFF")).stato, "applicato")  # la regola non si oppone allo spegnimento

    async def test_regola_asincrona_e_regola_che_solleva_chiude_in_sicurezza(self):
        async def asincrona(r):
            return "no"
        comandi.registra_regola("a", asincrona)
        self.assertEqual((await self.tenta()).stato, "bloccato_da_regola")
        comandi.rimuovi_regola("a")

        def rotta(r):
            raise RuntimeError("bug nella regola")
        comandi.registra_regola("rotta", rotta)
        esito = await self.tenta()
        self.assertEqual((esito.stato, esito.motivo_contratto), ("errore_interno", "internal_error"))
        self.assertEqual(await comandi.trova_tool(self.luce).get_tool_value(), "OFF")  # nessun comando se la regola non risponde

    async def test_dispositivo_offline_rifiutato_ed_errore_interno(self):
        for classe, nome, atteso, contratto in (
            (ToolOffline, "off", "dispositivo_offline", "device_offline"),
            (ToolRifiuta, "rif", "rifiutato_dal_dispositivo", "device_rejected"),
            (ToolRotto, "rotto", "errore_interno", "internal_error"),
        ):
            target = f"{nome}_{self.sfx}"
            registra_tool(target, classe(target, "OFF"))
            esito = await self.tenta(target=target)
            self.assertEqual((esito.stato, esito.motivo_contratto), (atteso, contratto), nome)
            self.assertEqual((await self._decisione(esito.decision_id))["outcome"], "failed")

    async def test_dispositivo_sconosciuto_non_solleva(self):
        esito = await self.tenta(target=f"non_esiste_{self.sfx}")
        self.assertEqual(esito.stato, "dispositivo_offline")
        self.assertIsNotNone(esito.decision_id)

    async def test_un_database_irraggiungibile_non_fa_sollevare(self):
        esito = await comandi.tenta_comando("user:a@b.it", self.luce, "ON", db_path=os.path.join(tempfile.gettempdir(), "no", "no", "x.db"))
        self.assertIn(esito.stato, comandi.STATI)  # nessuna eccezione; la decisione può mancare
        self.assertTrue(esito.decision_id is None or isinstance(esito.decision_id, int))

    async def test_ogni_stato_ha_un_motivo_del_contratto(self):
        self.assertEqual(set(comandi.MOTIVO_CONTRATTO), set(comandi.STATI))
        attesi = {"device_offline", "device_rejected", "blocked_by_rule", "blocked_by_priority", "out_of_limits", "internal_error"}
        self.assertEqual({v for v in comandi.MOTIVO_CONTRATTO.values() if v}, attesi)


if __name__ == "__main__":
    unittest.main()
