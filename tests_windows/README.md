# tests_windows

Suite di Windows: `conftest.py` proprio (percorsi con `as_posix`, `tests/` nel `sys.path`) e un modulo `test_win_<nome>.py` per ogni
modulo di `tests/`, che lo riesegue con `from tests.<nome> import *`. `tests/` è la suite di Linux e della CI; i test nuovi del nucleo
stanno in `tests/` (con il suo modulo qui), quelli scritti solo per Windows stanno qui.

    python -m pytest tests_windows -q -p no:cacheprovider

## Fallimenti noti: nessuno

La suite di Windows passa per intero. Fino al 16 ottobre 2026 aveva **11 fallimenti `PermissionError [WinError 32]`**
(`test_win_all.py::DatabaseTest` ×4, `test_win_checkpointer_persistente.py` ×1, `test_win_scenario.py` ×6): alla pulizia della cartella
temporanea il file SQLite era ancora aperto e Windows non lo lascia cancellare.

**Causa:** quei test aprivano il file con `with sqlite3.connect(percorso) as db:`. Il `with` di `sqlite3` conferma la transazione ma
**non chiude** la connessione (lo dice la documentazione di Python), quindi il file restava aperto fino alla fine del test. Il codice
dell'applicazione non c'entrava: usa SQLAlchemy/aiosqlite e chiude. Su Linux lo stesso test passava perché un file aperto si può cancellare.

**Correzione (solo nei test):** `tests/sqlite_chiusura.py` ha `connetti(percorso)`, una `sqlite3.Connection` il cui `__exit__` chiama `close()`
dopo il commit o il rollback di sempre. I test elencati sopra usano `with connetti(percorso) as db:`; `tests/test_sqlite_chiusura.py` ne prova il
comportamento (chiusura, commit, rollback, file cancellabile subito). Su Linux non cambia nulla. **In un test nuovo che apre un file SQLite
a mano usa `connetti`, non `sqlite3.connect`**, altrimenti su Windows il problema ricompare.

Il nodo premium (`langBrainInvoices`) ha lo stesso rimedio, applicato in modo globale nel proprio `tests_windows/conftest.py`
(`_ConnessioneChiusaAllUscita`): non serve toccarlo, i due si sommano senza conflitti.

## Test instabili

- `test_win_errori_llm.py::RicaricaEnvTest` rilevava il `.env` dalla data di modifica: ora il test imposta date crescenti e non dipende
  dalla risoluzione del file system.
