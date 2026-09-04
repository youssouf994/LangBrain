# =====================================================================
# LangBrain — Smoke Test End-to-End completo (PowerShell)
# Copia e incolla l'intero blocco nel terminale, oppure eseguilo come
# script: .\smoke_test_full.ps1
#
# Prerequisiti: server già avviato (locale o Docker) su $BaseUrl,
# provider LLM locale configurato in .env con DEFAULT_PROVIDER=local,
# LOCAL_MODEL_BASE_URL e LOCAL_MODEL. Riavviare il server dopo modifiche al .env.
# =====================================================================

$BaseUrl = "http://127.0.0.1:8000"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ScriptRoot) { $ScriptRoot = (Get-Location).Path }
$EnvPath = Join-Path $ScriptRoot ".env"

function Get-DotEnvValue([string]$Name) {
    if (-not (Test-Path -LiteralPath $EnvPath)) {
        throw "File .env non trovato: $EnvPath"
    }

    $entry = Get-Content -LiteralPath $EnvPath | Where-Object {
        $_ -match "^\s*$([regex]::Escape($Name))\s*="
    } | Select-Object -Last 1

    if (-not $entry) { return $null }
    $value = ($entry -split "=", 2)[1].Trim()
    return $value.Trim('"').Trim("'")
}

$LlmProvider = (Get-DotEnvValue "DEFAULT_PROVIDER")
if ($LlmProvider) { $LlmProvider = $LlmProvider.ToLowerInvariant() }
$LocalModel = (Get-DotEnvValue "LOCAL_MODEL")
$LocalModelBaseUrl = (Get-DotEnvValue "LOCAL_MODEL_BASE_URL")
if ($LlmProvider -ne "local") {
    throw "Lo smoke test richiede DEFAULT_PROVIDER=local nel file .env. Valore corrente: '$LlmProvider'."
}
if ([string]::IsNullOrWhiteSpace($LocalModel)) {
    throw "LOCAL_MODEL non e' configurato nel file .env."
}
if ([string]::IsNullOrWhiteSpace($LocalModelBaseUrl)) {
    throw "LOCAL_MODEL_BASE_URL non e' configurato nel file .env."
}

try {
    $localModels = Invoke-RestMethod -Uri "$($LocalModelBaseUrl.TrimEnd('/'))/models" -Method Get -TimeoutSec 5
} catch {
    throw "LLM locale non raggiungibile su '$LocalModelBaseUrl'. Avvia il server del modello prima dello smoke test."
}
$availableLocalModels = @($localModels.data | ForEach-Object { $_.id })
if ($availableLocalModels.Count -gt 0 -and $LocalModel -notin $availableLocalModels) {
    throw "LOCAL_MODEL='$LocalModel' non e' disponibile. Modelli esposti: $($availableLocalModels -join ', ')."
}

Write-Host "LLM smoke test: provider locale, modello '$LocalModel'." -ForegroundColor DarkGray

function Step($title) {
    Write-Host "`n=====================================================" -ForegroundColor Cyan
    Write-Host $title -ForegroundColor Cyan
    Write-Host "=====================================================" -ForegroundColor Cyan
}

function Show($response) {
    $response | ConvertTo-Json -Depth 10
}

function ConvertFrom-Utf8JsonResponse($response) {
    $stream = $response.RawContentStream
    if ($stream) {
        if ($stream.CanSeek) { $stream.Position = 0 }
        $bytes = New-Object byte[] $stream.Length
        [void]$stream.Read($bytes, 0, $bytes.Length)
        $jsonString = [System.Text.Encoding]::UTF8.GetString($bytes)
    } else {
        $jsonString = $response.Content
    }
    if ([string]::IsNullOrWhiteSpace($jsonString)) { return $null }
    return $jsonString | ConvertFrom-Json
}

function Invoke-JsonApi {
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [Parameter(Mandatory=$true)][string]$Method,
        [Parameter(Mandatory=$false)][string]$BodyJson
    )

    if ($PSBoundParameters.ContainsKey("BodyJson")) {
        $utf8Bytes = [System.Text.Encoding]::UTF8.GetBytes($BodyJson)
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -Method $Method -Body $utf8Bytes -ContentType "application/json; charset=utf-8"
    } else {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -Method $Method
    }
    return ConvertFrom-Utf8JsonResponse $response
}

# ---------------------------------------------------------------------
Step "0. Health check root"
# ---------------------------------------------------------------------
$health = Invoke-JsonApi -Uri "$BaseUrl/" -Method Get
Show $health

# ---------------------------------------------------------------------
Step "1. Reset ambiente (stato pulito prima della demo)"
# ---------------------------------------------------------------------
try {
    $reset = Invoke-JsonApi -Uri "$BaseUrl/system/reset" -Method Delete
    Show $reset
} catch {
    Write-Host "Reset non disponibile o già pulito, procedo comunque." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------
Step "2. Creazione gerarchia — Organo Clima (Livello 1)"
# ---------------------------------------------------------------------
$climateDef = @{
    name                    = "organ_climate"
    level                   = 1
    parent_agent_name       = "Brain"
    managed_targets         = @("ac_living_room")
    system_prompt_template  = "Sei l'Organo Clima. Se la temperatura supera 27C accendi il climatizzatore. Se rilevi un conflitto nel DB, fai escalation al Brain.`nFormato: DECISIONE: [ACTION|ESCALATE|NONE]"
    priority_weight         = 50.0
} | ConvertTo-Json -Compress

$climateBody = @{ agent_definition = $climateDef } | ConvertTo-Json
$climateResult = Invoke-JsonApi -Uri "$BaseUrl/agents/create" -Method Post -BodyJson $climateBody
Show $climateResult

# ---------------------------------------------------------------------
Step "3. Creazione gerarchia — Organo Sicurezza (Livello 1) + Componente Serratura (Livello 2)"
# ---------------------------------------------------------------------
$securityDef = @{
    name                    = "organ_security"
    level                   = 1
    parent_agent_name       = "Brain"
    managed_targets         = @("alarm_system")
    sub_agent_names         = @("component_door_lock")
    system_prompt_template  = "Sei l'Organo Sicurezza. Coordina i componenti figli e fai escalation al Brain per conflitti critici.`nFormato: DECISIONE: [ACTION|ESCALATE|NONE]"
    priority_weight         = 500.0
} | ConvertTo-Json -Compress

$securityBody = @{ agent_definition = $securityDef } | ConvertTo-Json
$securityResult = Invoke-JsonApi -Uri "$BaseUrl/agents/create" -Method Post -BodyJson $securityBody
Show $securityResult

$doorLockDef = @{
    name                    = "component_door_lock"
    level                   = 2
    parent_agent_name       = "organ_security"
    managed_targets         = @("front_door_lock")
    system_prompt_template  = "Sei il Componente Serratura. Gestisci il lock/unlock della porta principale.`nFormato: DECISIONE: [ACTION|ESCALATE|NONE]"
    priority_weight         = 20.0
} | ConvertTo-Json -Compress

$doorLockBody = @{ agent_definition = $doorLockDef } | ConvertTo-Json
$doorLockResult = Invoke-JsonApi -Uri "$BaseUrl/agents/create" -Method Post -BodyJson $doorLockBody
Show $doorLockResult

# ---------------------------------------------------------------------
Step "4. Verifica gerarchia completa (Brain -> Organi -> Componenti)"
# ---------------------------------------------------------------------
$hierarchy = Invoke-JsonApi -Uri "$BaseUrl/agents/hierarchy" -Method Get
Show $hierarchy

# ---------------------------------------------------------------------
Step "5. Configurazione HITL — richiedi supervisione umana sul target front_door_lock"
# ---------------------------------------------------------------------
$hitlConfig = @{
    hitl_all        = $false
    hitl_nodes      = @()
    hitl_targets    = @("front_door_lock")
    hitl_actions    = @("FORCE_SHUTDOWN", "UNLOCK")
    max_wait_seconds = 300
} | ConvertTo-Json

$hitlResult = Invoke-JsonApi -Uri "$BaseUrl/hitl/config" -Method Post -BodyJson $hitlConfig
Show $hitlResult

# ---------------------------------------------------------------------
Step "6. Ciclo grafo (ROUTING AUTOMATICO) — sensore temperatura alta, nessun force_next_agent"
# ---------------------------------------------------------------------
$threadId = "demo-thread-001"

$runBody1 = @{
    sensor_readings = @(
        @{ sensor_id = "temp_living_room"; agent_owner = "api"; value = "30.0"; unit = "°C" }
    )
    thread_id       = $threadId
} | ConvertTo-Json

$run1 = Invoke-JsonApi -Uri "$BaseUrl/graph/run" -Method Post -BodyJson $runBody1
Show $run1
Write-Host "next_agent riportato dal grafo: $($run1.next_agent)" -ForegroundColor Magenta
Write-Host "Atteso: END dopo la visita degli agenti registrati. L'ordine degli organi dipende da priorita' e ordine del registry." -ForegroundColor DarkGray

# ---------------------------------------------------------------------
Step "7. Simula un conflitto nel DB (qualcun altro ha appena toccato lo stesso target)"
# ---------------------------------------------------------------------
$conflictBody = @{
    actor      = "agent_manual_override"
    action     = "FORCE_SHUTDOWN"
    target     = "front_door_lock"
    old_value  = "LOCKED"
    new_value  = "UNLOCKED"
    reasoning  = "Simulazione conflitto via smoke test"
} | ConvertTo-Json

$conflict = Invoke-JsonApi -Uri "$BaseUrl/events/seed-conflict" -Method Post -BodyJson $conflictBody
Show $conflict

# ---------------------------------------------------------------------
Step "8. Ciclo grafo (ROUTING AUTOMATICO) — il conflitto dovrebbe risalire da solo: component_door_lock -> organ_security -> Brain"
# ---------------------------------------------------------------------
$runBody2 = @{
    sensor_readings = @()
    thread_id       = $threadId
} | ConvertTo-Json

$run2 = Invoke-JsonApi -Uri "$BaseUrl/graph/run" -Method Post -BodyJson $runBody2
Show $run2
Write-Host "next_agent riportato dal grafo: $($run2.next_agent)" -ForegroundColor Magenta
Write-Host "Questo è il test chiave del fix: se next_agent resta bloccato su END invece di risalire la gerarchia, l'escalation N-livelli non è ancora ricorsiva." -ForegroundColor DarkGray

# ---------------------------------------------------------------------
Step "9. Verifica stato del grafo — controlla se è in interrupt (HITL)"
# ---------------------------------------------------------------------
$state = Invoke-JsonApi -Uri "$BaseUrl/graph/state?thread_id=$threadId" -Method Get
Show $state

# ---------------------------------------------------------------------
Step "10. Resume con APPROVA (conferma umana simulata)"
# ---------------------------------------------------------------------
$resumeApprove = @{
    decision  = "APPROVA"
    reasoning = "Approvato dallo smoke test — nessun rischio reale"
    thread_id = $threadId
} | ConvertTo-Json

$approveResult = Invoke-JsonApi -Uri "$BaseUrl/graph/resume" -Method Post -BodyJson $resumeApprove
Show $approveResult

# ---------------------------------------------------------------------
Step "11. Sblocco manuale del target (nel caso sia rimasto bloccato)"
# ---------------------------------------------------------------------
$unblockBody = @{
    target    = "front_door_lock"
    reasoning = "Sblocco manuale post-test via smoke test"
} | ConvertTo-Json

$unblock = Invoke-JsonApi -Uri "$BaseUrl/events/unblock" -Method Post -BodyJson $unblockBody
Show $unblock

# ---------------------------------------------------------------------
Step "12. Nuovo conflitto per testare OVERRIDE (God Mode Semantico via LLM)"
# ---------------------------------------------------------------------
$overrideHitl = @{
    hitl_all         = $false
    hitl_nodes       = @()
    hitl_targets     = @("ac_living_room")
    hitl_actions     = @()
    max_wait_seconds = 300
} | ConvertTo-Json
$overrideHitlResult = Invoke-JsonApi -Uri "$BaseUrl/hitl/config" -Method Post -BodyJson $overrideHitl
Show $overrideHitlResult

$conflict2Body = @{
    actor      = "agent_manual_override"
    action     = "FORCE_SHUTDOWN"
    target     = "ac_living_room"
    old_value  = "22.5°C"
    new_value  = "OFF"
    reasoning  = "Secondo conflitto per test OVERRIDE"
} | ConvertTo-Json

$conflict2 = Invoke-JsonApi -Uri "$BaseUrl/events/seed-conflict" -Method Post -BodyJson $conflict2Body
Show $conflict2

$runBody3 = @{
    sensor_readings = @()
    thread_id       = $threadId
} | ConvertTo-Json

$run3 = Invoke-JsonApi -Uri "$BaseUrl/graph/run" -Method Post -BodyJson $runBody3
Show $run3
Write-Host "next_agent riportato dal grafo: $($run3.next_agent)" -ForegroundColor Magenta
Write-Host "Atteso: brain con interrupt HITL pendente per ac_living_room; il passo successivo esercita davvero OVERRIDE." -ForegroundColor DarkGray

# ---------------------------------------------------------------------
Step "13. Resume con OVERRIDE — l'LLM traduce linguaggio naturale in comandi fisici"
# ---------------------------------------------------------------------
$resumeOverride = @{
    decision  = "OVERRIDE"
    reasoning = "Ignora il blocco: riaccendi il climatizzatore del soggiorno a 22 gradi."
    thread_id = $threadId
} | ConvertTo-Json

$overrideResult = Invoke-JsonApi -Uri "$BaseUrl/graph/resume" -Method Post -BodyJson $resumeOverride
Show $overrideResult

# ---------------------------------------------------------------------
Step "14. Chiamata diretta all'LLM locale tramite il proxy MAO"
# ---------------------------------------------------------------------
$llmBody = @{
    system_prompt    = "Sei un assistente domotico esperto di efficienza energetica."
    user_prompt      = "Qual è la temperatura ideale per il soggiorno di sera in inverno, e perché?"
    provider         = $LlmProvider
    model            = $LocalModel
    temperature      = 0.3
    max_tokens       = 300
    enable_reasoning = $false
    fallback_on_error = $false
} | ConvertTo-Json

$llmResult = Invoke-JsonApi -Uri "$BaseUrl/llm/invoke" -Method Post -BodyJson $llmBody
Show $llmResult

# ---------------------------------------------------------------------
Step "15. Stato finale di tutti i tool IoT"
# ---------------------------------------------------------------------
$tools = Invoke-JsonApi -Uri "$BaseUrl/tools" -Method Get
Show $tools

# ---------------------------------------------------------------------
Step "16. Storico eventi audit log (ultime 4 ore)"
# ---------------------------------------------------------------------
$events = Invoke-JsonApi -Uri "$BaseUrl/events?window_minutes=240" -Method Get
Show $events

# ---------------------------------------------------------------------
Step "17. Health check macro dell'Orchestratore Supremo"
# ---------------------------------------------------------------------
$macroCheck = Invoke-JsonApi -Uri "$BaseUrl/graph/health-check" -Method Post
Show $macroCheck

Write-Host "`n=====================================================" -ForegroundColor Green
Write-Host "Smoke test completo terminato." -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
