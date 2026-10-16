"""Controlli all'avvio: backend del database e chiave API richiesta."""

import os
import unittest
from unittest import mock

from app.core import avvio

CHIAVI = ("API_KEY", "API_KEY_PRIMARIO", "API_KEY_ADMIN", "API_KEY_MEDICO_DI_GUARDIA", "API_KEY_OPERATOR",
          "API_KEY_TIROCINANTE", "API_KEY_VIEWER")


def _ambiente(**valori):
    base = {k: "" for k in CHIAVI}
    base.update({"LANGBRAIN_REQUIRE_API_KEY": "", "LANGBRAIN_DB_BACKEND": "", "LANGBRAIN_DB_URL": ""})
    base.update(valori)
    return mock.patch.dict(os.environ, base)


class AvvioTest(unittest.TestCase):
    def test_il_predefinito_non_richiede_nulla(self):
        with _ambiente():
            avvio.verifica_avvio()

    def test_senza_chiave_con_richiesta_attiva_l_avvio_e_rifiutato(self):
        with _ambiente(LANGBRAIN_REQUIRE_API_KEY="1"):
            with self.assertRaisesRegex(avvio.ErroreAvvio, "nessuna chiave API"):
                avvio.verifica_avvio()

    def test_con_la_chiave_e_la_richiesta_attiva_si_parte(self):
        with _ambiente(LANGBRAIN_REQUIRE_API_KEY="1", API_KEY="x" * 20):
            avvio.verifica_avvio()

    def test_senza_richiesta_la_chiave_resta_facoltativa(self):
        with _ambiente(LANGBRAIN_REQUIRE_API_KEY="0"):
            avvio.verifica_avvio()

    def test_postgres_senza_url_e_rifiutato(self):
        with _ambiente(LANGBRAIN_DB_BACKEND="postgres"):
            with self.assertRaisesRegex(avvio.ErroreAvvio, "LANGBRAIN_DB_URL"):
                avvio.verifica_avvio()

    def test_il_server_non_parte_se_l_avvio_e_rifiutato(self):
        from app import serve
        with _ambiente(LANGBRAIN_REQUIRE_API_KEY="1"):
            with self.assertRaisesRegex(SystemExit, "Avvio rifiutato"):
                serve.main(["--port", "0"])

    def test_il_lifespan_rifiuta_l_avvio(self):
        import asyncio
        from app.api.main import app, lifespan

        async def prova():
            async with lifespan(app):
                pass

        with _ambiente(LANGBRAIN_REQUIRE_API_KEY="1"):
            with self.assertRaises(avvio.ErroreAvvio):
                asyncio.run(prova())


if __name__ == "__main__":
    unittest.main()
