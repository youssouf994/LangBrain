# Tornare a SQLite (rollback)

Il database si sceglie con una variabile d'ambiente. SQLite è il predefinito e funziona sempre: tornare indietro non
richiede di toccare il codice.

## 1. Interruttore (secondi, senza ricostruire nulla)

| Variabile | Valore | Effetto |
|---|---|---|
| `LANGBRAIN_DB_BACKEND` | `sqlite` (o assente) | Il database è il file SQLite indicato da `DB_PATH` (in Docker `/data/langbrain.db`). |
| `LANGBRAIN_DB_BACKEND` | `postgres` | Il database è `LANGBRAIN_DB_URL` (`postgresql://utente:password@host:5432/database`). Senza URL l'avvio è rifiutato con un messaggio chiaro. |

Per tornare a SQLite:

```bash
# ambiente / .env
LANGBRAIN_DB_BACKEND=sqlite        # oppure togliere la riga
# LANGBRAIN_DB_URL=...             # ignorato con sqlite (un avviso nel log lo ricorda)
```

Con Docker Compose basta rilanciare con la variabile cambiata: `LANGBRAIN_DB_BACKEND=sqlite docker compose up -d`. Il volume
`/data` con il file SQLite non viene mai toccato quando si usa Postgres, quindi resta com'era.

### Chi usava `DATABASE_URL`

Il nucleo **non legge più `DATABASE_URL`** (la leggevano solo le versioni di sviluppo del ramo Postgres, mai un tag). Si usano
`LANGBRAIN_DB_BACKEND=postgres` e `LANGBRAIN_DB_URL` con lo stesso URL. Se `DATABASE_URL` è impostata e `LANGBRAIN_DB_URL` no,
all'avvio compare un avviso nel log che dice quale variabile usare; il database resta SQLite, senza fallback silenziosi.

## 2. Cosa succede ai dati

- **I due database sono indipendenti.** Nulla viene copiato da Postgres a SQLite (né il contrario) in automatico: tornando a
  SQLite si ritrovano i dati che c'erano nel file SQLite, non quelli scritti su Postgres nel frattempo.
- **Le migrazioni sono additive e idempotenti, su entrambi.** Si aggiungono colonne e indici (oggi: `correlation_id`,
  `outcome`, `command_id`, `approval_id` e l'indice `idx_events_correlation` sulla tabella `events`); non si toglie né si
  rinomina nulla. Un file SQLite migrato dal codice nuovo si apre senza problemi con il codice vecchio (il commit `3cb93f6`
  e il ramo `poc-sqlite`): le colonne in più vengono ignorate. È stato provato nei due sensi.
- **I timestamp** delle righe nuove hanno i millisecondi (`2026-10-14 13:02:11.420000` nel file); il codice vecchio li legge
  senza errori. Le righe vecchie restano al secondo.

## 3. In ultima istanza: il codice del PoC

Se il problema è nel codice e non nella scelta del database, si torna all'oggetto della licenza con HQ:

```bash
git fetch origin --tags
git checkout poc-sqlite                      # ramo di sole correzioni
# oppure, il punto fisso:
git checkout poc-contratto-1.0.4-base        # tag, commit 3cb93f6
docker compose build && docker compose up -d
```

`poc-sqlite` e il tag `poc-contratto-1.0.4-base` esistono su `origin` e puntano al commit `3cb93f6` (creati il 16 ottobre 2026, quando `main` ha
raccolto tutto il lavoro successivo): sono il rollback, non vanno spostati. Le correzioni al PoC vanno solo su `poc-sqlite`.

## 4. Verifica dopo il rollback

```bash
curl -s http://localhost:8000/                       # risponde
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/events | head -c 300
docker compose logs --tail 20 | grep -i "database inizializzato"   # deve nominare il file SQLite
```
