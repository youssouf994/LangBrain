"""Riesegue tests/test_errori_llm.py con la configurazione di Windows (tests_windows/conftest.py)."""

import os

from tests.test_errori_llm import *  # noqa: F401,F403


class RicaricaEnvTest(RicaricaEnvTest):  # noqa: F405
    """
    Come in tests/, ma ogni scrittura del .env riceve una data di modifica diversa dalla precedente. Su Windows l'orologio
    di sistema avanza a scatti di ~15 ms: due scritture ravvicinate (setUp e test) avevano la stessa data e la modifica
    non veniva rilevata, quindi il test falliva a caso.
    """
    _scritture = 0

    def scrivi(self, contenuto):
        with open(self.file_env, "w") as f:
            f.write(contenuto)
        RicaricaEnvTest._scritture += 1
        stat = os.stat(self.file_env)
        os.utime(self.file_env, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000 + RicaricaEnvTest._scritture * 1_000_000))
