$ErrorActionPreference = "Stop"

function Read-RequiredValue {
    param([string]$Name)

    do {
        $value = Read-Host $Name
        if ([string]::IsNullOrWhiteSpace($value)) {
            Write-Host "$Name is required." -ForegroundColor Yellow
        }
    } while ([string]::IsNullOrWhiteSpace($value))

    return $value
}

function Read-RequiredSecret {
    param([string]$Name)

    do {
        $secureValue = Read-Host $Name -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
        try {
            $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        }
        finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }

        if ([string]::IsNullOrWhiteSpace($value)) {
            Write-Host "$Name is required." -ForegroundColor Yellow
        }
    } while ([string]::IsNullOrWhiteSpace($value))

    return $value
}

$values = @{
    TWILIO_ACCOUNT_SID = Read-RequiredSecret "TWILIO_ACCOUNT_SID"
    TWILIO_AUTH_TOKEN = Read-RequiredSecret "TWILIO_AUTH_TOKEN"
    OPENAI_API_KEY = Read-RequiredSecret "OPENAI_API_KEY"
    TWILIO_FROM_NUMBER = Read-RequiredValue "TWILIO_FROM_NUMBER"
    CALL_TO_NUMBER = Read-RequiredValue "CALL_TO_NUMBER"
    TELEGRAM_BOT_TOKEN = Read-RequiredSecret "TELEGRAM_BOT_TOKEN"
    TELEGRAM_CHAT_ID = Read-RequiredValue "TELEGRAM_CHAT_ID"
    DTMF_DIGITS = Read-RequiredValue "DTMF_DIGITS"
    AUDIO_URL = Read-RequiredValue "AUDIO_URL"
    KEYWORD = Read-RequiredValue "KEYWORD"
}

foreach ($entry in $values.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "User")
    Set-Item -Path "Env:$($entry.Key)" -Value $entry.Value
}

Write-Host "Environment variables saved for this Windows user." -ForegroundColor Green
Write-Host "You can now test the deployment with:"
Write-Host "  python dialer.py --dry-run"