# Simulazione energetica

Una rete elettrica e gas simulata, con accumuli, che gira dentro il server di LangBrain e si guarda (e comanda) dalla
pagina `/energia`. Serve come banco di prova realistico e sotto stress per gli agenti: i valori cambiano da soli nel
tempo (meteo, carichi, prezzi, guasti) e producono le situazioni scomode che la gerarchia dovrà gestire.

Sopra la rete lavora una gerarchia di agenti LangBrain (Brain, organi, componenti) collegata dal **sistema nervoso**:
le anomalie svegliano il grafo, gli agenti leggono i sensori e azionano le leve della rete, le decisioni che fermano
l'industria arrivano al Brain e all'operatore. Gli agenti usano il modello LLM reale configurato nel `.env`.

## Avvio

```bash
python -m uvicorn app.api.main:app --port 8765
# poi apri http://127.0.0.1:8765/energia
```

La pagina si disattiva con `[demo] pagina_web = 0` in `configurazione.toml`, come `/demo`. Se le chiavi API sono
attive, inseriscila in alto nella pagina: leggere basta il tirocinante, avviare e mettere in pausa serve il medico di
guardia, creare scenari e provocare eventi serve il primario.

Dalla pagina si sceglie lo scenario (Italia o rete generata), la data di inizio, la durata (da ore ad anni), il passo
(5, 15, 30 o 60 minuti) e il seed; poi si avvia e si regola la velocità, da un minuto simulato al secondo fino a
"massima". I pulsanti **+1 passo** e **+1 giorno** avanzano a mano a simulazione ferma.

## Cosa si vede

- **Mappa schematica**: triangoli per le centrali (colore = tecnologia, riempimento = quota della potenza in uso,
  bordo rosso con croce = guasto, tratteggiato = manutenzione), quadrati per le città (rossi se c'è carico non servito,
  bordo arancione se manca gas), rombi per gli accumuli (riempimento = carica; bordo verde in scarica, blu in carica),
  cerchi per gli ingressi gas, esagoni per gli stoccaggi. Linee piene per l'elettricità, tratteggiate per il gas; lo
  spessore è la capacità, il colore il carico (arancione oltre l'80%, rosso se satura o guasta), la freccia il verso.
  Passando sopra un simbolo compare un riepilogo; cliccando si apre il dettaglio con lo storico orario e i pulsanti
  per provocare un guasto o ripristinarlo.
- **Cruscotto**: domanda, carico non servito, quota rinnovabile, prezzi, margine di riserva, import, accumuli, CO₂,
  gas richiesto e non servito, riempimento degli stoccaggi rispetto all'obiettivo stagionale.
- **Grafici**: produzione per fonte con la domanda (ultimi 7 giorni oppure medie giornaliere dell'intera simulazione),
  prezzi di elettricità e gas, domanda di gas e stoccaggi.
- **Anomalie ed eventi** in corso, cliccabili; eventi globali (ondata di calore o di freddo, calma di vento, picco
  del gas) con un pulsante.
- **Prestazioni**: entità, passi al secondo del motore, millisecondi per passo, ore simulate al secondo, tempo di
  disegno della mappa, dimensione dei messaggi. Con una rete generata grande la pagina diventa il test di carico visivo.

## Il modello

Tutti i dati sono **plausibili, non reali**: ordini di grandezza del sistema italiano, semplificati e arrotondati.
La centrale nucleare di Trino è ipotetica.

**Rete per zone di mercato.** Come nel mercato italiano, la rete elettrica è divisa in zone (Nord, Centro-Nord,
Centro-Sud, Sud, Calabria, Sicilia, Sardegna) collegate da limiti di transito. Dentro una zona la rete è un nodo
unico; tra le zone l'energia passa sulle linee fino alla loro capacità. La rete gas usa le stesse zone con le sue
condotte; la Sardegna non ha rete gas.

**A ogni passo:**

1. Meteo e prezzi avanzano con processi casuali correlati nel tempo e tra le zone: temperatura (stagione, ora,
   anomalie), nuvolosità, vento, prezzo del gas e della CO₂.
2. Eventi casuali: guasti di centrali (totali per gli impianti singoli, parziali per le flotte), linee, condotte e
   accumuli; crisi di fornitura degli ingressi gas; ondate di calore (estate) e di freddo (inverno); calme di vento;
   picchi del prezzo del gas. Le centrali termiche singole hanno 14 giorni di manutenzione programmata all'anno.
3. Domanda elettrica delle città: forma oraria (feriale o festiva), ferie di agosto e periodo natalizio, effetto della
   temperatura (condizionatori, pompe di calore), rumore. È calibrata perché la media annua sia quella dello scenario
   (circa 310 TWh per l'Italia).
4. Dispacciamento elettrico in ordine di merito: rinnovabili a costo zero, geotermico, nucleare, biomasse, bacini
   idroelettrici (costo dell'acqua che sale quando il bacino si svuota), gas (costo dal prezzo del gas e della CO₂ e
   dal rendimento del singolo impianto), carbone, import (più economico di notte), accumuli. Ogni offerta copre prima
   la propria zona e poi le altre raggiungibili entro i limiti delle linee. Il prezzo è il costo dell'ultima offerta
   usata; la domanda non coperta è carico non servito (prezzo 3000 €/MWh).
5. Gli accumuli comprano quando il prezzo è sotto la media delle ultime 24 ore e vendono quando è sopra, con le
   perdite di andata e ritorno (batterie 88%, pompaggi 76%).
6. Dispacciamento del gas: consumo civile (dipende dai gradi di riscaldamento), industriale (feriale o festivo) e delle
   centrali a gas (dal loro rendimento). Offerte: quota contrattuale degli ingressi, poi la parte flessibile
   (gasdotti, rigassificatori, produzione nazionale), stoccaggi e, per ultimo, il gas contenuto nei tubi (linepack).
   Gli stoccaggi si riempiono d'estate verso il 95% di novembre e si svuotano d'inverno. Il gas mancante si toglie
   prima all'industria, poi alle centrali, per ultimo alle case; le centrali a gas senza gas producono meno al passo
   successivo (accoppiamento tra le reti). La pressione segue il linepack.
7. Anomalie, statistiche cumulative e serie storiche.

**Margine di riserva.** Per ogni zona: capacità programmabile e di accumulo non usata nella zona, più quella che le
linee possono ancora portarle dalle zone vicine (limitata a ciò che lì è libero), divisa per la domanda della zona.
Il cruscotto e i sensori mostrano quello della zona più debole: una Sicilia isolata va in riserva bassa alla punta
serale anche se il resto d'Italia ha capacità in abbondanza, prima che si arrivi al distacco. Il margine nazionale
(tutta la capacità libera sulla domanda) resta in `margine_riserva_nazionale`.

**Determinismo.** Stesso scenario, seed e passo: stessa storia, passo per passo. La velocità di esecuzione non cambia
i risultati. Gli eventi provocati a mano cambiano la storia da quel momento.

**Unità.** Potenza MW, energia MWh (TWh nei totali), gas kSm³/h (migliaia di metri cubi standard all'ora), giacenze
in milioni di m³, pressione in bar, prezzi in €/MWh.

## Leve operative

Sei comandi ON/OFF, spenti all'inizio, elencati in `configurazione.toml` (`[dispositivi."rete_el_*"]` e
`[dispositivi."rete_gas_*"]`): nessun'altra scrittura è ammessa.

| Leva | Effetto |
|---|---|
| `rete_el_interrompibili` | in ogni zona fino all'8% della domanda si stacca in modo programmato a 400 €/MWh, prima del distacco incontrollato (3000 €/MWh) |
| `rete_el_import_emergenza` | le interconnessioni salgono dal 78% al 100%; la parte in più costa 40 €/MWh in più |
| `rete_el_accumuli_riserva` | gli accumuli si ricaricano appena possono e si scaricano solo in emergenza (oltre 350 €/MWh) |
| `rete_gas_interrompibili` | i consumi industriali di gas calano del 30% |
| `rete_gas_stoccaggio_strategico` | gli stoccaggi erogano alla portata massima, qualunque sia il riempimento |
| `rete_gas_gnl_spot` | i rigassificatori salgono al 135% con carichi comprati sul mercato spot |

Il carico interrotto non è carico non servito: il riepilogo li conta a parte, con il costo delle misure. In una crisi
di prova (freddo, cinque ingressi gas ridotti, Sicilia isolata) le leve portano l'energia non servita da 1225 MWh a
zero e il gas non servito da 257 a 24 milioni di m³.

## Agenti e sistema nervoso

```
Brain  (Centro nazionale di controllo, priorità 1000)
├── organo_rete_elettrica   leva: import d'emergenza            (600)
│   ├── componente_accumuli     leva: accumuli in riserva       (300)
│   └── componente_carichi      leva: carichi interrompibili    (250, chiede sempre al padre)
└── organo_rete_gas         leva: GNL spot                      (600)
    ├── componente_stoccaggi    leva: stoccaggio strategico     (300)
    └── componente_consumi_gas  leva: interrompibilità del gas  (250, chiede sempre al padre)
```

Agenti, prompt e soglie sono in `app/agents/agenti_energia.py`; i tool in `app/tools/strumenti_energia.py`: tre
sensori di sola lettura (`sensore_rete_elettrica`, `sensore_accumuli`, `sensore_rete_gas`, che restituiscono la
situazione in JSON) e le sei leve.

- Ogni agente visita prima i figli, poi legge i propri sensori e decide sulla propria leva con il modello:
  `ATTIVA`, `DISATTIVA`, `ESCALATE` (chiede al padre) o `NESSUNA`, con la motivazione.
- `ESCALATE` serve solo quando la propria leva è già accesa e non basta: l'agente chiede allora la **misura
  successiva** ancora spenta (per esempio l'organo gas chiede lo stoccaggio strategico, poi l'interrompibilità del gas).
  Se sono già tutte accese resta una `SEGNALAZIONE` nell'audit log, senza comando. Le richieste al Brain sono sempre di
  accensione: la propria leva non viene mai proposta spenta per errore.
- **Gli apparati hanno già elaborato i dati.** I tool non sono semplici letture: i sensori riportano nelle `verifiche`
  i confronti con ogni soglia delle regole (margine sotto il 10%, pressione sopra 55 bar, stoccaggi sopra o sotto
  l'obiettivo e di quanti punti...) e ogni leva ha un **automatismo** che applica le sue regole ai fatti e dà
  un'indicazione (ATTIVA, DISATTIVA, ESCALATE, NESSUNA) con la regola che l'ha prodotta. Sono funzioni deterministiche
  in `app/tools/strumenti_energia.py`; la tabella `REGOLE_LEVE` è l'unica fonte delle regole e i prompt degli agenti
  la riportano con gli stessi testi.
- Il modello decide seguendo l'indicazione: se se ne discosta deve dire quale rischio le regole non coprono, e lo
  scostamento compare nei passi del grafo e nell'audit log (`SCOSTAMENTO_AUTOMATISMO`). Quando un agente chiede una
  leva al Brain, gli passa anche l'indicazione dell'automatismo di quella leva.
- **Arco riflesso**: se l'automatismo indica NESSUNA e nel dominio dell'agente non c'è nessuna anomalia, l'agente
  risponde senza chiamare il modello. Un ciclo in una situazione normale non costa nulla.
- Le leve che fermano l'industria (`rete_el_interrompibili`, `rete_gas_interrompibili`) non vengono mai azionate dal
  componente: la richiesta sale al Brain, che per queste leve chiede sempre l'approvazione dell'operatore (flusso HITL
  "brain", chiave `target_critici_brain` del contesto del grafo). L'operatore approva, respinge o dà una direttiva
  (Override, solo primario).
- **Veti, in tempo simulato e per valore.** Un rifiuto del Brain vieta l'accensione di quella leva per 6 ore
  simulate: l'agente può tornare a chiederla, e il Brain sa che è una richiesta ripetuta. Un comando manuale
  dell'operatore fissa la leva per 6 ore simulate: gli agenti non la ribaltano e non la propongono al Brain. Nel
  dominio energia OFF è uno stato normale, non un blocco: la regola generale del framework sui blocchi (eventi OFF o
  REJECTED di attori più importanti, per una finestra di tempo reale) qui non si applica. I veti attivi compaiono
  nel pannello delle leve.
- Il Brain ha un prompt del dominio (priorità: persone, poi centrali, poi industria) e vede solo gli eventi delle
  leve e dei sensori dell'energia, non quelli della smart home nello stesso database.

Il **sistema nervoso** (`app/simulazione/sistema_nervoso.py`):

- *via afferente*: quando compare un'anomalia nuova del dominio degli agenti (carico o gas non servito, riserva
  bassa, pressione bassa, stoccaggi bassi, crisi di un ingresso), oppure quando tutto è rientrato e qualche leva è
  rimasta accesa, registra uno `STIMOLO` nell'audit log e sveglia il grafo;
- *via efferente*: gli agenti azionano le leve, ogni comando passa da validazione, priorità e audit log;
- mentre il grafo ragiona, e finché aspetta l'operatore, la simulazione è in pausa; poi riparte se stava andando.

Il grafo si sveglia a mano (**Sveglia il grafo**) oppure in automatico. L'automatico è spento all'avvio e ha due
protezioni per i crediti: un intervallo minimo in secondi reali tra un ciclo e l'altro (20 s) e un tetto ai cicli
automatici (5, azzerato con **Applica**). Un ciclo di crisi costa in genere 3-6 chiamate al modello.

Nella pagina: l'albero degli agenti si accende come in `/demo` (in esecuzione, già eseguito, in attesa
dell'operatore; il pallino verde indica la leva accesa), la richiesta del Brain compare con i pulsanti, le leve si
possono azionare anche a mano e i passi del grafo scorrono con l'ora simulata.

**Prova consigliata con il modello reale.** Scenario Italia a luglio, qualche giorno a 1 ora/s, poi **Ondata di
calore** e un guasto alla linea Calabria-Sicilia (clic sulla linea, *Provoca un guasto*) e alla centrale di Priolo:
compare carico non servito in Sicilia; premi **Sveglia il grafo** e guarda la richiesta salire fino al Brain.

## Anomalie

Ogni anomalia ha `id`, `tipo`, `gravita` (`critica` o `avviso`), `entita`, `descrizione`, `valore` e `da_ore`
(da quante ore simulate è attiva):

| Tipo | Quando |
|---|---|
| `distacco_carico` (critica) | in una zona la domanda elettrica non è coperta |
| `gas_non_servito` (critica) | in una zona manca gas |
| `pressione_gas_bassa` | pressione sotto 50 bar (critica sotto 46) |
| `congestione` | una linea elettrica è al 98% o più della capacità |
| `riserva_bassa` | in una zona il margine di riserva è sotto il 10% (vedi sotto) |
| `stoccaggio_gas_basso` | stoccaggi sotto il 10% (critica sotto il 3%) |
| `guasto`, `crisi_gas` | un'entità è guasta o un ingresso gas è ridotto |

## API

| Metodo e percorso | Ruolo minimo | Cosa fa |
|---|---|---|
| `GET /energia` | pubblica | la pagina |
| `GET /energia/rete` | tirocinante | topologia (entità, posizioni, capacità) |
| `GET /energia/stato` | tirocinante | esecuzione, valori attuali di tutte le entità, anomalie, eventi, riepilogo |
| `GET /energia/stream?frequenza=4` | tirocinante | lo stesso stato in Server-Sent Events, quando cambia (`limite=N` chiude dopo N messaggi) |
| `GET /energia/storico?scala=passi\|giorni&massimo=600` | tirocinante | serie nazionali: ultimi 7 giorni o medie giornaliere, ricampionate |
| `GET /energia/entita/{id}` | tirocinante | tutti i valori di un'entità, eventi, anomalie, storico orario (14 giorni) |
| `POST /energia/controllo` | medico di guardia | `{"azione": "avvia" \| "pausa" \| "passo" \| "velocita", "velocita": 3600, "passi": 1}` |
| `POST /energia/scenario` | primario | nuova simulazione: `tipo` (`italia`, `generata`), `seed`, `durata_ore`, `passo_minuti`, `inizio`, `zone`, `citta`, `centrali` |
| `POST /energia/evento` | primario | `{"tipo": "guasto", "entita": "centrale:turbigo", "durata_ore": 24}`; tipi: `guasto`, `crisi_gas`, `ondata_calore`, `ondata_freddo`, `calma_vento`, `picco_prezzo_gas`, `ripristino` |
| `POST /energia/leva` | medico di guardia | `{"leva": "rete_gas_gnl_spot", "valore": "ON"}`: comando manuale, registrato nell'audit log |
| `POST /energia/sistema-nervoso/ciclo` | medico di guardia | sveglia il grafo (un ciclo, modello reale); 409 se sta già lavorando |
| `POST /energia/sistema-nervoso/decisione` | medico di guardia (Override: primario) | `{"decisione": "APPROVA" \| "RESPINGI" \| "OVERRIDE", "motivazione": "..."}` |
| `POST /energia/sistema-nervoso/configura` | primario | `{"automatico": true, "intervallo_minimo_s": 20, "cicli_automatici_massimi": 5, "azzera_contatore": true}` |

`GET /energia/stato` e lo stream includono anche `sistema_nervoso` (stato, nodo attivo, nodi visitati, richiesta in
attesa, ultimi passi) e le leve; `GET /energia/rete` include l'albero degli agenti.

La velocità è in secondi simulati per secondo reale (3600 = un'ora al secondo); 0 vuol dire "massima". Gli
identificativi delle entità sono leggibili: `zona` (es. `NORD`), `centrale:turbigo`, `citta:milano`,
`accumulo:edolo`, `linea:NORD-CNOR`, `ingresso:mazara`, `stoccaggio:pianura_padana`, `condotta:SICI-CALA`.

## Prova di carico

```bash
python scripts/stress_energia.py --anni 10
python scripts/stress_energia.py --scenario generata --zone 80 --citta 3000 --centrali 1500 --anni 1 --controlli 24
```

Lo script esegue il motore alla massima velocità, verifica bilanci e limiti fisici (ogni `--controlli` passi) e
riporta velocità, memoria, costo dell'istantanea mandata al frontend e le statistiche finali (`--json` le salva). Esce
con codice 1 se trova violazioni.

Misure sulla macchina di sviluppo (Python 3.14, passo di un'ora):

| Scenario | Entità | Durata | Calcolo | Velocità | Memoria di picco | Istantanea | Violazioni |
|---|---|---|---|---|---|---|---|
| Italia | 117 | 10 anni | 24 s | 3 656 passi/s | 28 MB | 3,9 kB, 0,1 ms | nessuna (controllo a ogni passo) |
| Rete generata 80 zone, 3000 città, 1500 centrali | 4 932 | 1 anno | 72 s | 122 passi/s | 195 MB | 62 kB, 2,5 ms | nessuna (ogni 24 passi) |

Con la rete generata grande la pagina disegna la mappa in circa 10 ms.

I test automatici sono in `tests/test_simulazione_energia.py`: determinismo, bilanci elettrici di zona e nazionali,
bilancio del gas con il linepack, limiti di impianti, linee, condotte e stoccaggi, una crisi gas invernale completa,
ordini di grandezza di un anno di Italia, eventi, esecutore in background, API e permessi.

## Semplificazioni e limiti

- La rete è per zone, non per nodi: niente flussi di potenza (DC o AC), niente perdite di rete, niente tensioni.
- Il dispacciamento è a merito economico con un solo prezzo nazionale, senza vincoli di rampa, minimo tecnico,
  avviamenti o riserva; gli accumuli seguono una regola sul prezzo medio, non un'ottimizzazione.
- Il gas non ha dinamica di pressione lungo i tubi: la pressione di zona segue il linepack.
- Meteo e prezzi sono processi casuali plausibili, non dati storici.
- Lo stato della simulazione è in memoria del processo: si perde al riavvio e vale un solo worker.
- Le reti generate hanno numeri plausibili ma servono a misurare il motore, non a rappresentare un paese.

## Modifiche al framework

Per ospitare un secondo dominio accanto alla smart home:

- `build_graph(..., tools=..., brain_options=...)`: tool del dominio e opzioni del Brain (target e prompt);
- `BrainAgent(managed_targets=..., system_prompt=..., user_prompt_template=...)`: un Brain limitato ai dispositivi del
  proprio dominio, con i propri prompt (hanno la precedenza sul `.env`);
- con più organi radice il Brain li visita tutti nello stesso ciclo, anche dopo aver evaso le escalation (prima si
  fermava dopo il primo);
- `target_critici_brain` nel contesto del grafo protegge un dispositivo solo nel flusso HITL del Brain.

I test sono in `tests/test_agenti_energia.py` (modello finto): ciclo di crisi con approvazione, rifiuto, arco
riflesso senza chiamate, errore del modello, stimoli, tetto dei cicli automatici, API.
