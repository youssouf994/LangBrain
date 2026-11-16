"""Stato fisico persistente e riconciliazione dopo il riavvio (`app/core/stato_fisico.py`, `app/core/riconciliazione.py`)."""

import asyncio
import os
import tempfile
import unittest
from typing import Any

from app.core import archivio, riconciliazione, stato_fisico
from app.core.configurazione import costruisci_configurazione, imposta_configurazione
from app.core.priorita import trova_blocco_prevalente
from app.core.risultati import ErroreTool, comanda_tool
from app.db.database import DB_PATH, Database
from app.tools.effectors import SimulatedEffector
from app.tools.nervous_system import NervousSystem
from app.tools.reflex import Reflex


class DispositivoVero(Reflex):
    """Un dispositivo che ricorda il proprio valore (come un hardware): ciò che si cambia 'a mano' lo cambia qui."""
    simulato = False

    def __init__(self, nome: str, valore: Any = "LOCKED", unit: str = ""):
        super().__init__(nome)
        self.valore, self.unit, self.giu = valore, unit, False

    async def get_tool_value(self):
        if self.giu:
            raise ErroreTool("non risponde", codice="DEVICE_UNREACHABLE")
        return self.valore

    async def set_tool_value(self, value):
        if self.giu:
            raise ErroreTool("non risponde", codice="DEVICE_UNREACHABLE")
        self.valore = value
        return True


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Il database predefinito dell'applicazione (temporaneo nei test): è quello che usano `comanda_tool` e la riconciliazione.
        self.db = DB_PATH
        await Database(self.db).init_db()
        self.addAsyncCleanup(archivio.chiudi_motori)
        imposta_configurazione(costruisci_configurazione({}))
        self.addCleanup(imposta_configurazione, None)
        stato_fisico.dimentica_cache()
        riconciliazione._irraggiungibili.clear()
        self.addCleanup(stato_fisico.dimentica_cache)
        self.addAsyncCleanup(self.ripulisci_database_condiviso)

    async def ripulisci_database_condiviso(self):
        """Questi test scrivono nel database predefinito (come `comanda_tool`): non devono lasciarvi eventi o stati ai test successivi."""
        from sqlalchemy import delete
        from app.db import schema
        async with archivio.motore(self.db).begin() as db:
            await db.execute(delete(schema.device_state))
            await db.execute(delete(schema.events).where(schema.events.c.action.like("RECONCILED\_%", escape="\\")))

    async def salva(self, dispositivo, valore, source=stato_fisico.COMANDATO, unita=""):
        await stato_fisico.salva(dispositivo, valore, unita, source, percorso=self.db)

    async def eventi(self, azione, dispositivo):
        righe = await NervousSystem(target=[dispositivo], frequency=60, db_path=self.db).get_recent_events()
        return [e for e in righe if e["action"] == azione]


class StatoFisicoTest(Base):
    async def test_salva_e_rilegge_mantenendo_il_tipo(self):
        await self.salva("termostato", 21.5, unita="°C")
        await self.salva("luce", "ON", stato_fisico.OSSERVATO)
        noto = await stato_fisico.leggi("termostato", self.db)
        self.assertEqual((noto["value"], noto["unit"], noto["source"]), (21.5, "°C", "commanded"))
        self.assertEqual((await stato_fisico.leggi("luce", self.db))["source"], "observed")
        self.assertTrue({"termostato", "luce"} <= set(await stato_fisico.leggi_tutti(self.db)))
        self.assertIsNone(await stato_fisico.leggi("mai_visto", self.db))

    async def test_aggiorna_la_riga_non_riscrive_le_osservazioni_uguali_e_scrive_sempre_i_comandi(self):
        await self.salva("luce_o", "ON", stato_fisico.OSSERVATO)
        primo = (await stato_fisico.leggi("luce_o", self.db))["updated_at"]
        await asyncio.sleep(0.01)
        await self.salva("luce_o", "ON", stato_fisico.OSSERVATO)
        self.assertEqual((await stato_fisico.leggi("luce_o", self.db))["updated_at"], primo)
        await self.salva("luce_o", "OFF", stato_fisico.OSSERVATO)
        self.assertEqual((await stato_fisico.leggi("luce_o", self.db))["value"], "OFF")
        # un altro processo ha cambiato la riga: il comando di questo, anche se uguale all'ultimo scritto qui, la riporta com'era
        await self.salva("luce_c", "OFF")
        stato_fisico._ultimo_scritto.clear()
        await self.salva("luce_c", "ON")
        await self.salva("luce_c", "OFF")
        self.assertEqual((await stato_fisico.leggi("luce_c", self.db))["value"], "OFF")

    async def test_persistenza_spenta_non_scrive_ne_legge(self):
        imposta_configurazione(costruisci_configurazione({"stato_fisico": {"persistenza": 0}}))
        await self.salva("luce", "ON")
        self.assertIsNone(await stato_fisico.leggi("luce", self.db))
        self.assertEqual(await riconciliazione.riconcilia({"luce": DispositivoVero("luce")}, avvio=True), [])

    async def test_un_errore_del_database_non_rompe_il_comando(self):
        await stato_fisico.salva("x", 1, "", "commanded", percorso=os.path.join(tempfile.gettempdir(), "nessuna", "cartella", "x.db"))

    async def test_valori_equivalenti(self):
        eq = stato_fisico.valori_equivalenti
        self.assertTrue(eq(22, "22.0") and eq("22°C", 22, "°C") and eq("on", "ON") and eq("21,5", 21.5))
        self.assertFalse(eq("ON", "OFF") or eq(22, 23) or eq(None, "x"))

    async def test_il_comando_riuscito_viene_salvato_quello_fallito_no(self):
        dispositivo = DispositivoVero("porta_cmd")
        await comanda_tool(dispositivo, "porta_cmd", "UNLOCKED")
        self.assertEqual((await stato_fisico.leggi("porta_cmd"))["value"], "UNLOCKED")
        dispositivo.giu = True
        await comanda_tool(dispositivo, "porta_cmd", "LOCKED")
        self.assertEqual((await stato_fisico.leggi("porta_cmd"))["value"], "UNLOCKED")   # il comando fallito non cambia lo stato noto


class RiconciliazioneTest(Base):
    async def riconcilia(self, strumenti, avvio=False):
        return {r["device"]: r for r in await riconciliazione.riconcilia(strumenti, avvio=avvio)}

    async def test_all_avvio_il_dispositivo_simulato_ritrova_l_ultimo_valore_comandato(self):
        # Il database predefinito dell'applicazione è quello dei test: si salva lì, come fa `comanda_tool`.
        await stato_fisico.salva("luce_sim", "ON", "", stato_fisico.COMANDATO)
        luce = SimulatedEffector("luce_sim", "OFF")
        esito = (await self.riconcilia({"luce_sim": luce}, avvio=True))["luce_sim"]
        self.assertEqual((esito["outcome"], await luce.get_tool_value()), (riconciliazione.RIPRISTINATO, "ON"))

    async def test_il_simulato_non_si_tocca_nel_giro_periodico_e_senza_storia_resta_com_e(self):
        await stato_fisico.salva("luce_sim2", "ON", "", stato_fisico.COMANDATO)
        luce, nuova = SimulatedEffector("luce_sim2", "OFF"), SimulatedEffector("mai_comandata", "OFF")
        self.assertEqual(await self.riconcilia({"luce_sim2": luce}, avvio=False), {})
        esito = (await self.riconcilia({"mai_comandata": nuova}, avvio=True))["mai_comandata"]
        self.assertEqual(esito["outcome"], riconciliazione.ALLINEATO)
        self.assertEqual(await nuova.get_tool_value(), "OFF")

    async def test_dispositivo_vero_allineato(self):
        await stato_fisico.salva("porta_a", "LOCKED", "", stato_fisico.COMANDATO)
        esito = (await self.riconcilia({"porta_a": DispositivoVero("porta_a", "locked")}))["porta_a"]
        self.assertEqual(esito["outcome"], riconciliazione.ALLINEATO)
        self.assertEqual(await self.eventi("RECONCILED_DRIFT", "porta_a"), [])

    async def test_deriva_dopo_un_intervento_a_mano_registra_un_evento_e_non_comanda(self):
        await stato_fisico.salva("porta_b", "LOCKED", "", stato_fisico.COMANDATO)
        porta = DispositivoVero("porta_b", "UNLOCKED")   # qualcuno l'ha aperta mentre il sistema era spento
        esito = (await self.riconcilia({"porta_b": porta}, avvio=True))["porta_b"]
        self.assertEqual((esito["outcome"], esito["commanded"], esito["value"]), (riconciliazione.DERIVA, "LOCKED", "UNLOCKED"))
        self.assertEqual(porta.valore, "UNLOCKED")   # nessun comando automatico
        eventi = await self.eventi("RECONCILED_DRIFT", "porta_b")
        self.assertEqual(len(eventi), 1)
        self.assertEqual((eventi[0]["old_value"], eventi[0]["new_value"], eventi[0]["outcome"]), ("LOCKED", "UNLOCKED", "informational"))
        self.assertEqual((await stato_fisico.leggi("porta_b"))["source"], "observed")
        # il giro dopo non ripete l'evento: ora lo stato noto è quello letto
        await self.riconcilia({"porta_b": porta})
        self.assertEqual(len(await self.eventi("RECONCILED_DRIFT", "porta_b")), 1)

    async def test_un_evento_di_deriva_non_blocca_nessuno(self):
        await stato_fisico.salva("allarme_c", "ARMED", "", stato_fisico.COMANDATO)
        await self.riconcilia({"allarme_c": DispositivoVero("allarme_c", "OFF")})
        eventi = await NervousSystem(target=["all"], frequency=60).get_recent_events()
        self.assertTrue(eventi)
        self.assertIsNone(trova_blocco_prevalente(eventi, "allarme_c", "agente_x", 1.0, 60))

    async def test_un_valore_osservato_che_cambia_da_solo_non_e_una_deriva(self):
        await stato_fisico.salva("sensore_d", 20.0, "", stato_fisico.OSSERVATO)
        esito = (await self.riconcilia({"sensore_d": DispositivoVero("sensore_d", 23.5)}))["sensore_d"]
        self.assertEqual(esito["outcome"], riconciliazione.ALLINEATO)
        self.assertEqual(await self.eventi("RECONCILED_DRIFT", "sensore_d"), [])
        self.assertEqual((await stato_fisico.leggi("sensore_d"))["value"], 23.5)

    async def test_dispositivo_irraggiungibile_si_segnala_una_volta_per_interruzione(self):
        await stato_fisico.salva("porta_e", "LOCKED", "", stato_fisico.COMANDATO)
        porta = DispositivoVero("porta_e")
        porta.giu = True
        for _ in range(3):
            esito = (await self.riconcilia({"porta_e": porta}))["porta_e"]
            self.assertEqual((esito["outcome"], esito["error_type"]), (riconciliazione.IRRAGGIUNGIBILE, "DEVICE_UNREACHABLE"))
        eventi = await self.eventi("RECONCILED_UNREACHABLE", "porta_e")
        self.assertEqual((len(eventi), eventi[0]["outcome"], eventi[0]["old_value"]), (1, "failed", "LOCKED"))
        self.assertEqual((await stato_fisico.leggi("porta_e"))["value"], "LOCKED")   # lo stato noto non si perde
        porta.giu = False   # torna: la prossima interruzione si segnalerà di nuovo
        await self.riconcilia({"porta_e": porta})
        porta.giu = True
        await self.riconcilia({"porta_e": porta})
        self.assertEqual(len(await self.eventi("RECONCILED_UNREACHABLE", "porta_e")), 2)

    async def test_un_dispositivo_in_errore_non_ferma_gli_altri(self):
        class Rotto(DispositivoVero):
            async def get_tool_value(self):
                raise RuntimeError("bug del driver")

        await stato_fisico.salva("ok_f", "ON", "", stato_fisico.COMANDATO)
        esiti = await self.riconcilia({"a_rotto": Rotto("a_rotto"), "ok_f": DispositivoVero("ok_f", "ON")})
        self.assertEqual((esiti["a_rotto"]["outcome"], esiti["ok_f"]["outcome"]),
                         (riconciliazione.IRRAGGIUNGIBILE, riconciliazione.ALLINEATO))

    async def test_le_riconciliazioni_sono_contate(self):
        from app.core import metriche
        metriche.azzera()
        await self.riconcilia({"x_g": DispositivoVero("x_g")})
        self.assertIn('langbrain_device_reconciliations_total{outcome="allineato"} 1', await metriche.esporta())


if __name__ == "__main__":
    unittest.main()
