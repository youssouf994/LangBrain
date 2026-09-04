# Smoke test API di LangBrain

Questo script crea una gerarchia `Brain → organ_security → component_door_lock/component_alarm` e verifica i tre bug corretti: caricamento degli eventi DB nel percorso del grafo, routing case-insensitive e propagazione ricorsiva delle escalation. Copre inoltre registry, gerarchia, tool, audit log, unblock, HITL, stato/checkpoint, health check opzionale, proxy LLM opzionale, eliminazione agenti e reset.

Prerequisiti: server avviato dalla root con `python -m uvicorn app.api.main:app --reload`, `curl`, `jq` e almeno un provider LLM configurato per i cicli degli agenti. Il timeout MAO è configurabile con `MAO_TIMEOUT_SECONDS` e vale 40 secondi per default. Imposta `RUN_LLM_TESTS=1` per includere le chiamate LLM esplicite, che possono consumare quota o risorse del modello locale.

```bash
#!/usr/bin/env bash
set -euo pipefail

API_URL="${API_URL:-http://127.0.0.1:8000}"

request() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  printf '\n>>> %s %s\n' "$method" "$path"
  if [[ -n "$body" ]]; then
    curl --fail-with-body --silent --show-error \
      --request "$method" \
      --header "Content-Type: application/json" \
      --data "$body" \
      "$API_URL$path" | jq .
  else
    curl --fail-with-body --silent --show-error \
      --request "$method" \
      "$API_URL$path" | jq .
  fi
}

create_agent() {
  local definition="$1"
  request POST /agents/create "$(jq -cn --arg definition "$definition" '{agent_definition:$definition}')"
}

printf '=== 1. API e stato pulito ===\n'
request GET /
request DELETE /system/reset

printf '=== 2. Creazione gerarchia N-livelli ===\n'
create_agent "$(jq -cn '{name:"organ_security",level:1,parent_agent_name:"Brain",managed_targets:["front_door_lock","alarm_system"],sub_agent_names:["component_door_lock","component_alarm"],system_prompt_template:"Gestisci la sicurezza. Rispondi DECISIONE: NONE se non ci sono anomalie.",priority_weight:500.0}')"
create_agent "$(jq -cn '{name:"component_door_lock",level:2,parent_agent_name:"organ_security",managed_targets:["front_door_lock"],sub_agent_names:[],system_prompt_template:"Controlla la serratura. Rispondi nel formato DECISIONE: [ACTION|ESCALATE|NONE].",priority_weight:200.0}')"
create_agent "$(jq -cn '{name:"component_alarm",level:2,parent_agent_name:"organ_security",managed_targets:["alarm_system"],sub_agent_names:[],system_prompt_template:"Controlla l allarme. Rispondi nel formato DECISIONE: [ACTION|ESCALATE|NONE].",priority_weight:200.0}')"
request GET /agents
request GET /agents/hierarchy

printf '=== 3. Bug #3: il padre con target diretti visita entrambi i figli una volta ===\n'
request POST /graph/run "$(jq -cn '{sensor_readings:[],force_next_agent:"organ_security",thread_id:"smoke-hierarchy"}')"
request GET '/graph/state?thread_id=smoke-hierarchy'
# In values.messages devono comparire le deleghe a component_door_lock e component_alarm.

printf '=== 4. Bug #2: routing case-insensitive verso un nodo registrato ===\n'
request POST /graph/run "$(jq -cn '{sensor_readings:[],force_next_agent:"ORGAN_SECURITY",thread_id:"smoke-case-routing"}')"
request GET '/graph/state?thread_id=smoke-case-routing'
# Il ciclo deve attraversare organ_security, non terminare subito per nodo sconosciuto.

printf '=== 5. Bug #1 + propagazione: evento DB letto dal componente e risalita al Brain ===\n'
request POST /events/seed-conflict "$(jq -cn '{actor:"user_manual",action:"SECURITY_LOCK",target:"front_door_lock",old_value:"LOCKED",new_value:"LOCKED",reasoning:"Smoke test reconciliation DB"}')"
request POST /graph/run "$(jq -cn '{sensor_readings:[],force_next_agent:"component_door_lock",thread_id:"smoke-db-reconciliation"}')"
request GET '/graph/state?thread_id=smoke-db-reconciliation'
request POST /graph/resume "$(jq -cn '{decision:"APPROVA",reasoning:"Approva la reconciliation dello smoke test",thread_id:"smoke-db-reconciliation"}')"
request GET '/events?window_minutes=30'
# Negli eventi devono apparire ESCALATION_PROPOSED del componente e la risoluzione del Brain.

printf '=== 6. Tool on-demand, scrittura, lettura e audit/unblock ===\n'
request GET /tools
request GET /tools/pool_pump
request POST /tools "$(jq -cn '{target:"pool_pump",value:"ON"}')"
request GET /tools/pool_pump
request POST /events/unblock "$(jq -cn '{target:"front_door_lock",reasoning:"Smoke test concluso"}')"
request DELETE /events/reset-conflicts/front_door_lock
request GET '/events?window_minutes=30'

printf '=== 7. HITL: configurazione, interrupt, stato e resume senza chiamata LLM ===\n'
request POST /hitl/config "$(jq -cn '{hitl_all:false,hitl_nodes:["organ_security"],hitl_targets:[],hitl_actions:[],max_wait_seconds:120}')"
request GET /hitl/config
request POST /graph/run "$(jq -cn '{sensor_readings:[],force_next_agent:"organ_security",thread_id:"smoke-hitl"}')"
request GET '/graph/state?thread_id=smoke-hitl'
request POST /graph/resume "$(jq -cn '{decision:"RESPINGI",reasoning:"Rifiuto controllato dello smoke test",thread_id:"smoke-hitl"}')"
request POST /hitl/config "$(jq -cn '{hitl_all:false,hitl_nodes:[],hitl_targets:[],hitl_actions:[],max_wait_seconds:120}')"

if [[ "${RUN_LLM_TESTS:-0}" == "1" ]]; then
  printf '=== 8. Funzioni LLM opzionali ===\n'
  request POST /llm/invoke "$(jq -cn '{system_prompt:"Rispondi soltanto OK.",user_prompt:"Health check LangBrain",temperature:0,max_tokens:16,enable_reasoning:false,fallback_on_error:false}')"
  request POST /graph/health-check
fi

printf '=== 9. Eliminazione agenti e reset finale ===\n'
request DELETE /agents/component_alarm
request DELETE /agents/component_door_lock
request DELETE /agents/organ_security
request GET /agents
request DELETE /system/reset

printf '\nSmoke test LangBrain completato.\n'
```
