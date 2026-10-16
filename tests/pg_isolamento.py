"""
Esegue la stessa suite su Postgres invece che su SQLite.

Con `TEST_PG_URL=postgresql://...` (un database di PROVA) ogni database temporaneo dei test — che di solito è un file
SQLite in una cartella temporanea — diventa uno SCHEMA proprio dentro quel database Postgres (`?lb_schema=...`), quindi i
test restano isolati come con i file e il codice sotto test è esattamente quello di produzione.

I test che aprono il file con `sqlite3`/`aiosqlite` per ispezionarlo o modificarlo non hanno senso su Postgres: sono
elencati in `NON_SU_POSTGRES` con il motivo. Ogni altro fallimento in questa modalità è un errore da correggere.

Locale:  docker compose -f docker-compose.test.yml up -d
         TEST_PG_URL=postgresql://postgres:prova@localhost:55433/langbrain_test python -m pytest tests
"""

import hashlib
import os

import pytest

URL_BASE = (os.getenv("TEST_PG_URL") or "").strip()

# nodeid (suffisso) -> motivo. Si riempie solo con test che ispezionano il file SQLite direttamente.
_SQLITE_DIRETTO = "apre il file SQLite con sqlite3/aiosqlite per ispezionarlo o modificarlo"
_SQLITE_ELIMINAZIONE = "verifica che il file SQLite si possa cancellare o ricreare"
NON_SU_POSTGRES: dict[str, str] = {
    "DatabaseTest::test_il_conflitto_dimostrativo_si_semina_solo_se_richiesto_e_non_si_accumula": _SQLITE_DIRETTO,
    "DatabaseTest::test_init_db_crea_le_tabelle_e_l_indice": _SQLITE_DIRETTO,
    "DatabaseTest::test_init_db_e_idempotente_e_non_semina_dati_di_prova_per_default": _SQLITE_DIRETTO,
    "DatabaseTest::test_un_database_con_lo_schema_vecchio_viene_migrato": _SQLITE_DIRETTO + " (la migrazione su Postgres è in test_postgres.py)",
    "CheckpointerPersistenteTest::test_tabelle_create_nello_stesso_file_del_db_applicativo": _SQLITE_DIRETTO,
    "EventLogDatabaseTest::test_expiry_skips_only_actions_already_expired_or_resolved": _SQLITE_DIRETTO,
    "SignalTableTest::test_window_excludes_old_rows": _SQLITE_DIRETTO,
    "RisoluzioneModelloTest::test_un_ciclo_nei_dati_non_causa_un_ciclo_infinito": _SQLITE_DIRETTO,
    "MigrazioneTest::test_un_database_vecchio_riceve_colonne_e_indice": _SQLITE_DIRETTO + " (PRAGMA)",
    "ScenarioTest::": _SQLITE_DIRETTO,
    "ScriptScenarioTest::": _SQLITE_DIRETTO + " e ne copia il file",
    "UrlTest::test_file_sqlite": "prova la traduzione di un percorso in URL SQLite, che qui è sostituita",
    "UrlTest::test_il_backend_predefinito_e_sqlite_anche_se_c_e_un_url": "prova la scelta SQLite, che qui è sostituita",
    "UrlTest::test_backend_postgres_vale_solo_per_il_file_predefinito": "prova la scelta del file, che qui è sostituita",
    "MotoreSqliteTest::": _SQLITE_ELIMINAZIONE,
}

_schemi: set[str] = set()


def attivo() -> bool:
    return bool(URL_BASE)


def installa() -> None:
    """Fa mappare a `archivio.url_database` ogni percorso di file su uno schema Postgres dedicato."""
    if not attivo():
        return
    from app.core import archivio

    originale = archivio.url_database
    separatore = "&" if "?" in URL_BASE else "?"

    def url_su_postgres(percorso=None):
        if percorso and "://" not in str(percorso):
            schema = "t_" + hashlib.md5(os.path.abspath(str(percorso)).encode()).hexdigest()[:16]
            _schemi.add(schema)
            return originale(f"{URL_BASE}{separatore}{archivio.PARAMETRO_SCHEMA}={schema}")
        return originale(percorso)

    archivio.url_database = url_su_postgres

    # Ogni test asincrono ha il suo event loop, e una connessione asyncpg resta legata al loop che l'ha aperta: senza pool
    # (una connessione per operazione) un motore riusato da un test all'altro non porta con sé connessioni di un loop morto.
    from sqlalchemy.pool import NullPool

    crea_originale = archivio.create_async_engine

    def crea_senza_pool(url, **opzioni):
        if str(url).startswith("postgresql"):
            for chiave in ("pool_size", "max_overflow", "pool_pre_ping"):
                opzioni.pop(chiave, None)
            opzioni["poolclass"] = NullPool
        return crea_originale(url, **opzioni)

    archivio.create_async_engine = crea_senza_pool


def segna_test_non_adatti(items) -> None:
    if not attivo():
        return
    for item in items:
        for suffisso, motivo in NON_SU_POSTGRES.items():
            if item.nodeid.endswith(suffisso) or suffisso in item.nodeid:
                item.add_marker(pytest.mark.skip(reason=f"su Postgres non applicabile: {motivo}"))


def pulisci() -> None:
    """Toglie gli schemi creati dalla sessione (il database di prova resta pulito)."""
    if not attivo() or not _schemi:
        return
    try:
        import psycopg
        from app.core import archivio

        with psycopg.connect(archivio.url_per_psycopg(URL_BASE), autocommit=True) as conn:
            for schema in sorted(_schemi):
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    except Exception as errore:  # la pulizia non deve far fallire la suite
        print(f"[pg_isolamento] pulizia degli schemi non riuscita: {errore}")
