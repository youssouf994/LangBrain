"""Idempotenza: stessa chiave = stessa risposta e una sola esecuzione (60 s per i comandi, per sempre per le prenotazioni)."""

import asyncio
import os
import tempfile
import unittest
import uuid

import sqlalchemy as sa

from app.core import archivio, comandi, contesto, idempotenza
from app.db import schema
from app.db.database import Database
from app.tools.effectors import SimulatedEffector, registra_tool
from app.tools.nervous_system import NervousSystem


class BaseDbTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = os.path.join(tempfile.mkdtemp(prefix="lb_idem_"), "i.db")
        await Database(self.db).init_db()
        self.addAsyncCleanup(archivio.chiudi_motori)

    async def una_volta(self, ambito, chiave, funzione, ttl=60.0):
        return await idempotenza.esegui_una_volta(ambito, chiave, funzione, ttl, self.db)

    async def scadi(self, ambito, chiave):
        async with archivio.motore(self.db).begin() as conn:
            await conn.execute(sa.update(schema.idempotenza).where(
                schema.idempotenza.c.ambito == ambito, schema.idempotenza.c.chiave == chiave).values(scade_il=1.0))


class EsegUnaVoltaTest(BaseDbTest):
    async def test_la_seconda_chiamata_riprende_il_risultato_senza_eseguire(self):
        chiamate = []

        async def lavoro():
            chiamate.append(1)
            return {"command_id": "cmd-1", "n": len(chiamate)}

        primo = await self.una_volta("POST /commands", "cid-1", lavoro)
        secondo = await self.una_volta("POST /commands", "cid-1", lavoro)
        self.assertEqual(primo, ({"command_id": "cmd-1", "n": 1}, False))
        self.assertEqual(secondo, ({"command_id": "cmd-1", "n": 1}, True))
        self.assertEqual(len(chiamate), 1)

    async def test_richieste_concorrenti_identiche_eseguono_una_volta_e_rispondono_uguale(self):
        chiamate = []

        async def lavoro():
            chiamate.append(1)
            await asyncio.sleep(0.3)  # lenta: le altre arrivano mentre è in corso
            return {"id": uuid.uuid4().hex}

        risultati = await asyncio.gather(*[self.una_volta("A", "stessa", lavoro) for _ in range(6)])
        self.assertEqual(len(chiamate), 1)
        self.assertEqual(len({r[0]["id"] for r in risultati}), 1)
        self.assertEqual(sorted(r[1] for r in risultati), [False] + [True] * 5)

    async def test_ambito_o_chiave_diversi_sono_indipendenti(self):
        async def lavoro():
            return {"id": uuid.uuid4().hex}

        a = await self.una_volta("POST /devices/luce/commands", "cid", lavoro)
        altro_endpoint = await self.una_volta("POST /devices/clima/commands", "cid", lavoro)
        altra_chiave = await self.una_volta("POST /devices/luce/commands", "cid-2", lavoro)
        self.assertEqual(len({a[0]["id"], altro_endpoint[0]["id"], altra_chiave[0]["id"]}), 3)
        self.assertEqual((a[1], altro_endpoint[1], altra_chiave[1]), (False, False, False))

    async def test_dopo_la_scadenza_la_stessa_chiave_esegue_di_nuovo(self):
        contatore = []

        async def lavoro():
            contatore.append(1)
            return {"n": len(contatore)}

        await self.una_volta("A", "k", lavoro, ttl=60)
        await self.scadi("A", "k")  # i 60 s sono passati
        secondo = await self.una_volta("A", "k", lavoro, ttl=60)
        self.assertEqual(secondo, ({"n": 2}, False))

    async def test_senza_limite_di_tempo_non_scade_mai(self):
        async def lavoro():
            return {"schedule_ids": ["s1", "s2"]}

        await self.una_volta("POST /bookings", "evt-1", lavoro, ttl=None)
        async with archivio.motore(self.db).connect() as conn:
            scade = (await conn.execute(sa.select(schema.idempotenza.c.scade_il))).scalar_one()
        self.assertIsNone(scade)
        self.assertEqual(await idempotenza.pulisci_scadute(self.db), 0)
        again = await self.una_volta("POST /bookings", "evt-1", lavoro, ttl=None)
        self.assertEqual(again, ({"schedule_ids": ["s1", "s2"]}, True))

    async def test_se_la_funzione_solleva_non_si_memorizza_nulla(self):
        tentativi = []

        async def lavoro():
            tentativi.append(1)
            if len(tentativi) == 1:
                raise RuntimeError("guasto")
            return {"ok": True}

        with self.assertRaises(RuntimeError):
            await self.una_volta("A", "k", lavoro)
        self.assertEqual(await self.una_volta("A", "k", lavoro), ({"ok": True}, False))  # riprova: esegue di nuovo

    async def test_una_esecuzione_abbandonata_viene_rilevata(self):
        async with archivio.motore(self.db).begin() as conn:  # un processo morto a metà: in corso, con la scadenza passata
            await conn.execute(sa.insert(schema.idempotenza).values(
                ambito="A", chiave="k", stato="in_corso", risposta=None, creata_il=1.0, scade_il=2.0))

        async def lavoro():
            return {"ok": 1}

        self.assertEqual(await self.una_volta("A", "k", lavoro), ({"ok": 1}, False))

    async def test_pulisci_scadute_toglie_solo_quelle_scadute(self):
        async def lavoro():
            return {}

        await self.una_volta("A", "vecchia", lavoro)
        await self.una_volta("A", "recente", lavoro)
        await self.scadi("A", "vecchia")
        self.assertEqual(await idempotenza.pulisci_scadute(self.db), 1)
        self.assertEqual((await self.una_volta("A", "recente", lavoro))[1], True)


class ComandoIdempotenteTest(BaseDbTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.target = f"luce_{uuid.uuid4().hex[:6]}"
        self.tool = SimulatedEffector(self.target, "OFF")
        self.azionamenti = 0
        originale = self.tool.set_tool_value

        async def conta(valore):
            self.azionamenti += 1
            return await originale(valore)

        self.tool.set_tool_value = conta
        registra_tool(self.target, self.tool)

    async def comando(self, cid="cid-c02", secondi=60):
        return await comandi.tenta_comando("user:anna@example.com", self.target, "ON", cid, command_id="cmd-7",
                                           db_path=self.db, idempotenza_secondi=secondi)

    async def test_c02_due_richieste_identiche_stesso_esito_una_esecuzione_una_decision(self):
        primo = await self.comando()
        secondo = await self.comando()
        self.assertEqual((primo.riutilizzato, secondo.riutilizzato), (False, True))
        self.assertEqual((secondo.stato, secondo.decision_id, secondo.correlation_id), (primo.stato, primo.decision_id, "cid-c02"))
        self.assertEqual(self.azionamenti, 1)
        eventi = [e for e in await NervousSystem(db_path=self.db).get_recent_events() if e["target"] == self.target]
        self.assertEqual(len(eventi), 1)

    async def test_richieste_concorrenti_identiche_azionano_una_volta(self):
        esiti = await asyncio.gather(*[self.comando("cid-conc") for _ in range(5)])
        self.assertEqual(self.azionamenti, 1)
        self.assertEqual(len({e.decision_id for e in esiti}), 1)
        self.assertEqual(sum(e.riutilizzato for e in esiti), 4)

    async def test_un_blocco_ripetuto_resta_un_blocco_con_una_sola_decision(self):
        comandi.registra_regola("sempre_no", lambda r: "Finestra aperta.")
        self.addCleanup(comandi.rimuovi_regola, "sempre_no")
        a, b = await self.comando("cid-blocco"), await self.comando("cid-blocco")
        self.assertEqual((a.stato, b.stato, b.riutilizzato, a.decision_id == b.decision_id), ("bloccato_da_regola",) * 2 + (True, True))

    async def test_correlation_id_diversi_sono_comandi_diversi(self):
        await self.comando("cid-a")
        await self.comando("cid-b")
        self.assertEqual(self.azionamenti, 2)

    async def test_dopo_la_scadenza_il_comando_si_esegue_di_nuovo(self):
        await self.comando("cid-s")
        await self.scadi(f"comando:{self.target}", "cid-s")
        nuovo = await self.comando("cid-s")
        self.assertFalse(nuovo.riutilizzato)
        self.assertEqual(self.azionamenti, 2)

    async def test_senza_idempotenza_o_senza_correlation_id_ogni_chiamata_esegue(self):
        for _ in range(2):
            await comandi.tenta_comando("user:a@b.it", self.target, "ON", "cid-x", db_path=self.db)
        for _ in range(2):
            await comandi.tenta_comando("user:a@b.it", self.target, "ON", None, db_path=self.db, idempotenza_secondi=60)
        self.assertEqual(self.azionamenti, 4)

    async def test_il_correlation_id_puo_venire_dal_contesto(self):
        with contesto.contesto("cid-ctx-idem", None):
            a = await comandi.tenta_comando("user:a@b.it", self.target, "ON", db_path=self.db, idempotenza_secondi=60)
            b = await comandi.tenta_comando("user:a@b.it", self.target, "ON", db_path=self.db, idempotenza_secondi=60)
        self.assertEqual((a.riutilizzato, b.riutilizzato, self.azionamenti), (False, True, 1))


if __name__ == "__main__":
    unittest.main()
