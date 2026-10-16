# Contribuire a LangBrain

Grazie per l'interesse. Il progetto è in italiano (codice, commenti, messaggi e documentazione); i README hanno anche
la versione inglese.

## Ambiente

Serve Python 3.12 o superiore.

```bash
python -m venv venv
source venv/bin/activate            # su Windows: .\venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
cp .env.example .env                # poi inserisci almeno una chiave di un provider LLM
```

## Prima di aprire una pull request

1. `python -m pytest tests` deve passare. I test usano database temporanei e non chiamano LLM reali: un test nuovo
   non deve dipendere dal tuo `.env` né toccare `langbrain.db`.
   Per provare anche Postgres: `docker compose -f docker-compose.test.yml up -d`, poi
   `TEST_PG_URL=postgresql://postgres:prova@localhost:55433/langbrain_test TEST_DATABASE_URL=$TEST_PG_URL python -m pytest tests`
   (ogni database temporaneo diventa uno schema; la CI lo fa in un job a parte). I test che aprono il file SQLite con
   `sqlite3` sono elencati in `tests/pg_isolamento.py` e su Postgres si saltano.
2. Una correzione di bug porta un test che fallisce senza la correzione.
3. `bash scripts/scansione_sicurezza.sh` non deve trovare segreti né vulnerabilità (servono `gitleaks` e `pip-audit`).
4. Se cambi comportamento o configurazione, aggiorna `docs/HOW_TO_CUSTOMIZE.md` e, se serve, i README.

## Convenzioni

- Nomi, commenti e messaggi in italiano; commenti solo dove il perché non è ovvio.
- Le scelte dell'utente stanno in `configurazione.toml`, i segreti nel `.env`, mai nel codice. Ordine di precedenza:
  variabile d'ambiente, poi `configurazione.toml`, poi valore predefinito nel codice.
- Un tool o un sotto-agente non solleva eccezioni al padre: restituisce un risultato strutturato
  (`app/core/risultati.py`), così l'errore risale la gerarchia con il suo motivo.
- Le dipendenze hanno versione esatta in `requirements.txt`; per aggiornarle rigenera `requirements.lock` da un ambiente
  pulito e rilancia i test.
- Ogni nuovo endpoint va aggiunto alla matrice dei ruoli in `app/core/ruoli.py` (un test verifica che non ne manchi nessuno).

## Provare le modifiche a mano

`python examples/avvia_demo.py` avvia il server con uno scenario di prova su un database dedicato (`demo.db`) e apre la
pagina che mostra il grafo in azione.

## Nodi costruiti sul core

Un caso d'uso (un nodo) non modifica il core: si aggancia con un modulo in `app/nodes/` che espone `ESTENSIONE`
(`app/core/estensioni.py`, `docs/HOW_TO_CUSTOMIZE.md` sezione 10). Il core non deve mai nominare un nodo. Le modifiche al
framework si fanno qui; i repository dei nodi le ricevono con un merge di `main` (workflow `notify-nodes.yml`).
