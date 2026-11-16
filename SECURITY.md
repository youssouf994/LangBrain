# Sicurezza

## Segnalare una vulnerabilità

Non aprire una issue pubblica. Usa la segnalazione privata di GitHub: scheda **Security** del repository, poi
**Report a vulnerability**. Indica cosa hai trovato, come riprodurlo e quale versione usi. Riceverai una risposta
appena possibile; la correzione viene pubblicata prima di descrivere il problema.

## Cosa aspettarsi da LangBrain

LangBrain è un framework per dispositivi IoT e agenti LLM, pensato per essere eseguito in una rete fidata.

- **Autenticazione**: se nel `.env` è impostata almeno una chiave (`API_KEY`, `API_KEY_PRIMARIO`,
  `API_KEY_MEDICO_DI_GUARDIA`, `API_KEY_TIROCINANTE`) ogni endpoint, tranne `GET /` e la pagina `GET /demo`, richiede
  l'header `X-API-Key` e i tre ruoli hanno permessi separati (vedi `docs/HOW_TO_CUSTOMIZE.md`). **Senza nessuna chiave
  l'API è aperta a chiunque la raggiunga**: all'avvio compare un avviso. Non esporla su Internet in quello stato.
- **Trasporto**: l'API non fa TLS. Per usarla fuori da localhost mettila dietro un reverse proxy HTTPS.
- **Segreti**: le chiavi dei provider LLM e dell'API stanno solo nel `.env`, che è ignorato da Git. I messaggi di errore dell'LLM
  oscurano le chiavi. Le chiavi presenti in `.env.example` e nei test sono segnaposto.
- **Comandi ai dispositivi**: nessun agente può attuare un dispositivo o un valore non elencato in
  `configurazione.toml`; con `dispositivi_non_elencati = "rifiuta"` (predefinito) un nome nuovo viene respinto.
- **Più processi**: `python -m app.serve` condivide lo stato (configurazione HITL, grafo, valori simulati, metriche) nel database; `python -m uvicorn` è un processo solo.
  Gli interruttori di circuito e i budget dei modelli sono per processo (il budget si divide).
- **Metriche**: `GET /metrics` richiede il ruolo `tirocinante` quando le chiavi sono attive; non espone segreti ma elenca nomi di dispositivi, nodi e modelli.
- **Dispositivi veri**: il token del gateway sta solo nel `.env` (`token_env`), mai in `configurazione.toml`; gli URL dei dispositivi li sceglie chi modifica la configurazione
  (ruolo `primario`). Il sistema non comanda mai un dispositivo vero da solo per "ripristinare" uno stato, né durante `DELETE /system/reset`.
- **Migrazioni**: in produzione usa `LANGBRAIN_MIGRAZIONI=manuale` ed esegui `alembic upgrade head` dopo un backup (`docs/MIGRAZIONI.md`).

## Controlli prima di pubblicare

```bash
python -m pytest tests
bash scripts/scansione_sicurezza.sh   # gitleaks + pip-audit
```

La CI (`.github/workflows/ci.yml`) esegue gli stessi controlli a ogni push e pull request.
