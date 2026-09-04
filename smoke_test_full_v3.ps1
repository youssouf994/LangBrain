$baseUrl = "http://127.0.0.1:8000"

# 1. Health check & Reset
Invoke-RestMethod -Uri "$baseUrl/" -Method Get
Invoke-RestMethod -Uri "$baseUrl/system/reset" -Method Delete

# 2. Test MAO con provider locale
Invoke-RestMethod -Uri "$baseUrl/llm/invoke" -Method Post -ContentType "application/json" -Body (@{
    system_prompt = "Sei un assistente."
    user_prompt = "Test"
    provider = "local"
    model = "local_model_id"
    temperature = 0.0
    max_tokens = 512
    enable_reasoning = $true
    fallback_on_error = $false
} | ConvertTo-Json -Depth 5)

# 3. Livello 1: Organo (aggiunto managed_targets)
Invoke-RestMethod -Uri "$baseUrl/agents/create" -Method Post -ContentType "application/json" -Body (@{
    agent_definition = '{"name": "organ_security", "level": 1, "parent_agent_name": "Brain", "managed_targets": ["security_perimeter"], "sub_agent_names": ["component_access"]}'
} | ConvertTo-Json)

# 4. Livello 2: Componente (aggiunto managed_targets)
Invoke-RestMethod -Uri "$baseUrl/agents/create" -Method Post -ContentType "application/json" -Body (@{
    agent_definition = '{"name": "component_access", "level": 2, "parent_agent_name": "organ_security", "managed_targets": ["access_control"], "sub_agent_names": ["subcomponent_vault"]}'
} | ConvertTo-Json)

# 5. Livello 3: Sotto-Componente (già corretto, riportato per completezza)
Invoke-RestMethod -Uri "$baseUrl/agents/create" -Method Post -ContentType "application/json" -Body (@{
    agent_definition = '{"name": "subcomponent_vault", "level": 3, "parent_agent_name": "component_access", "managed_targets": ["vault_door"]}'
} | ConvertTo-Json)

# 6. Lettura registry e gerarchia
Invoke-RestMethod -Uri "$baseUrl/agents" -Method Get
Invoke-RestMethod -Uri "$baseUrl/agents/hierarchy" -Method Get

# 7. Scrittura e Lettura Tools
Invoke-RestMethod -Uri "$baseUrl/tools" -Method Post -ContentType "application/json" -Body (@{target="vault_door"; value="LOCKED"} | ConvertTo-Json)
Invoke-RestMethod -Uri "$baseUrl/tools" -Method Get
Invoke-RestMethod -Uri "$baseUrl/tools/vault_door" -Method Get

# 8. Configurazione HITL
Invoke-RestMethod -Uri "$baseUrl/hitl/config" -Method Post -ContentType "application/json" -Body (@{
    hitl_all = $false
    hitl_nodes = @("brain", "organ_security")
    hitl_targets = @("vault_door")
    hitl_actions = @("FORCE_OPEN")
    max_wait_seconds = 300
} | ConvertTo-Json -Depth 5)
Invoke-RestMethod -Uri "$baseUrl/hitl/config" -Method Get

# 9. Iniezione conflitto e Audit Log
Invoke-RestMethod -Uri "$baseUrl/events/seed-conflict" -Method Post -ContentType "application/json" -Body (@{
    actor = "subcomponent_vault"
    action = "FORCE_OPEN"
    target = "vault_door"
    old_value = "LOCKED"
    new_value = "OPEN"
    reasoning = "Test anomalia per escalation al Livello 0"
} | ConvertTo-Json)
Invoke-RestMethod -Uri "$baseUrl/events?window_minutes=120" -Method Get

# 10. Esecuzione Grafo e Gestione Interrupt (HITL)
Invoke-RestMethod -Uri "$baseUrl/graph/run" -Method Post -ContentType "application/json" -Body (@{
    sensor_readings = @()
    force_next_agent = "subcomponent_vault"
    thread_id = "test_flow"
} | ConvertTo-Json -Depth 5)
Invoke-RestMethod -Uri "$baseUrl/graph/state?thread_id=test_flow" -Method Get
Invoke-RestMethod -Uri "$baseUrl/graph/resume" -Method Post -ContentType "application/json" -Body (@{
    decision = "OVERRIDE"
    reasoning = "Sblocca immediatamente la porta del caveau e mantieni la sicurezza attiva."
    thread_id = "test_flow"
} | ConvertTo-Json)

# 11. Pulizia conflitti e rimozione agente
Invoke-RestMethod -Uri "$baseUrl/events/reset-conflicts/vault_door" -Method Delete
Invoke-RestMethod -Uri "$baseUrl/events/unblock" -Method Post -ContentType "application/json" -Body (@{
    target = "vault_door"
    reasoning = "Ripristino manuale post-test"
} | ConvertTo-Json)
Invoke-RestMethod -Uri "$baseUrl/graph/health-check" -Method Post
Invoke-RestMethod -Uri "$baseUrl/agents/subcomponent_vault" -Method Delete