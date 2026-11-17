# Migrazioni del database

Lo schema del database cambia solo con le migrazioni [Alembic](https://alembic.sqlalchemy.org): `migrations/` (le revisioni), `alembic.ini` (la
configurazione), `app/db/migrazioni.py` (come l'applicazione le esegue). Valgono uguali su SQLite e su Postgres. L'auto-migrazione di prima
(aggiunta silenziosa delle colonne mancanti) non c'è più: ora ogni cambio di schema è una revisione scritta, versionata e rivedibile.

## Come funziona

| | |
|---|---|
| Revisione `0001` | la **baseline**: lo schema com'era prima di Alembic, con una copia congelata delle definizioni. Su un database nuovo crea tutto; su un database di una versione precedente (tabelle già presenti, magari con meno colonne) aggiunge le colonne e gli indici che mancano, senza toccare i dati. |
| Revisione `0002` | tabella `device_state` (stato fisico dei dispositivi, [`DISPOSITIVI_REALI.md`](DISPOSITIVI_REALI.md)). |
| Tabella `alembic_version` | la revisione in cui si trova il database. |

All'avvio `Database.init_db()` porta il database all'ultima revisione, dentro il blocco di avvio (con più processi parte uno alla volta; su
Postgres c'è in più un blocco consultivo).

## Produzione: migrare come passo del deploy

Con `LANGBRAIN_MIGRAZIONI=manuale` l'applicazione **non** migra: verifica che il database sia già all'ultima revisione e, se è indietro, si
ferma con un messaggio chiaro. Così lo schema cambia in un momento scelto (con un backup fatto) e non al primo avvio di una nuova versione.
`GET /system/migrations` (ruolo `tirocinante`) mostra modo, revisione del database, revisione attesa dal codice e se sono allineate.

```bash
# stesso ambiente dell'app: DB_PATH oppure LANGBRAIN_DB_BACKEND=postgres + LANGBRAIN_DB_URL
alembic upgrade head      # porta lo schema all'ultima revisione
alembic current           # dove si trova adesso
alembic check             # 0 se lo schema nel codice e quello delle revisioni coincidono
LANGBRAIN_MIGRAZIONI=manuale python -m app.serve ...
```

Con Docker: un passo di deploy `docker compose run --rm langbrain alembic upgrade head`, poi `docker compose up` con
`LANGBRAIN_MIGRAZIONI=manuale`. Il predefinito resta `auto`, comodo in sviluppo e con un'istanza sola.

## Cambiare lo schema

1. Modifica `app/db/schema.py`.
2. `alembic revision --autogenerate -m "descrizione breve"` crea il file in `migrations/versions/`. **Rileggilo**: l'autogenerazione propone, non
   decide (non capisce le rinomine: le vede come cancella-e-crea, quindi perderebbe i dati).
3. Prova su una copia: `alembic upgrade head`, poi `alembic downgrade -1` e di nuovo `upgrade head`.
4. `python -m pytest tests/test_migrazioni.py`: il test `lo_schema_nel_codice_e_quello_delle_migrazioni_coincidono` fallisce se hai cambiato
   `schema.py` senza la revisione.

Regole:

- **Mai modificare una revisione già rilasciata**: se ne scrive una nuova. La `0001` è congelata apposta.
- **Additive prima di distruttive.** Aggiungere una colonna è sicuro con l'app vecchia ancora in esecuzione; toglierla o rinominarla va fatto in due
  rilasci (prima il codice smette di usarla, poi la revisione la elimina).
- **Una revisione deve poter girare su un database già parzialmente aggiornato** quando la stessa tabella può essere creata altrove (es. la `0002`
  controlla `has_table`): alcuni moduli creano la propria tabella al primo uso.
- Le tabelle del checkpointer di LangGraph (`checkpoints`, `writes`, ...) non sono nostre: Alembic le ignora (`include_object` in `migrations/env.py`).
- L'indice `idx_events_target_ts` ha un'espressione (`timestamp DESC`) che Alembic non sa confrontare: è escluso dall'autogenerazione.

## Ripristino

`alembic downgrade -1` annulla l'ultima revisione (la `0001` non si annulla: cancellerebbe i dati). Per tornare a una versione precedente del codice
dopo una migrazione si ripristina il backup del database: vedi [`ROLLBACK.md`](ROLLBACK.md) per il cambio di backend.

## `examples/crea_scenario.py` e `DELETE /system/reset`

`azzera_database` elimina anche `alembic_version`: senza, Alembic crederebbe lo schema già aggiornato e non ricreerebbe le tabelle.
