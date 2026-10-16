"""Correlation id, attore e registro delle decisioni: contesto della richiesta, colonne nuove, timestamp al millisecondo."""

import asyncio
import os
import re
import tempfile
import unittest
import uuid

import sqlalchemy as sa
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.core import archivio, contesto
from app.db.database import Database
from app.tools.nervous_system import NervousSystem

ISO_MS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


class MappaAttoreTest(unittest.TestCase):
    def test_email_diventa_user(self):
        self.assertEqual(contesto.mappa_attore("anna@example.com"), "user:anna@example.com")

    def test_system_con_nome_diventa_system(self):
        self.assertEqual(contesto.mappa_attore("system:scheduler"), "system")
        self.assertEqual(contesto.mappa_attore("system"), "system")

    def test_gli_attori_interni_restano_come_sono(self):
        self.assertEqual(contesto.mappa_attore("rule:window_open_off"), "rule:window_open_off")
        self.assertEqual(contesto.mappa_attore("organ:clima"), "organ:clima")
        self.assertEqual(contesto.mappa_attore("brain"), "brain")
        self.assertEqual(contesto.mappa_attore("assistant"), "assistant")
        self.assertEqual(contesto.mappa_attore("user:anna@example.com"), "user:anna@example.com")

    def test_valori_non_riconoscibili_sono_ignorati(self):
        for valore in (None, "", "   ", "pinco pallino", "rule:", "ftp://x"):
            self.assertIsNone(contesto.mappa_attore(valore), valore)

    def test_correlation_id_ricevuto_o_scartato(self):
        self.assertEqual(contesto.pulisci_correlation_id(" abc-123 "), "abc-123")
        self.assertIsNone(contesto.pulisci_correlation_id("x" * 200))
        self.assertIsNone(contesto.pulisci_correlation_id("a\nb"))
        self.assertIsNone(contesto.pulisci_correlation_id("  "))


class MiddlewareTest(unittest.TestCase):
    def setUp(self):
        async def leggi(request):
            return JSONResponse({"correlation_id": contesto.correlation_id_corrente(), "attore": contesto.attore_corrente()})

        app = Starlette(routes=[Route("/x", leggi)])
        app.add_middleware(contesto.MiddlewareContesto)
        self.client = TestClient(app)

    def test_l_header_ricevuto_si_usa_cosi_com_e_e_si_restituisce(self):
        cid = str(uuid.uuid4())
        r = self.client.get("/x", headers={"X-Correlation-Id": cid, "X-Actor": "anna@example.com"})
        self.assertEqual(r.json(), {"correlation_id": cid, "attore": "user:anna@example.com"})
        self.assertEqual(r.headers["X-Correlation-Id"], cid)

    def test_senza_header_si_genera_un_uuid4(self):
        r = self.client.get("/x")
        cid = r.json()["correlation_id"]
        self.assertEqual(str(uuid.UUID(cid, version=4)), cid)
        self.assertEqual(r.headers["X-Correlation-Id"], cid)
        self.assertIsNone(r.json()["attore"])

    def test_due_richieste_non_condividono_il_contesto(self):
        a = self.client.get("/x").json()["correlation_id"]
        b = self.client.get("/x").json()["correlation_id"]
        self.assertNotEqual(a, b)
        self.assertIsNone(contesto.correlation_id_corrente())

    def test_un_correlation_id_non_valido_viene_sostituito(self):
        r = self.client.get("/x", headers={"X-Correlation-Id": "x" * 300})
        self.assertNotEqual(r.json()["correlation_id"], "x" * 300)


class ContestoTest(unittest.TestCase):
    def test_fatto_interno_ha_un_id_proprio_e_ripristina(self):
        with contesto.contesto("esterno", "user:a@b.it"):
            with contesto.fatto_interno() as interno:
                self.assertNotEqual(interno, "esterno")
                self.assertEqual(contesto.correlation_id_corrente(), interno)
                self.assertEqual(contesto.attore_corrente(), "system")
            self.assertEqual(contesto.correlation_id_corrente(), "esterno")
            self.assertEqual(contesto.attore_corrente(), "user:a@b.it")

    def test_il_contesto_attraversa_i_task(self):
        async def prova():
            with contesto.contesto("cid-1", "brain"):
                return await asyncio.create_task(_leggi())

        async def _leggi():
            await asyncio.sleep(0)
            return contesto.correlation_id_corrente(), contesto.attore_corrente()

        self.assertEqual(asyncio.run(prova()), ("cid-1", "brain"))


class RegistroDecisioniTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cartella = tempfile.mkdtemp(prefix="lb_ctx_")
        self.db = os.path.join(self.cartella, "t.db")
        await Database(self.db).init_db()
        self.log = NervousSystem(db_path=self.db)
        self.addAsyncCleanup(archivio.chiudi_motori)

    async def _riga(self, evento_id):
        async with archivio.motore(self.db).connect() as db:
            return (await db.execute(sa.text("SELECT * FROM events WHERE event_id = :i"), {"i": evento_id})).mappings().one()

    async def test_log_event_restituisce_l_id_e_riempie_i_campi_nuovi(self):
        evento = await self.log.log_event("organ_x", "TURN_ON", "lampada", "OFF", "ON", "motivo")
        riga = await self._riga(evento)
        self.assertEqual(riga["actor"], "organ_x")
        self.assertEqual(riga["outcome"], "applied")
        self.assertEqual(str(uuid.UUID(riga["correlation_id"], version=4)), riga["correlation_id"])  # fatto interno: id nuovo
        self.assertIsNone(riga["command_id"])
        self.assertIsNone(riga["approval_id"])

    async def test_attore_e_correlation_dal_contesto(self):
        with contesto.contesto("cid-xyz", "user:anna@example.com"):
            evento = await self.log.log_event(None, "SET_TEMP", "clima", "20", "22", "richiesta")
        riga = await self._riga(evento)
        self.assertEqual((riga["actor"], riga["correlation_id"]), ("user:anna@example.com", "cid-xyz"))

    async def test_un_attore_esplicito_prevale_sul_contesto(self):
        with contesto.contesto("cid-1", "user:anna@example.com"):
            evento = await self.log.log_event("rule:window_open_off", "TURN_OFF", "clima", "ON", "OFF", "finestra aperta")
        self.assertEqual((await self._riga(evento))["actor"], "rule:window_open_off")

    async def test_senza_attore_ne_contesto_e_system(self):
        evento = await self.log.log_event(None, "X", "t", "a", "b", "r")
        self.assertEqual((await self._riga(evento))["actor"], "system")

    async def test_outcome_esplicito_ids_collegati_e_validazione(self):
        evento = await self.log.log_event("brain", "TURN_ON", "luce", "OFF", "ON", "r", outcome="pending_approval",
                                          command_id="cmd-1", approval_id="app-1", correlation_id="c-9")
        riga = await self._riga(evento)
        self.assertEqual((riga["outcome"], riga["command_id"], riga["approval_id"], riga["correlation_id"]),
                         ("pending_approval", "cmd-1", "app-1", "c-9"))
        with self.assertRaisesRegex(ValueError, "non valido"):
            await self.log.log_event("brain", "X", "t", "a", "b", "r", outcome="boh")

    async def test_outcome_dedotto_dall_azione(self):
        casi = {"REJECTED_TURN_ON": "rejected", "TOOL_ERROR_TURN_ON": "failed", "RESOLVED_X": "informational",
                "ESCALATION_PROPOSED": "informational", "TURN_ON": "applied"}
        for azione, atteso in casi.items():
            evento = await self.log.log_event("a", azione, "t", "x", "y", "r")
            self.assertEqual((await self._riga(evento))["outcome"], atteso, azione)

    async def test_timestamp_iso_al_millisecondo_e_ordine_per_id(self):
        ids = [await self.log.log_event("a", "X", "ordine", "1", str(i), "r") for i in range(5)]
        self.assertEqual(ids, sorted(ids))
        eventi = await self.log.get_recent_events()
        self.assertEqual([e["event_id"] for e in eventi], sorted(ids, reverse=True))  # a pari millisecondo decide l'id
        for e in eventi:
            self.assertRegex(e["ts"], ISO_MS)
            self.assertRegex(e["timestamp"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")  # formato storico invariato

    async def test_unblock_riporta_il_correlation_id(self):
        with contesto.contesto("cid-unblock", None):
            await self.log.unblock_target("porta", "ok")
        eventi = [e for e in await self.log.get_recent_events() if e["action"] == "UNBLOCKED"]
        self.assertEqual((eventi[0]["correlation_id"], eventi[0]["outcome"]), ("cid-unblock", "informational"))


class MigrazioneTest(unittest.IsolatedAsyncioTestCase):
    async def test_un_database_vecchio_riceve_colonne_e_indice(self):
        cartella = tempfile.mkdtemp(prefix="lb_mig_")
        db = os.path.join(cartella, "vecchio.db")
        async with archivio.motore(db).begin() as conn:
            await conn.execute(sa.text(
                "CREATE TABLE events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, "
                "target TEXT NOT NULL, old_value TEXT, new_value TEXT, reasoning TEXT, "
                "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, escalated INTEGER DEFAULT 0)"))
            await conn.execute(sa.text("INSERT INTO events (actor, action, target) VALUES ('vecchio', 'X', 't')"))
        self.addAsyncCleanup(archivio.chiudi_motori)
        await Database(db).init_db()
        await Database(db).init_db()  # idempotente
        async with archivio.motore(db).connect() as conn:
            colonne = {r[1] for r in await conn.execute(sa.text("PRAGMA table_info(events)"))}
            indici = {r[1] for r in await conn.execute(sa.text("PRAGMA index_list(events)"))}
            vecchia = (await conn.execute(sa.text("SELECT actor, correlation_id, outcome FROM events"))).one()
        self.assertTrue({"correlation_id", "outcome", "command_id", "approval_id"} <= colonne)
        self.assertIn("idx_events_correlation", indici)
        self.assertEqual(tuple(vecchia), ("vecchio", None, None))  # nessun dato perso né inventato


if __name__ == "__main__":
    unittest.main()
