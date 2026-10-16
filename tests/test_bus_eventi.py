"""Bus degli eventi: id monotono mai riusato, Last-Event-ID, buffer, solo ciò che si pubblica."""

import asyncio
import json
import os
import re
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

import sqlalchemy as sa

from app.core import archivio, bus_eventi, contesto
from app.db import schema
from app.db.database import Database
from app.tools.nervous_system import NervousSystem

ISO_MS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


class BusTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = os.path.join(tempfile.mkdtemp(prefix="lb_bus_"), "b.db")
        await Database(self.db).init_db()
        self.addAsyncCleanup(archivio.chiudi_motori)

    async def pubblica(self, evento="device.state", data=None, **kw):
        return await bus_eventi.pubblica(evento, data, db_path=self.db, **kw)

    async def test_gli_id_crescono_e_l_involucro_e_completo(self):
        with mock.patch.dict(os.environ, {"LANGBRAIN_APARTMENT_ID": "ap01"}):
            a = await self.pubblica("device.state", {"device_id": "luce", "state": "ON"}, correlation_id="c-1")
            b = await self.pubblica("command.updated", {"command_id": "x", "status": "acked"})
        self.assertLess(a, b)
        eventi = await bus_eventi.leggi_da(0, db_path=self.db)
        self.assertEqual([e["id"] for e in eventi], [a, b])
        primo = eventi[0]
        self.assertEqual(set(primo), {"id", "event", "ts", "apartment_id", "correlation_id", "data"})
        self.assertEqual((primo["event"], primo["apartment_id"], primo["correlation_id"]), ("device.state", "ap01", "c-1"))
        self.assertEqual(primo["data"], {"device_id": "luce", "state": "ON"})
        self.assertRegex(primo["ts"], ISO_MS)

    async def test_il_correlation_id_arriva_dal_contesto(self):
        with contesto.contesto("cid-ctx", None):
            await self.pubblica("decision.logged")
        self.assertEqual((await bus_eventi.leggi_da(0, db_path=self.db))[0]["correlation_id"], "cid-ctx")

    async def test_last_event_id_riconsegna_esattamente_gli_eventi_persi_in_ordine(self):
        ids = [await self.pubblica("device.online", {"n": i}) for i in range(6)]
        persi = await bus_eventi.leggi_da(ids[2], db_path=self.db)
        self.assertEqual([e["id"] for e in persi], ids[3:])
        self.assertEqual([e["data"]["n"] for e in persi], [3, 4, 5])
        self.assertEqual(await bus_eventi.leggi_da(ids[-1], db_path=self.db), [])

    async def test_l_id_non_si_riusa_dopo_la_pulizia_del_buffer(self):
        ultimo = await self.pubblica()
        async with archivio.motore(self.db).begin() as conn:  # il buffer scade: tutto tolto
            await conn.execute(sa.delete(schema.stream_events))
        self.assertEqual(await bus_eventi.ultimo_id(self.db), 0)
        nuovo = await self.pubblica()
        self.assertGreater(nuovo, ultimo)

    async def test_l_id_non_si_riusa_dopo_un_riavvio(self):
        prima = [await self.pubblica() for _ in range(3)]
        await archivio.chiudi_motori()  # il processo si ferma...
        async with archivio.motore(self.db).begin() as conn:
            await conn.execute(sa.delete(schema.stream_events))
        await archivio.chiudi_motori()  # ...e riparte con un motore nuovo
        dopo = await self.pubblica()
        self.assertGreater(dopo, max(prima))

    async def test_tipi_non_ammessi_sono_rifiutati(self):
        for tipo in ("node.run", "graph.interrupt", "device.stato"):
            with self.assertRaisesRegex(ValueError, "non ammesso"):
                await self.pubblica(tipo)
        self.assertEqual(await bus_eventi.leggi_da(0, db_path=self.db), [])
        self.assertEqual(set(bus_eventi.TIPI_EVENTO), {
            "device.state", "device.online", "command.updated", "decision.logged",
            "approval.pending", "apartment.changed", "instance.health"})

    async def test_gli_eventi_interni_del_grafo_non_finiscono_nel_bus(self):
        log = NervousSystem(db_path=self.db)
        await log.log_event("brain", "X", "t", "1", "2", "r")
        await log.record_node_run("run1", "brain", "2026-10-16T10:00:00+00:00", 5.0, "ok")
        self.assertEqual(await bus_eventi.leggi_da(0, db_path=self.db), [])

    async def test_il_buffer_tiene_almeno_un_ora(self):
        vecchio = schema.iso_ms(schema.adesso_utc() - timedelta(hours=30))
        poco_fa = schema.iso_ms(schema.adesso_utc() - timedelta(minutes=50))
        async with archivio.motore(self.db).begin() as conn:
            for ts in (vecchio, poco_fa):
                await conn.execute(sa.insert(schema.stream_events).values(event="device.state", ts=ts, data="{}"))
        with mock.patch.dict(os.environ, {bus_eventi.VARIABILE_BUFFER: "0.1"}):  # sotto il minimo: vale 1 ora
            self.assertEqual(bus_eventi.ore_buffer(), 1.0)
            self.assertEqual(await bus_eventi.pulisci_buffer(self.db), 1)
        self.assertEqual(len(await bus_eventi.leggi_da(0, db_path=self.db)), 1)

    async def test_attendi_nuovi_si_risveglia_subito_nello_stesso_processo(self):
        inizio = asyncio.get_running_loop().time()
        attesa = asyncio.create_task(bus_eventi.attendi_nuovi(0, 5, self.db))
        await asyncio.sleep(0.05)
        nuovo = await self.pubblica("apartment.changed", {"mode": "eco"})
        eventi = await asyncio.wait_for(attesa, timeout=2)
        self.assertEqual([e["id"] for e in eventi], [nuovo])
        self.assertLess(asyncio.get_running_loop().time() - inizio, 1.0)  # entro un secondo dal fatto

    async def test_attendi_nuovi_scade_senza_eventi(self):
        self.assertEqual(await bus_eventi.attendi_nuovi(0, 0.3, self.db), [])

    async def test_flusso_sse_riconsegna_segue_e_manda_ping(self):
        ids = [await self.pubblica("device.state", {"n": i}) for i in range(3)]
        sorgente = bus_eventi.flusso(ids[0], ping_secondi=0.3, db_path=self.db)
        self.addAsyncCleanup(sorgente.aclose)
        blocchi = [await asyncio.wait_for(sorgente.__anext__(), 2) for _ in range(2)]
        self.assertEqual([b.split("\n")[0] for b in blocchi], [f"id: {ids[1]}", f"id: {ids[2]}"])
        self.assertTrue(blocchi[0].endswith("\n\n"))
        dati = json.loads(next(r for r in blocchi[0].split("\n") if r.startswith("data: "))[6:])
        self.assertEqual(dati["data"], {"n": 1})
        self.assertEqual(await asyncio.wait_for(sorgente.__anext__(), 2), ": ping\n\n")  # silenzio -> ping
        nuovo = await self.pubblica("instance.health", {"status": "ok"})
        seguente = await asyncio.wait_for(sorgente.__anext__(), 2)
        self.assertTrue(seguente.startswith(f"id: {nuovo}\nevent: instance.health\n"))

    async def test_senza_last_event_id_il_flusso_parte_dal_prossimo(self):
        await self.pubblica("device.state", {"vecchio": True})
        sorgente = bus_eventi.flusso(None, ping_secondi=0.3, db_path=self.db)
        self.addAsyncCleanup(sorgente.aclose)
        self.assertEqual(await asyncio.wait_for(sorgente.__anext__(), 2), ": ping\n\n")


if __name__ == "__main__":
    unittest.main()
