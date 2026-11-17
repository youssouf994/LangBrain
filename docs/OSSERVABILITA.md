# Osservabilità: metriche, traccia e protezione dei modelli

Tre cose, tutte senza dipendenze nuove: le **metriche** Prometheus (`GET /metrics`), la **traccia** W3C (`traceparent`) e la **protezione** delle
chiamate al modello (interruttore di circuito, budget, timeout) che le alimenta.

## Metriche (`GET /metrics`)

Formato testuale di Prometheus 0.0.4, ruolo minimo `tirocinante` (se le chiavi API sono attive serve l'header `X-API-Key`).

```yaml
# prometheus.yml
scrape_configs:
  - job_name: langbrain
    metrics_path: /metrics
    http_headers:                 # Prometheus 2.55 o successivo
      X-API-Key:
        values: ["<chiave del ruolo tirocinante>"]
    static_configs:
      - targets: ["langbrain:8000"]
```

| Metrica | Tipo | Etichette | Cosa dice |
|---|---|---|---|
| `langbrain_http_requests_total` | contatore | `method`, `route`, `status` | richieste servite (la rotta è il modello `/effectors/{device_id}`, le sconosciute `non_trovata`) |
| `langbrain_http_request_duration_seconds` | istogramma | `method`, `route` | durata delle richieste (un flusso SSE conta per l'intero flusso) |
| `langbrain_llm_calls_total` | contatore | `provider`, `model`, `outcome` | chiamate al modello; `outcome` è `ok` o il codice dell'errore (`LIMITE_RICHIESTE`, `CIRCUITO_APERTO`, ...) |
| `langbrain_llm_call_duration_seconds` | istogramma | `provider` | durata delle chiamate |
| `langbrain_llm_tokens_estimated_total` | contatore | `provider`, `direction` | token **stimati** (4 caratteri per token), non quelli fatturati |
| `langbrain_llm_circuit_state` | indicatore | `provider`, `model` | 0 chiuso, 1 in prova, 2 aperto |
| `langbrain_llm_circuit_rejections_total` | contatore | `provider`, `model`, `motivo` | chiamate respinte prima di partire (`circuito`, `budget`) |
| `langbrain_llm_budget_used_ratio` | indicatore | `provider`, `tipo` | quota di budget usata, 0..1 |
| `langbrain_device_operations_total` | contatore | `device`, `operation`, `outcome` | letture (`read`) e comandi (`command`) ai dispositivi |
| `langbrain_device_operation_duration_seconds` | istogramma | `operation` | durata delle operazioni sui dispositivi |
| `langbrain_device_online` | indicatore | `device` | 1 se il dispositivo risponde |
| `langbrain_device_circuit_state` | indicatore | `device` | interruttore del dispositivo (adattatore HTTP) |
| `langbrain_device_reconciliations_total` | contatore | `outcome` | `allineato`, `deriva`, `irraggiungibile`, `ripristinato` |
| `langbrain_graph_node_runs_total` | contatore | `node`, `outcome` | esecuzioni dei nodi del grafo |
| `langbrain_graph_node_duration_seconds` | istogramma | `node` | durata dei nodi |
| `langbrain_database_up`, `langbrain_uptime_seconds`, `langbrain_build_info`, `langbrain_workers` | indicatori | — | database raggiungibile, durata di vita, versione, processi |

**Più processi** (`python -m app.serve`): ogni processo pubblica ogni 5 secondi la propria istantanea nella tabella `runtime_state` e `GET /metrics`
somma quelle di tutti (contatori e istogrammi si sommano, gli indicatori prendono il massimo). Si vede un servizio solo, qualunque processo risponda.
Un processo che non pubblica da più di 60 secondi non conta più.

`LANGBRAIN_METRICS=0` spegne i contatori delle richieste HTTP; gli altri costano quasi nulla e restano.

### Allarmi consigliati

[`docs/monitoraggio/allarmi.yml`](monitoraggio/allarmi.yml) contiene regole pronte (Prometheus/Alertmanager): database giù, interruttore di un
modello aperto, budget oltre l'80%, errori 5xx, dispositivo offline o con interruttore aperto, deriva dello stato fisico, nodi del grafo in errore,
nessun processo che pubblica.

## Traccia (`traceparent`)

Non c'è un esportatore OpenTelemetry: c'è la **propagazione** dello standard [W3C Trace Context](https://www.w3.org/TR/trace-context/), che basta a
collegare LangBrain a ciò che sta a monte e a valle.

- Se la richiesta porta un `traceparent` valido, la traccia continua (stesso `trace-id`, nuovo `span-id`); altrimenti ne nasce una.
- La risposta riporta `traceparent` e `X-Correlation-Id`. Senza `X-Correlation-Id` in ingresso, il correlation id è il `trace-id` ricevuto: il registro
  delle decisioni (`events.correlation_id`) e i log JSON si ritrovano dall'altra parte con lo stesso identificatore.
- Le chiamate in uscita verso i dispositivi (adattatore HTTP) portano `traceparent` (nuovo span, stessa traccia) e `X-Correlation-Id`: un gateway che li
  registra collega il comando fisico alla decisione che l'ha causato.
- I fatti che non nascono da una richiesta (timer HITL, riconciliazione) aprono una traccia propria.

## Protezione dei modelli

Configurazione in `configurazione.toml` (si ricarica a caldo), sezioni `[llm.protezione]`, `[llm.timeout_secondi]`, `[llm.budget]`.

- **Interruttore di circuito** per (provider, modello). Dopo `soglia_errori` (5) errori consecutivi di *indisponibilità* — provider non raggiungibile, errore
  del provider, limite di richieste, crediti, chiave, errore imprevisto — le chiamate a quel modello sono respinte subito con `CIRCUITO_APERTO` per
  `pausa_secondi` (60): nessuna richiesta aspetta il timeout di un provider caduto. Scaduta la pausa passa **una** chiamata di prova: se riesce l'interruttore
  si chiude, se fallisce si riapre. Gli errori della singola richiesta (`RISPOSTA_TRONCATA`, `CONTESTO_SUPERATO`, `MODELLO_NON_TROVATO`, ...) non lo aprono:
  il provider ha risposto.
- **Budget** per provider: `richieste_ora` e `token_giorno` (giorno UTC). Esaurito il budget la chiamata è respinta con `BUDGET_ESAURITO`. I token sono
  **stimati** (4 caratteri per token): serve un freno alla spesa, non una fattura. Con più processi il limite si divide per il numero di processi.
- **Timeout** per provider o `"provider/modello"`: massimo per un tentativo, comprese le rigenerazioni di una risposta troncata.

Coerente con il resto del progetto, **nessun ripiego silenzioso**: un circuito aperto o un budget esaurito sono errori del modello come gli altri, quindi
il grafo si ferma e l'operatore decide (cambia modello, alza il budget, aspetta). Con il ripiego attivo (`[llm] fallback = true`) un modello con il circuito
aperto passa al modello o al provider successivo della catena. Lo stato degli interruttori e dei conteggi è in memoria del processo e riparte da zero a ogni
riavvio.

Via API: `GET /llm/protection` (ruolo `tirocinante`) riporta interruttori e uso del budget del processo che risponde; `POST /llm/protection/reset`
(ruolo `medico_di_guardia`, parametri facoltativi `provider` e `model`) chiude subito gli interruttori aperti quando il provider è tornato. Non azzera il budget.

## Limiti

- Nessun esportatore di tracce (OpenTelemetry/Jaeger) e nessuno *span* interno ai nodi del grafo: la durata dei nodi è nelle metriche e in `GET /graph/stats`.
- Gli interruttori e i budget non sono condivisi tra processi (il budget si divide, l'interruttore è per processo).
- Le etichette `device`, `node`, `model` hanno tanti valori quanti ne ha l'installazione: con migliaia di dispositivi valuta di toglierle.
