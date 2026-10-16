"""Conservazione dei dati: eventi a N giorni, comandi tramite la pulizia di chi li tiene, disattivata con 0."""

import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

import sqlalchemy as sa

from app.core import archivio, conservazione
from app.db import schema
from app.db.database import Database
from app.tools.nervous_system import NervousSystem


def _ambiente(**valori):
    base = {conservazione.VARIABILE_EVENTI: "", conservazione.VARIABILE_COMANDI: "", conservazione.VARIABILE_INTERVALLO: ""}
    base.update(valori)
    return mock.patch.dict(os.environ, base)


class VariabiliTest(unittest.TestCase):
    def test_predefinito_e_disattivato(self):
        with _ambiente():
            self.assertEqual((conservazione.giorni_eventi(), conservazione.giorni_comandi()), (0, 0))

    def test_valori(self):
        with _ambiente(**{conservazione.VARIABILE_EVENTI: "90", conservazione.VARIABILE_COMANDI: "7"}):
            self.assertEqual((conservazione.giorni_eventi(), conservazione.giorni_comandi()), (90, 7))

    def test_valori_non_validi_o_negativi_disattivano(self):
        with _ambiente(**{conservazione.VARIABILE_EVENTI: "tanti", conservazione.VARIABILE_COMANDI: "-3"}):
            self.assertEqual((conservazione.giorni_eventi(), conservazione.giorni_comandi()), (0, 0))

    def test_intervallo(self):
        with _ambiente():
            self.assertEqual(conservazione.intervallo_secondi(), 6 * 3600)
        with _ambiente(**{conservazione.VARIABILE_INTERVALLO: "0.5"}):
            self.assertEqual(conservazione.intervallo_secondi(), 1800)
        with _ambiente(**{conservazione.VARIABILE_INTERVALLO: "0"}):
            self.assertEqual(conservazione.intervallo_secondi(), 60)  # mai più spesso di una volta al minuto


class PulizieTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = os.path.join(tempfile.mkdtemp(prefix="lb_cons_"), "c.db")
        await Database(self.db).init_db()
        self.log = NervousSystem(db_path=self.db)
        self.addAsyncCleanup(archivio.chiudi_motori)
        self.addCleanup(lambda: [conservazione.rimuovi_pulizia_comandi(n) for n in ("comandi_x", "rotta", "ok")])

    async def evento(self, giorni_fa, target="t"):
        async with archivio.motore(self.db).begin() as conn:
            await conn.execute(sa.insert(schema.events).values(
                actor="a", action="X", target=target, old_value="", new_value="", reasoning="r",
                timestamp=schema.adesso_utc() - timedelta(days=giorni_fa)))

    async def conta(self):
        async with archivio.motore(self.db).connect() as conn:
            return (await conn.execute(sa.select(sa.func.count()).select_from(schema.events))).scalar_one()

    async def test_toglie_solo_gli_eventi_piu_vecchi_del_limite(self):
        await self.evento(100)
        await self.evento(91)
        await self.evento(89)
        await self.evento(0)
        self.assertEqual(await conservazione.pulisci_eventi(90, self.db), 2)
        self.assertEqual(await self.conta(), 2)

    async def test_zero_non_cancella_nulla(self):
        await self.evento(1000)
        self.assertEqual(await conservazione.pulisci_eventi(0, self.db), 0)
        with _ambiente():
            self.assertEqual(await conservazione.esegui_pulizie(self.db), {"stream": 0, "idempotenza": 0})
        self.assertEqual(await self.conta(), 1)

    async def test_un_giro_pulisce_eventi_e_comandi_con_i_giorni_giusti(self):
        await self.evento(200)
        await self.evento(1)
        chiamate = []

        async def comandi_x(giorni):
            chiamate.append(giorni)
            return 3

        conservazione.registra_pulizia_comandi("comandi_x", comandi_x)
        with _ambiente(**{conservazione.VARIABILE_EVENTI: "90", conservazione.VARIABILE_COMANDI: "7"}):
            tolte = await conservazione.esegui_pulizie(self.db)
        self.assertEqual(tolte, {"stream": 0, "idempotenza": 0, "eventi": 1, "comandi_x": 3})
        self.assertEqual(chiamate, [7])
        self.assertEqual(await self.conta(), 1)

    async def test_i_comandi_non_si_puliscono_se_la_variabile_e_zero(self):
        chiamate = []

        async def comandi_x(giorni):
            chiamate.append(giorni)
            return 1

        conservazione.registra_pulizia_comandi("comandi_x", comandi_x)
        with _ambiente(**{conservazione.VARIABILE_EVENTI: "90"}):
            await conservazione.esegui_pulizie(self.db)
        self.assertEqual(chiamate, [])

    async def test_una_pulizia_che_solleva_non_ferma_le_altre(self):
        async def rotta(giorni):
            raise RuntimeError("tabella assente")

        async def ok(giorni):
            return 2

        conservazione.registra_pulizia_comandi("rotta", rotta)
        conservazione.registra_pulizia_comandi("ok", ok)
        with _ambiente(**{conservazione.VARIABILE_COMANDI: "7"}):
            tolte = await conservazione.esegui_pulizie(self.db)
        self.assertEqual(tolte, {"stream": 0, "idempotenza": 0, "ok": 2})

    async def test_la_pulizia_non_tocca_le_decisioni_recenti_con_ids_collegati(self):
        recente = await self.log.log_event("a", "X", "t", "1", "2", "r", command_id="c1", correlation_id="cid")
        await self.evento(120)
        self.assertEqual(await conservazione.pulisci_eventi(90, self.db), 1)
        eventi = await self.log.get_recent_events()
        self.assertEqual([e["event_id"] for e in eventi], [recente])


if __name__ == "__main__":
    unittest.main()
