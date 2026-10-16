# tests_windows

Suite di Windows: `conftest.py` proprio (percorsi con `as_posix`, `tests/` nel `sys.path`) e un modulo `test_win_<nome>.py` per ogni
modulo di `tests/`, che lo riesegue con `from tests.<nome> import *`. `tests/` è la suite di Linux e della CI; i test nuovi del nucleo
stanno in `tests/` (con il suo modulo qui), quelli scritti solo per Windows stanno qui.

    python -m pytest tests_windows -q -p no:cacheprovider

## Fallimenti noti: 11 × `PermissionError [WinError 32]`

Su Windows la suite ha **11 fallimenti noti**, tutti per lo stesso motivo: alla pulizia della cartella temporanea il file SQLite è
ancora aperto e Windows non lo lascia cancellare (`WinError 32`). Su Linux lo stesso test passa perché un file aperto si può cancellare.

| Modulo | Test |
|---|---|
| `test_win_all.py::DatabaseTest` | `test_il_conflitto_dimostrativo_si_semina_solo_se_richiesto_e_non_si_accumula`, `test_init_db_crea_le_tabelle_e_l_indice`, `test_init_db_e_idempotente_e_non_semina_dati_di_prova_per_default`, `test_un_database_con_lo_schema_vecchio_viene_migrato` |
| `test_win_checkpointer_persistente.py::CheckpointerPersistenteTest` | `test_tabelle_create_nello_stesso_file_del_db_applicativo` |
| `test_win_scenario.py::ScenarioTest` | `test_azzera_database_elimina_le_tabelle_e_tollera_quelle_mancanti`, `test_lo_scenario_base_crea_la_gerarchia_e_lo_storico`, `test_lo_scenario_vuoto_azzera_tutto_senza_nemmeno_il_seme`, `test_ricreare_lo_scenario_e_idempotente_e_cancella_i_dati_precedenti`, `test_un_database_con_schema_vecchio_viene_ricreato_da_capo` |
| `test_win_scenario.py::ScriptScenarioTest` | `test_lo_script_crea_lo_scenario_e_fa_il_backup_di_un_database_esistente` |

**Causa (verificata):** quei test aprono il file con `with sqlite3.connect(percorso) as db:`. Il `with` di `sqlite3` conferma la
transazione ma **non chiude** la connessione (lo dice la documentazione di Python), quindi il file resta aperto fino alla fine del test.
Il codice dell'applicazione non c'entra: usa SQLAlchemy/aiosqlite e chiude. Prova: lanciando gli stessi 34 test con un plugin che chiude la
connessione all'uscita del `with` (sottoclasse di `sqlite3.Connection` il cui `__exit__` chiama `close()`), **passano tutti e 34**; senza,
11 falliscono. Il nodo premium (`langbrain-premium-nodes`) ha già questa correzione nel proprio `tests_windows/conftest.py`
(`_ConnessioneChiusaAllUscita`) e lì i 11 non falliscono.

Non sono stati corretti, per scelta: restano documentati. Chi vuole farlo ha due strade, entrambe solo nei test: usare
`contextlib.closing(sqlite3.connect(...))` nei test elencati, oppure portare nel `conftest.py` la sottoclasse che chiude all'uscita.

## Test instabili

- `test_win_errori_llm.py::RicaricaEnvTest` rilevava il `.env` dalla data di modifica: ora il test imposta date crescenti e non dipende
  dalla risoluzione del file system.
