"""Decisioni nel formato `Decision` del contratto 1.0.4: attori, target, valori nulli, filtri, ordine e cursore."""

import os
import re
import tempfile
import unittest
import uuid
from datetime import timedelta

import sqlalchemy as sa

from app.core import archivio, contesto, decisioni
from app.core.priorita import registra_priorita
from app.db import schema
from app.db.database import Database
from app.tools.nervous_system import NervousSystem

ISO_MS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
CAMPI = {"decision_id", "ts", "actor", "target", "action", "old_value", "new_value", "reasoning", "outcome", "command_id",
         "approval_id", "correlation_id"}


class AttoriETargetTest(unittest.TestCase):
    def test_gli_attori_nella_forma_del_contratto(self):
        registra_priorita("organ_security", 500.0)
        casi = {
            "Brain": "brain", "brain": "brain", "organ_security": "organ:organ_security", "component_x": "organ:component_x",
            "organ:clima": "organ:clima", "rule:window_open_off": "rule:window_open_off", "assistant": "assistant",
            "anna@example.com": "user:anna@example.com", "user:anna@example.com": "user:anna@example.com",
            "system:scheduler": "system", "system": "system", "user_manual": "system", "": "system", None: "system",
        }
        for ingresso, atteso in casi.items():
            self.assertEqual(decisioni.attore_per_contratto(ingresso), atteso, ingresso)

    def test_target(self):
        self.assertEqual(decisioni.target_per_contratto("lampada"), {"type": "device", "device_id": "lampada"})
        for tipo in ("apartment", "rules", "schedule"):
            self.assertEqual(decisioni.target_per_contratto(tipo), {"type": tipo})

    def test_cursore_opaco_e_malformato(self):
        self.assertEqual(decisioni.decodifica_cursore(decisioni.codifica_cursore(42)), 42)
        self.assertIsNone(decisioni.decodifica_cursore(None))
        with self.assertRaises(ValueError):
            decisioni.decodifica_cursore("non-un-cursore!!")


class RicercaTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = os.path.join(tempfile.mkdtemp(prefix="lb_dec_"), "d.db")
        await Database(self.db).init_db()
        self.log = NervousSystem(db_path=self.db)
        self.addAsyncCleanup(archivio.chiudi_motori)

    async def cerca(self, **kw):
        return await decisioni.cerca_decisioni(db_path=self.db, **kw)

    async def test_il_formato_e_completo_e_i_valori_mancanti_sono_nulli(self):
        cid = str(uuid.uuid4())
        await self.log.log_event("Brain", "SET_TEMP", "ac_salotto", "21", None, "Troppo caldo", correlation_id=cid,
                                 command_id="cmd-1")
        d = (await self.cerca())["items"][0]
        self.assertEqual(set(d), CAMPI)
        self.assertEqual((d["actor"], d["target"], d["old_value"], d["new_value"], d["outcome"], d["command_id"], d["correlation_id"]),
                         ("brain", {"type": "device", "device_id": "ac_salotto"}, "21", None, "applied", "cmd-1", cid))
        self.assertIsInstance(d["decision_id"], str)
        self.assertRegex(d["ts"], ISO_MS)
        self.assertIsNone(d["approval_id"])

    async def test_una_riga_vecchia_senza_correlation_id_esce_con_l_uuid_nullo(self):
        async with archivio.motore(self.db).begin() as conn:
            await conn.execute(sa.insert(schema.events).values(actor="vecchio", action="X", target="t", old_value="a", new_value="b"))
        d = (await self.cerca())["items"][0]
        self.assertEqual((d["correlation_id"], d["actor"]), (decisioni.UUID_NULLO, "system"))
        uuid.UUID(d["correlation_id"])  # resta un UUID valido

    async def test_dalla_piu_recente_con_cursore_senza_buchi_ne_doppioni(self):
        for i in range(7):
            await self.log.log_event("brain", "X", "t", str(i), str(i + 1), "r")
        vistii, cursore = [], None
        while True:
            pagina = await self.cerca(limite=3, cursore=cursore)
            vistii += [d["decision_id"] for d in pagina["items"]]
            cursore = pagina["next_cursor"]
            if cursore is None:
                break
        self.assertEqual(len(vistii), 7)
        self.assertEqual(vistii, sorted(vistii, key=int, reverse=True))
        self.assertEqual(len(set(vistii)), 7)

    async def test_l_ultima_pagina_non_ha_cursore(self):
        for i in range(3):
            await self.log.log_event("brain", "X", "t", "a", "b", "r")
        self.assertIsNone((await self.cerca(limite=3))["next_cursor"])
        self.assertIsNotNone((await self.cerca(limite=2))["next_cursor"])

    async def test_il_limite_e_portato_tra_1_e_200(self):
        for i in range(3):
            await self.log.log_event("brain", "X", "t", "a", "b", "r")
        self.assertEqual(len((await self.cerca(limite=0))["items"]), 1)
        self.assertEqual(len((await self.cerca(limite=10_000))["items"]), 3)

    async def test_filtri_device_correlation_e_attore(self):
        await self.log.log_event("Brain", "X", "luce", "OFF", "ON", "r", correlation_id="c-1")
        await self.log.log_event("organ_clima", "X", "clima", "OFF", "ON", "r", correlation_id="c-2")
        with contesto.contesto("c-3", "user:anna@example.com"):
            await self.log.log_event(None, "X", "luce", "ON", "OFF", "r")
        self.assertEqual({d["target"]["device_id"] for d in (await self.cerca(device_id="luce"))["items"]}, {"luce"})
        self.assertEqual([d["correlation_id"] for d in (await self.cerca(correlation_id="c-2"))["items"]], ["c-2"])
        self.assertEqual([d["actor"] for d in (await self.cerca(actor="brain"))["items"]], ["brain"])
        self.assertEqual([d["actor"] for d in (await self.cerca(actor="user:anna@example.com"))["items"]], ["user:anna@example.com"])
        self.assertEqual([d["actor"] for d in (await self.cerca(actor="organ:organ_clima"))["items"]], ["organ:organ_clima"])
        self.assertEqual((await self.cerca(actor="rule:nessuna"))["items"], [])

    async def test_il_filtro_attore_e_il_cursore_insieme(self):
        for i in range(6):
            await self.log.log_event("Brain" if i % 2 == 0 else "organ_x", "X", "t", "a", "b", "r")
        prima = await self.cerca(actor="brain", limite=2)
        seconda = await self.cerca(actor="brain", limite=2, cursore=prima["next_cursor"])
        ids = [d["decision_id"] for d in prima["items"] + seconda["items"]]
        self.assertEqual(len(set(ids)), 3)
        self.assertIsNone(seconda["next_cursor"])

    async def test_filtro_per_data(self):
        passato = schema.adesso_utc() - timedelta(days=3)
        async with archivio.motore(self.db).begin() as conn:
            await conn.execute(sa.insert(schema.events).values(actor="brain", action="VECCHIA", target="t", old_value="", new_value="",
                                                               reasoning="r", timestamp=passato))
        await self.log.log_event("brain", "NUOVA", "t", "a", "b", "r")
        ieri = schema.iso_ms(schema.adesso_utc() - timedelta(days=1))
        self.assertEqual([d["action"] for d in (await self.cerca(da=ieri))["items"]], ["NUOVA"])
        self.assertEqual([d["action"] for d in (await self.cerca(a=ieri))["items"]], ["VECCHIA"])
        with self.assertRaises(ValueError):
            await self.cerca(da="ieri")

    async def test_il_cursore_malformato_solleva_value_error(self):
        with self.assertRaises(ValueError):
            await self.cerca(cursore="@@@")


if __name__ == "__main__":
    unittest.main()
