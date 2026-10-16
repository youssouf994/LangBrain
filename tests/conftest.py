"""
Configurazione condivisa dei test.

- I test usano una configurazione permissiva (dispositivi non elencati consentiti) così i dispositivi finti dei test
  non devono comparire in configurazione.toml. I test che verificano l'elenco esplicito dei dispositivi usano
  `imposta_configurazione` con un oggetto proprio.
- Il database è SEMPRE temporaneo: un test che per errore raggiungesse un gestore reale (es. `DELETE /system/reset`)
  non deve poter toccare `langbrain.db` o il database indicato nel `.env`. Le variabili vanno impostate prima che
  i moduli dell'applicazione vengano importati, perciò stanno qui, a livello di modulo.
- Le chiavi API sono svuotate: i test non devono dipendere da quelle che uno sviluppatore ha nel proprio `.env`
  (`load_dotenv` non sovrascrive le variabili già presenti, nemmeno se vuote). Ogni test imposta le chiavi che gli servono.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

os.environ["LANGBRAIN_CONFIG"] = str(Path(__file__).with_name("configurazione_test.toml"))

_cartella_db = tempfile.mkdtemp(prefix="langbrain_test_")
atexit.register(shutil.rmtree, _cartella_db, ignore_errors=True)
os.environ["DB_PATH"] = os.path.join(_cartella_db, "test.db")

for _variabile in ("API_KEY", "API_KEY_TIROCINANTE", "API_KEY_MEDICO_DI_GUARDIA", "API_KEY_PRIMARIO",
                   "API_KEY_VIEWER", "API_KEY_OPERATOR", "API_KEY_ADMIN"):
    os.environ[_variabile] = ""


# Modalità Postgres (TEST_PG_URL): ogni database temporaneo diventa uno schema dello stesso Postgres di prova.
try:
    from tests import pg_isolamento
except ImportError:  # `tests/` non è un pacchetto importabile in questa modalità di lancio
    import pg_isolamento

pg_isolamento.installa()


def pytest_collection_modifyitems(items):
    pg_isolamento.segna_test_non_adatti(items)


def pytest_runtest_setup(item):
    pg_isolamento.prima_di_ogni_test()


def pytest_sessionfinish(session, exitstatus):
    pg_isolamento.pulisci()
