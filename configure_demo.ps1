$ErrorActionPreference = 'Stop'
$runtimePath = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$localConfigPath = Join-Path $runtimePath 'demo-config.json'
$currentConfig = $null
if (Test-Path -LiteralPath $localConfigPath) {
    $currentConfig = Get-Content -LiteralPath $localConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
$dbParent = Read-Host 'WeChat 4.x database parent directory (contains account folders)'
if (-not $dbParent -and $currentConfig) { $dbParent = $currentConfig.db_dir }
$accountFolder = Read-Host 'Full account folder name (contains db_storage)'
if (-not $accountFolder -and $currentConfig) { $accountFolder = $currentConfig.account }
$testFriend = Read-Host 'Test friend remark [default: 补习Demo测试]'
if (-not $testFriend) { $testFriend = '补习Demo测试' }
@{ db_dir = $dbParent; account = $accountFolder; test_friend = $testFriend } | ConvertTo-Json |
    Set-Content -LiteralPath $localConfigPath -Encoding UTF8
$secureKey = Read-Host 'DeepSeek API key (hidden; leave empty to keep existing local key)' -AsSecureString
if ($secureKey.Length -gt 0) {
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try {
        $keyText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
        @{ DEEPSEEK_API_KEY = $keyText } | ConvertTo-Json |
            Set-Content -LiteralPath (Join-Path $runtimePath 'secrets.json') -Encoding UTF8
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
        $keyText = $null
    }
}
Write-Output 'Saved locally. Never commit runtime or paste API keys into chat.'
