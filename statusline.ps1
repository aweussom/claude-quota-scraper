# Claude Code status line script
# Reads session JSON from stdin and quota data from ~/.claude/quota-data.json

param(
    [string]$QuotaFile = "$env:USERPROFILE\.claude\quota-data.json"
)

$sessionRaw = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($sessionRaw)) {
    exit 0
}

try {
    $session = $sessionRaw | ConvertFrom-Json
}
catch {
    Write-Output "ctx:?% quota:?%"
    exit 0
}

$contextPct = "?"
if ($session.context_window -and $null -ne $session.context_window.used_percentage) {
    $contextPct = [math]::Round([double]$session.context_window.used_percentage, 1)
}

$quotaPct = "?"
if (Test-Path $QuotaFile) {
    try {
        $quota = Get-Content -Raw -Path $QuotaFile | ConvertFrom-Json
        if ($null -ne $quota.quota_used_pct) {
            $quotaPct = [math]::Round([double]$quota.quota_used_pct, 1)
        }
    }
    catch {
        $quotaPct = "?"
    }
}

Write-Output "ctx:$contextPct% quota:$quotaPct%"
