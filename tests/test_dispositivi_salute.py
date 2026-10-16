"""Dispositivo (valore, unità, limiti, online, last_seen) e salute del nucleo per `/health` e `/version` del contratto."""

import os
import re
import tempfile
import unittest
import uuid

from app.core import archivio, dispositivi, salute
from app.core.configurazione import costruisci_configurazione, imposta_configurazione
from app.db.database import Database
from app.tools.effectors import SimulatedEffector, registra_tool

ISO_MS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


class DispositivoTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.id = f"clima_{uuid.uuid4().hex[:6]}"
        self.tool = SimulatedEffector(self.id, "OFF", "°C")
        registra_tool(self.id, self.tool)
        imposta_configurazione(costruisci_configurazione({"dispositivi": {self.id: {"intervallo": [16, 30], "unita": "°C"}}}))
        self.addCleanup(imposta_configurazione, None)

    async def test_valore_unita_limiti_e_online(self):
        d = await dispositivi.descrivi_dispositivo(self.id)
        self.assertEqual((d["device_id"], d["value"], d["unit"], d["setpoint_min"], d["setpoint_max"], d["online"]),
                         (self.id, "OFF", "°C", 16.0, 30.0, True))
        self.assertRegex(d["last_seen"], ISO_MS)

    async def test_senza_limiti_in_configurazione_sono_nulli(self):
        imposta_configurazione(costruisci_configurazione({}))
        d = await dispositivi.descrivi_dispositivo(self.id)
        self.assertEqual((d["setpoint_min"], d["setpoint_max"]), (None, None))

    async def test_un_dispositivo_che_non_risponde_e_offline_senza_sollevare(self):
        await self.tool.set_tool_value("ON")
        visto = (await dispositivi.descrivi_dispositivo(self.id))["last_seen"]
        self.tool.imposta_guasto("Shelly non risponde")
        d = await dispositivi.descrivi_dispositivo(self.id)
        self.assertEqual((d["online"], d["value"], d["last_seen"]), (False, None, visto))  # l'ultima volta vista resta

    async def test_un_guasto_dei_soli_comandi_non_lo_rende_offline(self):
        self.tool.imposta_guasto("attuatore bloccato", solo_comandi=True)
        self.assertTrue((await dispositivi.descrivi_dispositivo(self.id))["online"])

    async def test_dispositivo_sconosciuto(self):
        self.assertIsNone(await dispositivi.descrivi_dispositivo("non_esiste_" + self.id))

    async def test_elenco(self):
        nomi = [d["device_id"] for d in await dispositivi.elenca_dispositivi()]
        self.assertIn(self.id, nomi)
        self.assertEqual(nomi, sorted(nomi))


class SaluteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = os.path.join(tempfile.mkdtemp(prefix="lb_sal_"), "s.db")
        await Database(self.db).init_db()
        self.addAsyncCleanup(archivio.chiudi_motori)

    async def test_i_campi_di_health_che_il_nucleo_conosce(self):
        s = await salute.salute_nucleo(self.db)
        self.assertEqual(set(s), {"implementation", "uptime_s", "database", "model_provider", "checked_at"})
        self.assertEqual(s["implementation"], {"name": "langbrain", "version": salute.VERSIONE_NUCLEO})
        self.assertEqual(s["database"], {"status": "ok"})
        self.assertIsInstance(s["uptime_s"], int)
        self.assertRegex(s["checked_at"], ISO_MS)
        self.assertIn(s["model_provider"]["status"], ("ok", "degraded", "unknown"))
        self.assertTrue(s["model_provider"]["provider"])

    async def test_database_irraggiungibile_non_solleva(self):
        stato = await salute.stato_database(os.path.join(tempfile.gettempdir(), "nessuna", "cartella", "x.db"))
        self.assertEqual(stato, {"status": "error"})

    async def test_la_versione_del_nucleo_e_quella_dichiarata(self):
        # `app.version` non si confronta: un nodo installato (app/nodes) può legittimamente dichiarare la versione del proprio prodotto.
        self.assertEqual(salute.implementazione(), {"name": "langbrain", "version": salute.VERSIONE_NUCLEO})
        self.assertRegex(salute.VERSIONE_NUCLEO, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
