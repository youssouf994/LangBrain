$ErrorActionPreference = "Stop"
$ApiUrl = if ($env:LANGBRAIN_API_URL) { $env:LANGBRAIN_API_URL.TrimEnd("/") } else { "http://127.0.0.1:8000" }
$RunLlmTests = $env:RUN_LLM_TESTS -eq "1"

function Invoke-LangBrainApi {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("GET", "POST", "DELETE")]
        [string]$Method,

        [Parameter(Mandatory = $true)]
        [string]$Path,

        [hashtable]$Body
    )

    Write-Host "`n>>> $Method $Path" -ForegroundColor Cyan
    $parameters = @{
        Method = $Method
        Uri = "$ApiUrl$Path"
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json; charset=utf-8"
        $json = $Body | ConvertTo-Json -Depth 20 -Compress
        $parameters.Body = [System.Text.Encoding]::UTF8.GetBytes($json)
    }

    $webResponse = Invoke-WebRequest -UseBasicParsing @parameters
    $stream = $webResponse.RawContentStream
    if ($stream.CanSeek) { $stream.Position = 0 }
    $bytes = New-Object byte[] $stream.Length
    [void]$stream.Read($bytes, 0, $bytes.Length)
    $jsonResponse = [System.Text.Encoding]::UTF8.GetString($bytes)
    $response = if ([string]::IsNullOrWhiteSpace($jsonResponse)) { $null } else { $jsonResponse | ConvertFrom-Json }
    Write-Host ($response | ConvertTo-Json -Depth 20)
    return $response
}

function New-LangBrainAgent {
    param([Parameter(Mandatory = $true)][hashtable]$Definition)

    $agentDefinition = $Definition | ConvertTo-Json -Depth 20 -Compress
    Invoke-LangBrainApi -Method POST -Path "/agents/create" -Body @{
        agent_definition = $agentDefinition
    } | Out-Null
}

Write-Host "=== 1. API e stato pulito ===" -ForegroundColor Yellow
Invoke-LangBrainApi -Method GET -Path "/" | Out-Null
Invoke-LangBrainApi -Method DELETE -Path "/system/reset" | Out-Null

Write-Host "=== 2. Creazione gerarchia Brain -> organo -> componenti ===" -ForegroundColor Yellow
New-LangBrainAgent -Definition @{
    name = "organ_security"
    level = 1
    parent_agent_name = "Brain"
    managed_targets = @("front_door_lock", "alarm_system")
    sub_agent_names = @("component_door_lock", "component_alarm")
    system_prompt_template = "Gestisci la sicurezza. Rispondi DECISIONE: NONE se non ci sono anomalie."
    priority_weight = 500.0
}
New-LangBrainAgent -Definition @{
    name = "component_door_lock"
    level = 2
    parent_agent_name = "organ_security"
    managed_targets = @("front_door_lock")
    sub_agent_names = @()
    system_prompt_template = "Controlla la serratura. Rispondi nel formato DECISIONE: [ACTION|ESCALATE|NONE]."
    priority_weight = 200.0
}
New-LangBrainAgent -Definition @{
    name = "component_alarm"
    level = 2
    parent_agent_name = "organ_security"
    managed_targets = @("alarm_system")
    sub_agent_names = @()
    system_prompt_template = "Controlla l'allarme. Rispondi nel formato DECISIONE: [ACTION|ESCALATE|NONE]."
    priority_weight = 200.0
}
Invoke-LangBrainApi -Method GET -Path "/agents" | Out-Null
Invoke-LangBrainApi -Method GET -Path "/agents/hierarchy" | Out-Null

Write-Host "=== 3. Bug #3: visita dei figli con target diretti ===" -ForegroundColor Yellow
Invoke-LangBrainApi -Method POST -Path "/graph/run" -Body @{
    sensor_readings = @()
    force_next_agent = "organ_security"
    thread_id = "smoke-hierarchy"
} | Out-Null
$hierarchyState = Invoke-LangBrainApi -Method GET -Path "/graph/state?thread_id=smoke-hierarchy"
Write-Host "Verifica in values.messages le deleghe a component_door_lock e component_alarm." -ForegroundColor Green

Write-Host "=== 4. Bug #2: routing case-insensitive ===" -ForegroundColor Yellow
Invoke-LangBrainApi -Method POST -Path "/graph/run" -Body @{
    sensor_readings = @()
    force_next_agent = "ORGAN_SECURITY"
    thread_id = "smoke-case-routing"
} | Out-Null
$caseState = Invoke-LangBrainApi -Method GET -Path "/graph/state?thread_id=smoke-case-routing"
Write-Host "Il ciclo deve attraversare organ_security anziché terminare per nodo sconosciuto." -ForegroundColor Green

Write-Host "=== 5. Bug #1 e propagazione: DB -> componente -> organo -> Brain ===" -ForegroundColor Yellow
Invoke-LangBrainApi -Method POST -Path "/events/seed-conflict" -Body @{
    actor = "user_manual"
    action = "SECURITY_LOCK"
    target = "front_door_lock"
    old_value = "LOCKED"
    new_value = "LOCKED"
    reasoning = "Smoke test reconciliation DB"
} | Out-Null
Invoke-LangBrainApi -Method POST -Path "/graph/run" -Body @{
    sensor_readings = @()
    force_next_agent = "component_door_lock"
    thread_id = "smoke-db-reconciliation"
} | Out-Null
$reconciliationState = Invoke-LangBrainApi -Method GET -Path "/graph/state?thread_id=smoke-db-reconciliation"
if ($reconciliationState.is_interrupted) {
    Invoke-LangBrainApi -Method POST -Path "/graph/resume" -Body @{
        decision = "APPROVA"
        reasoning = "Approva la reconciliation dello smoke test"
        thread_id = "smoke-db-reconciliation"
    } | Out-Null
}
$events = Invoke-LangBrainApi -Method GET -Path "/events?window_minutes=30"
Write-Host "Verifica ESCALATION_PROPOSED del componente e la risoluzione del Brain." -ForegroundColor Green

Write-Host "=== 6. Tool on-demand, lettura, scrittura, unblock e audit ===" -ForegroundColor Yellow
Invoke-LangBrainApi -Method GET -Path "/tools" | Out-Null
Invoke-LangBrainApi -Method GET -Path "/tools/pool_pump" | Out-Null
Invoke-LangBrainApi -Method POST -Path "/tools" -Body @{
    target = "pool_pump"
    value = "ON"
} | Out-Null
Invoke-LangBrainApi -Method GET -Path "/tools/pool_pump" | Out-Null
Invoke-LangBrainApi -Method POST -Path "/events/unblock" -Body @{
    target = "front_door_lock"
    reasoning = "Smoke test concluso"
} | Out-Null
Invoke-LangBrainApi -Method DELETE -Path "/events/reset-conflicts/front_door_lock" | Out-Null
Invoke-LangBrainApi -Method GET -Path "/events?window_minutes=30" | Out-Null

Write-Host "=== 7. HITL: configurazione, interrupt, stato e resume ===" -ForegroundColor Yellow
Invoke-LangBrainApi -Method POST -Path "/hitl/config" -Body @{
    hitl_all = $false
    hitl_nodes = @("organ_security")
    hitl_targets = @()
    hitl_actions = @()
    max_wait_seconds = 120
} | Out-Null
Invoke-LangBrainApi -Method GET -Path "/hitl/config" | Out-Null
Invoke-LangBrainApi -Method POST -Path "/graph/run" -Body @{
    sensor_readings = @()
    force_next_agent = "organ_security"
    thread_id = "smoke-hitl"
} | Out-Null
$hitlState = Invoke-LangBrainApi -Method GET -Path "/graph/state?thread_id=smoke-hitl"
if (-not $hitlState.is_interrupted) {
    throw "Lo smoke test si aspettava un interrupt HITL sul nodo organ_security."
}
Invoke-LangBrainApi -Method POST -Path "/graph/resume" -Body @{
    decision = "RESPINGI"
    reasoning = "Rifiuto controllato dello smoke test"
    thread_id = "smoke-hitl"
} | Out-Null
Invoke-LangBrainApi -Method POST -Path "/hitl/config" -Body @{
    hitl_all = $false
    hitl_nodes = @()
    hitl_targets = @()
    hitl_actions = @()
    max_wait_seconds = 120
} | Out-Null

if ($RunLlmTests) {
    Write-Host "=== 8. Proxy LLM, health check e semantic override opzionali ===" -ForegroundColor Yellow
    Invoke-LangBrainApi -Method POST -Path "/llm/invoke" -Body @{
        system_prompt = "Rispondi soltanto OK."
        user_prompt = "Health check LangBrain"
        temperature = 0
        max_tokens = 16
        enable_reasoning = $false
        fallback_on_error = $false
    } | Out-Null
    Invoke-LangBrainApi -Method POST -Path "/graph/health-check" | Out-Null

    Invoke-LangBrainApi -Method POST -Path "/hitl/config" -Body @{
        hitl_all = $false
        hitl_nodes = @("brain")
        hitl_targets = @()
        hitl_actions = @()
        max_wait_seconds = 120
    } | Out-Null
    Invoke-LangBrainApi -Method POST -Path "/graph/run" -Body @{
        sensor_readings = @()
        force_next_agent = "brain"
        thread_id = "smoke-override"
    } | Out-Null
    Invoke-LangBrainApi -Method POST -Path "/graph/resume" -Body @{
        decision = "OVERRIDE"
        reasoning = "Accendi pool_pump"
        thread_id = "smoke-override"
    } | Out-Null
    Invoke-LangBrainApi -Method POST -Path "/hitl/config" -Body @{
        hitl_all = $false
        hitl_nodes = @()
        hitl_targets = @()
        hitl_actions = @()
        max_wait_seconds = 120
    } | Out-Null
}

Write-Host "=== 9. Eliminazione agenti e reset finale ===" -ForegroundColor Yellow
Invoke-LangBrainApi -Method DELETE -Path "/agents/component_alarm" | Out-Null
Invoke-LangBrainApi -Method DELETE -Path "/agents/component_door_lock" | Out-Null
Invoke-LangBrainApi -Method DELETE -Path "/agents/organ_security" | Out-Null
Invoke-LangBrainApi -Method GET -Path "/agents" | Out-Null
Invoke-LangBrainApi -Method DELETE -Path "/system/reset" | Out-Null

Write-Host "`nSmoke test API di LangBrain completato." -ForegroundColor Green
