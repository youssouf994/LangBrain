# Dispositivi reali e stato fisico

Fino a ieri ogni dispositivo era un `SimulatedEffector`: un valore in memoria. Qui sta come collegare un dispositivo vero e come il sistema ne
ricorda e ne verifica lo stato dopo un riavvio.

## Collegare un dispositivo: `adattatore = "http"`

In `configurazione.toml`, dispositivo per dispositivo (gli altri restano simulati):

```toml
[dispositivi.front_door_lock]
valori = ["LOCKED", "UNLOCKED"]
adattatore = "http"
url = "http://gateway.local:8080/devices/front_door_lock"   # GET legge, POST comanda (se manca url_comando)
token_env = "GATEWAY_TOKEN"        # nome della variabile di .env col token Bearer: il token non sta mai nel TOML
timeout_secondi = 5
soglia_errori = 3                  # interruttore di circuito del dispositivo
pausa_secondi = 30
```

Tutto il resto del sistema non cambia: i permessi, l'elenco dei valori ammessi (`valori`, `intervallo`), le priorità, l'HITL, il troubleshooting
sui guasti. Dopo una modifica degli adattatori **riavvia l'app** (gli altri campi si ricaricano a caldo). Il nome del dispositivo deve essere esatto, non
un pattern.

### Contratto del gateway

`EffettoreHttp` (`app/tools/adattatori.py`) parla con un gateway — Home Assistant, un bridge MQTT, il servizio del produttore — con un contratto minimo:

| | Richiesta | Risposta attesa |
|---|---|---|
| Leggere | `GET <url>` | `200` con `{"value": "LOCKED"}` (anche uno scalare JSON o un testo semplice; `campo_valore` cambia il nome del campo) |
| Comandare | `POST <url_comando>` con `{"value": "UNLOCKED", "command_id": "<uuid>"}` | `2xx` = accettato; `4xx` = il dispositivo rifiuta (`COMMAND_REJECTED`); `5xx`, timeout, rete = guasto |

Ogni richiesta porta `Authorization: Bearer <token>` (da `token_env`), `Idempotency-Key` (= `command_id`, lo stesso su un eventuale secondo tentativo),
`X-Correlation-Id` e `traceparent` ([`OSSERVABILITA.md`](OSSERVABILITA.md)): un gateway che li registra collega il comando fisico alla decisione.
Si consiglia al gateway di usare `Idempotency-Key` per non eseguire due volte lo stesso comando.

### Cosa succede quando qualcosa va storto

- **Letture**: un errore di rete o di timeout si ripete una volta.
- **Comandi**: non si ripetono alla cieca (un timeout non dice se il comando è stato eseguito). Solo se la connessione non è mai stata stabilita
  (`ConnectError`: la richiesta non è partita) si riprova una volta, con lo stesso `command_id`.
- **Guasto** (5xx, 408, 429, timeout, rete): errore strutturato (`DEVICE_ERROR`, `TIMEOUT`, `DEVICE_UNREACHABLE`) che segue il percorso di sempre: il
  padre con priorità sufficiente diagnostica e ritenta una volta, poi il guasto sale al Brain e all'operatore.
- **Interruttore di circuito**: dopo `soglia_errori` guasti di fila il dispositivo è sospeso per `pausa_secondi` (`DEVICE_CIRCUIT_OPEN`, risposta
  immediata invece dell'attesa del timeout); una chiamata di prova decide se riaprire. Un `4xx` non è un guasto: il gateway risponde.
- `app.core.dispositivi.descrivi_dispositivo` (la descrizione che l'adattatore del contratto espone in `/health`) riporta `online: false` mentre il
  dispositivo non risponde o l'interruttore è aperto; la metrica `langbrain_device_online` dice lo stesso.
- `DELETE /system/reset` **non comanda** i dispositivi veri (azzera solo i simulati): un reset del database non deve chiudere una serratura vera.

### Un altro protocollo (MQTT, Modbus, Zigbee diretto)

Scrivi una sottoclasse di `Reflex` con `get_tool_value` e `set_tool_value` (sollevando `ErroreTool` con un codice per i guasti), imposta `simulato = False` (senza l'attributo il tool non viene riconciliato)
e registrala con `registra_tool("nome", istanza)` (di solito dal modulo di un nodo in `app/nodes/`). Nessuna modifica al nucleo: stato fisico, riconciliazione,
metriche e guasti funzionano uguale. Un client MQTT con una connessione persistente va aperto e chiuso con le funzioni `chiusure` dell'estensione.

## Stato fisico che sopravvive al riavvio

Prima lo stato dei dispositivi viveva nella memoria del processo: dopo un riavvio il sistema non sapeva più cosa aveva comandato. Ora:

1. **Ogni comando riuscito** (qualunque `Reflex`, via `comanda_tool`) scrive l'ultimo valore comandato nella tabella `device_state` (`source = commanded`).
   Un comando fallito non cambia lo stato noto. Si spegne con `[stato_fisico] persistenza = 0`.
2. **All'avvio** (`app/core/riconciliazione.py`, un processo alla volta):
   - un dispositivo **simulato** ritrova in memoria l'ultimo valore comandato (non ha un hardware che lo ricordi);
   - un dispositivo **vero** viene *letto* — mai comandato: un riavvio non deve aprire una serratura — e confrontato con l'ultimo comando.
3. **A intervalli** (`[stato_fisico] intervallo_secondi`, 300; 0 = solo all'avvio) lo stesso confronto per i dispositivi veri, in un processo solo.

Esiti della riconciliazione di un dispositivo vero:

| Esito | Significato | Cosa succede |
|---|---|---|
| `allineato` | il valore letto coincide con l'ultimo comando (`22`, `"22.0"` e `"22°C"` sono lo stesso valore; i testi senza maiuscole) | niente |
| `deriva` | l'ultimo stato noto era un **comando** e il dispositivo ora dichiara un valore diverso (qualcuno è intervenuto a mano, il comando non ha avuto effetto, il dispositivo si è riavviato) | evento `RECONCILED_DRIFT` nel registro (vecchio = comandato, nuovo = letto); il valore letto diventa lo stato noto. **Nessun comando automatico**: il Brain lo vede tra gli eventi recenti e decide |
| `irraggiungibile` | la lettura è fallita | evento `RECONCILED_UNREACHABLE` (una volta per interruzione, non a ogni giro); lo stato noto resta quello di prima |
| `ripristinato` | (solo simulati, all'avvio) | il valore è rimesso in memoria |

Il prefisso `RECONCILED_` fa sì che gli eventi siano per le regole di priorità eventi *chiusi*, mai blocchi: una deriva non impedisce a un agente di agire.
Una differenza tra due valori **osservati** (un sensore che cambia da solo) non è una deriva: lo è solo la differenza dall'ultimo *comando*.

`GET /effectors/{id}/state` e `GET /system/device-state` (ruolo `tirocinante`) restituiscono l'ultimo stato noto salvato, senza interrogare i dispositivi (404 se mai comandato né riconciliato).
`POST /effectors/reconcile` (ruolo `medico_di_guardia`) esegue la riconciliazione subito e restituisce il rapporto per dispositivo. Nelle metriche:
`langbrain_device_reconciliations_total{outcome}`.

## Limiti

- Con più processi i dispositivi **veri** non hanno un valore condiviso nel database: ogni lettura va al dispositivo (è la verità).
- Un solo adattatore incluso (HTTP). MQTT/Modbus sono sottoclassi da scrivere, come sopra.
- La riconciliazione non *corregge*: segnala. Ripristinare l'ultimo comando dopo un riavvio è una decisione di dominio (per una serratura è la scelta
  sbagliata) e spetta al Brain, a una regola o all'operatore, non al codice.
