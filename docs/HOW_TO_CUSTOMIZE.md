# Guida alla Personalizzazione: LangBrain

Questa guida spiega come estendere e personalizzare il boilerplate per adattarlo alla tua architettura IoT o a qualsiasi altro dominio gerarchico.

---

## 0. Installazione e Avvio del Server

### Avvio locale su Windows PowerShell

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --reload
```

Configura almeno un provider LLM nel file `.env` prima di eseguire cicli che invocano gli agenti. La documentazione interattiva sarà disponibile su `http://127.0.0.1:8000/docs`.

Per un modello OpenAI-compatible avviato direttamente su Windows usa, per esempio, `LOCAL_MODEL_BASE_URL=http://127.0.0.1:8080/v1`. Se LangBrain gira in Docker e il modello è sull'host Windows, imposta `LOCAL_MODEL_DOCKER_BASE_URL=http://host.docker.internal:8080/v1`; se il modello è su un'altra macchina della LAN, usa lo stesso URL LAN in entrambe le variabili. `LOCAL_MODEL` deve essere l'ID esatto restituito da `GET <base_url>/models`. Il server del modello deve essere già avviato e raggiungibile.

Il timeout delle richieste LLM è `40` secondi per default. Puoi modificarlo nel `.env`, poi riavviare o ricreare il server:

```dotenv
MAO_TIMEOUT_SECONDS=40
```

### Avvio con Docker Compose

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Il servizio viene pubblicato su `http://127.0.0.1:8000` per default. Puoi cambiare la porta host impostando `LANGBRAIN_PORT` in `.env`. SQLite viene conservato nel volume Docker `langbrain_data`. Il container usa intenzionalmente un solo worker perché checkpointer, registry dei tool e configurazione HITL sono ancora in memoria di processo.

Per arrestare il servizio:

```powershell
docker compose down
```

Per eliminare anche il volume SQLite, solo quando vuoi cancellare definitivamente i dati:

```powershell
docker compose down --volumes
```

---

## 1. Architettura Gerarchica N-Livelli

Il sistema adotta una struttura ricorsiva ad albero **Padre-Figlio**:

$$\text{Cervello (Brain - Livello 0)} \longrightarrow \text{Organo (Livello 1)} \longrightarrow \text{Componente dell'Organo (Livello 2)} \longrightarrow \text{Sotto-Componente (Livello N)}$$

### Principi Chiave:
- **Cervello (Brain):** Orchestratore Supremo di Livello 0 con priorità massima (`1000.0`). Possiede la visione globale ed effettua la riconciliazione finale dei conflitti.
- **Organo (es. `organ_security`, `organ_climate`):** Agente di Livello 1 responsabile di una macro-area. Può gestire direttamente dei tool oppure coordinare dei componenti figli.
- **Componente dell'Organo (es. `component_door_lock`, `component_alarm`):** Agente di Livello 2 o superiore dedicato ad una specifica periferica o compito.
- **Escalation Ricorsiva:** Se un componente o organo rileva un conflitto non risolvibile localmente, genera un'`EscalationItem` verso il proprio `parent_agent_name`.

---

## 2. Mappatura Completa Endpoints API REST

Tutti gli endpoint disponibili in FastAPI ([`app/api/main.py`](../app/api/main.py)):

### 📌 1. Sistema & Grafo Agenti

#### `GET /`
- **Descrizione:** Health check root dell'API.
- **Risposta (200):**
  ```json
  { "status": "ok", "version": "2.1.0", "architecture": "Hierarchical N-Level (Brain -> Organs -> Components)" }
  ```

#### `POST /graph/run`
- **Descrizione:** Esegue un singolo ciclo del grafo agenti LangGraph per uno specifico `thread_id`.
- **Request Body:**
  ```json
  {
    "sensor_readings": [
      { "sensor_id": "temp_living_room", "agent_owner": "api", "value": "30.0", "unit": "°C" }
    ],
    "force_next_agent": "brain",
    "thread_id": "test-thread-123"
  }
  ```
- **Risposta (200):**
  ```json
  {
    "next_agent": "END",
    "last_message": "[agent_climate] AC attivata su ac_living_room: OFF -> 22.5°C.",
    "pending_escalations": []
  }
  ```

#### `GET /graph/state`
- **Descrizione:** Restituisce lo stato attuale del grafo dal checkpointer, indicando eventuali `interrupt` pendenti.
- **Query Params:** `thread_id` (opzionale)
- **Risposta (200):**
  ```json
  {
    "next": ["brain"],
    "values": { "next_agent": "brain", "pending_escalations": [] },
    "tasks": [],
    "is_interrupted": false
  }
  ```

#### `POST /graph/resume`
- **Descrizione:** Riprende l'esecuzione del grafo sospeso da un interrupt **Human-in-the-Loop (HITL)**.
- **Request Body:**
  ```json
  {
    "decision": "APPROVA",
    "reasoning": "Approvato dall'utente tramite dashboard",
    "thread_id": "api_session"
  }
  ```
- **Risposta (200):**
  ```json
  {
    "status": "resumed",
    "decision_applied": "APPROVA",
    "last_message": "[Brain] Escalation APPROVATA per front_door_lock.",
    "next_agent": "END"
  }
  ```

#### `POST /graph/health-check`
- **Descrizione:** Invoca l'analisi macro di routine dell'Orchestratore Supremo (`check_body_status`).
- **Risposta (200):**
  ```json
  { "result": "[Brain] Macro Check Completato: STATUS: OK" }
  ```

---

### 🤖 2. Gestione Sotto-Agenti Dinamici (N-Livelli)

#### `GET /agents`
- **Descrizione:** Elenca tutti i sotto-agenti registrati nel sistema.
- **Risposta (200):**
  ```json
  {
    "count": 2,
    "agents": [
      { "name": "organ_climate", "level": 1, "parent_agent_name": "Brain", "managed_targets": ["ac_living_room"] }
    ]
  }
  ```

#### `GET /agents/hierarchy`
- **Descrizione:** Restituisce l'albero gerarchico completo: `Cervello (Brain) -> Organi -> Componenti`.
- **Risposta (200):**
  ```json
  {
    "root": "Brain",
    "title": "Gerarchia IoT: Cervello -> Organi -> Componenti dell'Organo",
    "tree": {
      "name": "Brain",
      "level": 0,
      "children": [
        {
          "name": "organ_security",
          "level": 1,
          "children": [
            { "name": "component_door_lock", "level": 2, "children": [] }
          ]
        }
      ]
    }
  }
  ```

#### `POST /agents/create`
- **Descrizione:** Registra a runtime un nuovo Organo o Componente dell'Organo e ricompila il grafo.
- **Request Body:**
  ```json
  {
    "agent_definition": "{\"name\": \"organ_security\", \"level\": 1, \"parent_agent_name\": \"Brain\", \"managed_targets\": [\"alarm_system\"], \"sub_agent_names\": [\"component_door_lock\"], \"system_prompt_template\": \"Sei l'organo di sicurezza...\", \"priority_weight\": 500.0}"
  }
  ```
- **Risposta (200):**
  ```json
  {
    "status": "registered_and_compiled",
    "agent_name": "organ_security",
    "level": 1,
    "parent_agent_name": "Brain",
    "managed_targets": ["alarm_system"],
    "graph_node_active": true
  }
  ```

#### `DELETE /agents/{agent_name}`
- **Descrizione:** Rimuove un sotto-agente dal registry e ricompila la topologia del grafo.
- **Risposta (200):**
  ```json
  { "status": "deleted", "agent_name": "component_door_lock" }
  ```

---

### 🛠️ 3. Gestione Tool IoT (Hardware/Simulati)

#### `GET /tools`
- **Descrizione:** Elenca tutti i tool IoT registrati ed il loro valore attuale.
- **Risposta (200):**
  ```json
  {
    "ac_living_room": { "value": "22.5°C", "unit": "°C" },
    "front_door_lock": { "value": "LOCKED", "unit": "" }
  }
  ```

#### `GET /tools/{device_id}`
- **Descrizione:** Legge il valore corrente di uno specifico tool.
- **Risposta (200):**
  ```json
  { "device_id": "ac_living_room", "value": "22.5°C", "unit": "°C" }
  ```

#### `POST /tools`
- **Descrizione:** Scrive direttamente il valore di un tool (bypassando la decisione degli agenti).
- **Request Body:**
  ```json
  { "target": "ac_living_room", "value": "OFF" }
  ```
- **Risposta (200):**
  ```json
  { "device_id": "ac_living_room", "new_value": "OFF" }
  ```

---

### 🗄️ 4. Database, Conflitti & Event-Driven Unblock

#### `GET /events`
- **Descrizione:** Recupera lo storico degli eventi dal DB audit log nella finestra specificata.
- **Query Params:** `window_minutes` (default: 240)
- **Risposta (200):**
  ```json
  { "count": 5, "events": [...] }
  ```

#### `POST /events/seed-conflict`
- **Descrizione:** Inserisce un evento di conflitto nel DB per simulare uno scenario di escalation.
- **Request Body:**
  ```json
  {
    "actor": "agent_security",
    "action": "FORCE_SHUTDOWN",
    "target": "ac_living_room",
    "old_value": "22.5°C",
    "new_value": "OFF",
    "reasoning": "Simulazione conflitto via API"
  }
  ```

#### `DELETE /events/reset-conflicts/{target}`
- **Descrizione:** Marca come risolti (`RESOLVED_`) gli eventi di escalation pendenti per il target.

#### `POST /events/unblock`
- **Descrizione:** Sblocca un dispositivo precedentemente bloccato da un flag (`REJECTED`/`BLOCKED`).
- **Request Body:**
  ```json
  { "target": "ac_living_room", "reasoning": "Finestra chiusa: sblocco manuale via API" }
  ```
- **Risposta (200):**
  ```json
  { "unblocked": true, "target": "ac_living_room", "reasoning": "Finestra chiusa: sblocco manuale via API" }
  ```

#### `DELETE /system/reset`
- **Descrizione:** Esegue un reset totale dell'ambiente: svuota il DB degli eventi/readings, rimuove gli agenti dinamici registrati, ripristina la configurazione HITL ai default e ricompila la topologia del grafo.
- **Risposta (200):**
  ```json
  { "status": "reset_complete", "message": "Database svuotato, registro agenti resettato e grafo ricompilato." }
  ```

---

### 🧠 5. Proxy LLM Centralizzato (MAO)

#### `POST /llm/invoke`
- **Descrizione:** Invoca direttamente il MAO (Model Access Object) con provider e parametri a scelta.
- **Request Body:**
  ```json
  {
    "system_prompt": "Sei un assistente domotico.",
    "user_prompt": "Qual è la temperatura ideale per dormire?",
    "provider": "openrouter",
    "model": "deepseek/deepseek-r1:free",
    "temperature": 0.0,
    "max_tokens": 512,
    "enable_reasoning": true,
    "fallback_on_error": false
  }
  ```
- **Risposta (200):**
  ```json
  { "response": "La temperatura consigliata è tra 18°C e 20°C.", "provider": "openrouter" }
  ```
- **Errori provider:** se nessun provider disponibile completa la richiesta, l'API restituisce `503 Service Unavailable` senza traceback ASGI. I provider cloud con chiavi mancanti/placeholder non vengono tentati. `fallback_on_error` vale `false` per default sul proxy diretto.

---

## 3. Creare un Nuovo Agente in Codice Python

Per creare un agente personalizzato in Python:

```python
from app.agents.base_agent import BaseAgent
from app.graph.state import GraphState
from typing import Any

class MyCustomAgent(BaseAgent):
    def __init__(self, tools: dict[str, Any] | None = None):
        super().__init__(
            name="agent_lighting",
            managed_targets=["living_room_lights"],
            conflict_window_minutes=15,
            priority_weight=50.0
        )
        self.tools = tools or {}

    async def process(
        self,
        state: GraphState,
        recent_events: list[dict],
        relevant_readings: list[dict],
        agent_escalations: list[dict]
    ) -> dict[str, Any]:
        # 1. Analisi dello stato e dei sensori
        # 2. Invocazione modello via self.ask_brain()
        # 3. Azionamento tool via self.apply_status() o Escalation verso il Padre
        return {"next_agent": "Brain"}
```

---

## 4. Creare e Registrare Nuovi Tool IoT

Tutti i tool ereditano da `BaseTool`:

```python
from app.tools.baseTool import BaseTool

class SmartBlindTool(BaseTool):
    def __init__(self, target_device: str = "living_room_blinds"):
        super().__init__(target_device=target_device)
        self.position = 0  # 0% chiuso, 100% aperto

    async def get_tool_value(self):
        return f"{self.position}%"

    async def set_tool_value(self, value):
        self.position = int(str(value).replace("%", ""))
        return True
```

Registra il tool in `app/tools/sensor_tools.py` per renderlo disponibile a tutti gli agenti tramite il singleton global registry.

---

## 5. Personalizzazione dei Prompt (Brain & Sotto-Agenti)

Puoi personalizzare sia le istruzioni di sistema (System Prompt) che la struttura dei dati inviati al modello (User Prompt) a qualsiasi livello della gerarchia.

### A. Personalizzare i Prompt dell'Orchestratore Supremo (Brain) via `.env`

Nel file `.env` puoi sovrascrivere direttamente i prompt del `BrainAgent`:

```env
# System Prompt dell'Orchestratore
BRAIN_SYSTEM_PROMPT="Sei l'Orchestratore Supremo della Smart Home. Hai ricevuto un'escalation da un sotto-agente per un conflitto o un'anomalia. Valuta il contesto e decidi se APPROVARE o RESPINGERE l'azione.\nFormato Risposta:\nDECISIONE: [APPROVA|RESPINGI]\nMOTIVAZIONE: [spiegazione]"

# Template del User Prompt con segnaposto dinamici
BRAIN_USER_PROMPT_TEMPLATE="Agente Richiedente: {source}\nDispositivo Target: {target}\nAzione Proposta: {action}\nMotivo Escalation: {reason}\nLetture Sensori Reali: {readings}\nStorico Eventi Recenti: {recent_events}\nQual è la risoluzione corretta?"
```

#### Segnaposto disponibili per `BRAIN_USER_PROMPT_TEMPLATE`:
| Segnaposto | Descrizione |
|---|---|
| `{source}` | Nome dell'agente che ha inviato l'escalation (es. `component_door_lock`) |
| `{target}` | Dispositivo/target interessato (es. `front_door_lock`) |
| `{action}` | Azione proposta dal sotto-agente (es. `22.5°C`, `LOCKED`) |
| `{reason}` | Motivazione/dettagli forniti dal sotto-agente |
| `{readings}` | Mappa delle letture in tempo reale per quel dispositivo |
| `{recent_events}` | Lista degli eventi dal DB audit log nella finestra temporale |

---

### B. Personalizzare i Prompt dei Sotto-Agenti via API (`POST /agents/create`)

Quando crei o aggiorni un sotto-agente via API REST o nel file di configurazione JSON, puoi specificare `system_prompt_template` e `user_prompt_template`:

```json
{
  "agent_definition": "{\"name\": \"organ_respiratory\", \"level\": 1, \"parent_agent_name\": \"Brain\", \"managed_targets\": [\"oxygen_regulator\"], \"system_prompt_template\": \"Sei l'Organo Respiratorio. Monitora la SpO2 ed aziona i regolatori di ossigeno.\\nFormato: DECISIONE: [ACTION|ESCALATE|NONE]\", \"user_prompt_template\": \"Target: {target}\\nStato Attuale: {current_status}\\nConflitto DB: {has_conflict}\\nLetture: {relevant_readings}\\nQual è la decisione?\"}"
}
```

#### Segnaposto disponibili per `user_prompt_template` nei Sotto-Agenti:
| Segnaposto | Descrizione |
|---|---|
| `{target}` | Target primario controllato dall'agente (es. `ac_living_room`) |
| `{current_status}` | Stato letto in tempo reale dal tool IoT (es. `OFF`, `28.5°C`) |
| `{has_conflict}` | `True` se il DB rileva un conflitto non ancora risolto |
| `{recently_reconciled}` | `True` se il target è stato riconciliato recentemente |
| `{relevant_readings}` | Letture dei sensori filtrate per questo specifico agente |
| `{recent_events}` | Eventi recenti di audit log per i target gestiti |

---

## 6. Configurazione Dinamica Human-in-the-Loop (HITL) e TTL

Gli interrupt HITL possono essere collocati **ovunque nel flusso del grafo** tramite il wrapper universale in [`app/graph/builder.py`](../app/graph/builder.py) e gestiti dinamicamente via API senza riavviare il sistema.

### A. Configurare i punti di Interrupt HITL ed Attesa Massima via API

#### `GET /hitl/config`
- **Descrizione:** Legge la configurazione HITL attiva.
- **Risposta (200):**
  ```json
  {
    "hitl_all": false,
    "hitl_nodes": ["organ_security", "brain"],
    "hitl_targets": ["front_door_lock", "cardiac_pacemaker"],
    "hitl_actions": ["FORCE_SHUTDOWN", "UNLOCK"],
    "max_wait_seconds": 120
  }
  ```

#### `POST /hitl/config`
- **Descrizione:** Imposta o aggiorna a runtime le regole di intercettazione e l'attesa massima (`max_wait_seconds`).
- **Request Body:**
  ```json
  {
    "hitl_all": false,
    "hitl_nodes": ["organ_security"],
    "hitl_targets": ["cardiac_pacemaker"],
    "hitl_actions": ["CRITICAL_ARHYTHMIA_ESCALATION"],
    "max_wait_seconds": 300
  }
  ```

### B. Gestione dell'Interrupt e Resume del Grafo

Quando l'esecuzione del grafo incontra un punto protetto:
1. LangGraph sospende il ciclo invocando `interrupt()`.
2. Puoi rilevare lo stato di pausa con `GET /graph/state`.
3. Ripristina l'esecuzione con `POST /graph/resume`, scegliendo una delle tre modalità:

#### Tre modalità di decisione

| `decision` | Effetto |
|---|---|
| `APPROVA` | Il Brain scrive `RECONCILED_<action>` nel DB per ogni conflitto pendente e termina il ciclo. Il tool **non viene** azionato fisicamente. |
| `RESPINGI` | Il Brain scrive `REJECTED_<action>` nel DB, il device rimane bloccato fino a TTL o unblock manuale. |
| `OVERRIDE` | **God Mode Semantico**: il campo `reasoning` viene inviato al MAO con il prompt di Arbitrato Semantico. Il MAO traduce la frase in linguaggio naturale in un array JSON di comandi, eseguiti fisicamente via `force_execute_tool` che bypassa tutti i lock di priorità. |

#### Esempio — APPROVA
```http
POST /graph/resume
Content-Type: application/json

{"decision": "APPROVA", "reasoning": "Intervento approvato via Dashboard", "thread_id": "api_session"}
```

#### Esempio — RESPINGI
```http
POST /graph/resume
Content-Type: application/json

{"decision": "RESPINGI", "reasoning": "Sicurezza prioritaria, non modificare", "thread_id": "api_session"}
```

#### Esempio — OVERRIDE (God Mode Semantico)
Puoi scrivere la direttiva interamente in linguaggio naturale; il MAO si occupa di tradurla:
```http
POST /graph/resume
Content-Type: application/json

{
  "decision": "OVERRIDE",
  "reasoning": "Ignora il blocco dell'energia: mia nonna ha freddo. Accendi la stufa a 22 gradi e spegni la pompa della piscina.",
  "thread_id": "api_session"
}
```

Il sistema risponde con il log delle azioni fisicamente eseguite:
```json
{
  "status": "resumed",
  "decision_applied": "OVERRIDE",
  "last_message": "[Brain_Override] ✓ ESEGUITO — UNBLOCK_AND_SET su 'heater_bedroom' → '22'\n[Brain_Override] ✓ ESEGUITO — TURN_OFF su 'pool_pump' → 'OFF'"
}
```

> [!NOTE]
> Il System Prompt di Arbitrato Semantico usato dal MAO è definito in [`app/graph/orchestrator.py`](../app/graph/orchestrator.py) come `_OVERRIDE_SYSTEM_PROMPT`.
> Ogni azione OVERRIDE viene auditata nel DB con `actor: "Brain_Override"` e l'azione originale (es. `UNBLOCK_AND_SET`) per tracciabilità completa.

---


## 7. Gestione TTL (Time-To-Live) per i Blocchi
I flag di controllo (es. `REJECTED`, `BLOCKED`) scadono automaticamente dopo `FLAG_TTL_MINUTES` (default: 60 min).
Per sbloccare manualmente un dispositivo congelato:
```http
POST /events/unblock
Content-Type: application/json

{
  "target": "ac_living_room",
  "reasoning": "Finestra chiusa: sblocco manuale via API"
}
```

---

## 8. Smoke Test API Completo

Gli script completi [`smoke_test_full.ps1`](../smoke_test_full.ps1) e [`smoke_test_full_v2.ps1`](../smoke_test_full_v2.ps1) verificano prima la raggiungibilità di `/v1/models`, usano UTF-8 esplicito anche per le risposte PowerShell 5.1 e configurano un interrupt HITL sul target `ac_living_room` prima del resume `OVERRIDE`. Un resume può esercitare `OVERRIDE` soltanto se lo stesso `thread_id` ha davvero un interrupt pendente.

Lo smoke test API più compatto per Windows è disponibile in [`docs/API_SMOKE_TEST_WINDOWS.ps1`](API_SMOKE_TEST_WINDOWS.ps1). È presente anche la [versione Bash](API_SMOKE_TEST.md).

---

## 9. Contratto degli Stati e delle Azioni del Dominio

Il core conserva `action`, `old_value` e `new_value` come valori estensibili e non impone una normalizzazione universale. Ogni sviluppatore che implementa un tool o un agente deve:

- definire gli stati fisici ammessi dal proprio dispositivo;
- tradurre le azioni simboliche del proprio dominio negli stati fisici corretti;
- rifiutare valori sconosciuti prima dell'attuazione;
- aggiungere test per transizioni valide, invalide e idempotenti;
- decidere come risolvere flag interni quali `REJECTED` e `BLOCKED`.

L'health check segnala questi flag come `MACRO_ADJUSTMENT_REQUIRED`, ma intenzionalmente non sceglie né applica un valore fisico sostitutivo.


