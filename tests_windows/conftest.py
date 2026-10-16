"""
Configurazione dei test su Windows (python -m pytest tests_windows).

Stesse scelte di tests/conftest.py (configurazione permissiva, database SEMPRE temporaneo, chiavi API svuotate), con i
percorsi scritti in avanti (`as_posix`): nel TOML le barre rovesciate di Windows (`C:\\Users`) sono sequenze di escape
non valide. tests/ resta la suite di Linux e della CI; qui stanno i test scritti su Windows e i moduli che rieseguono
quelli di tests/ con questa configurazione.
"""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Some modules of tests/ import their neighbours as top-level modules (`from test_something import SOMETHING`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

os.environ["LANGBRAIN_CONFIG"] = str(Path(__file__).resolve().parent.parent / "tests" / "configurazione_test.toml")

_cartella_db = Path(tempfile.mkdtemp(prefix="langbrain_test_win_"))
atexit.register(shutil.rmtree, _cartella_db, ignore_errors=True)
os.environ["DB_PATH"] = str(_cartella_db / "test.db")


def _toml(percorso: Path) -> str:
    return percorso.as_posix()


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


def pytest_sessionfinish(session, exitstatus):
    pg_isolamento.pulisci()
