param(
    [Parameter(Mandatory = $true)] [string] $ProjectId,
    [Parameter(Mandatory = $true)] [string] $DocumentId,
    [string] $Region = "us-east1",
    [string] $DeviceId = "shopping-board-camera",
    [string] $DeviceToken = ""
)

$ErrorActionPreference = "Stop"
$serviceName = "shopping-board-ingest"
$serviceAccountName = "shopping-board-runtime"
$serviceAccount = "$serviceAccountName@$ProjectId.iam.gserviceaccount.com"
$secretName = "shopping-board-device-token"
$cloudSource = Join-Path $PSScriptRoot "cloud"

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "gcloud is not installed or is not on PATH"
}

function Invoke-Gcloud {
    & gcloud @args
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud command failed: gcloud $($args -join ' ')"
    }
}

if ([string]::IsNullOrWhiteSpace($DeviceToken)) {
    $randomBytes = New-Object byte[] 32
    $randomNumberGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $randomNumberGenerator.GetBytes($randomBytes)
    }
    finally {
        $randomNumberGenerator.Dispose()
    }
    $DeviceToken = -join ($randomBytes | ForEach-Object { $_.ToString("x2") })
}

Invoke-Gcloud config set project $ProjectId
Invoke-Gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com vision.googleapis.com docs.googleapis.com secretmanager.googleapis.com

$projectNumber = & gcloud projects describe $ProjectId --format="value(projectNumber)"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($projectNumber)) {
    throw "Could not resolve the Google Cloud project number"
}
$buildServiceAccount = "$projectNumber-compute@developer.gserviceaccount.com"
Invoke-Gcloud projects add-iam-policy-binding $ProjectId --member="serviceAccount:$buildServiceAccount" --role="roles/run.builder"

$existingServiceAccount = & gcloud iam service-accounts list --project=$ProjectId --filter="email=$serviceAccount" --format="value(email)"
if ($LASTEXITCODE -ne 0) {
    throw "Could not list service accounts"
}
if ($existingServiceAccount -ne $serviceAccount) {
    Invoke-Gcloud iam service-accounts create $serviceAccountName --display-name="Shopping board runtime"
}

Invoke-Gcloud projects add-iam-policy-binding $ProjectId --member="serviceAccount:$serviceAccount" --role="roles/serviceusage.serviceUsageConsumer"

$temporarySecretFile = [System.IO.Path]::GetTempFileName()
try {
    [System.IO.File]::WriteAllText($temporarySecretFile, $DeviceToken)
    $existingSecrets = & gcloud secrets list --project=$ProjectId --format="value(name)"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not list Secret Manager secrets"
    }
    if ($existingSecrets -notcontains $secretName) {
        Invoke-Gcloud secrets create $secretName --replication-policy="automatic"
    }
    Invoke-Gcloud secrets versions add $secretName --data-file=$temporarySecretFile
}
finally {
    Remove-Item -LiteralPath $temporarySecretFile -Force
}

Invoke-Gcloud secrets add-iam-policy-binding $secretName --member="serviceAccount:$serviceAccount" --role="roles/secretmanager.secretAccessor"

Invoke-Gcloud run deploy $serviceName --source=$cloudSource --region=$Region --service-account=$serviceAccount --no-invoker-iam-check --default-url --max-instances=1 --concurrency=4 --memory=512Mi --timeout=60 --set-env-vars="GOOGLE_DOC_ID=$DocumentId,ALLOWED_DEVICE_ID=$DeviceId" --set-secrets="DEVICE_TOKEN=${secretName}:latest"

$serviceUrl = gcloud run services describe $serviceName --region=$Region --format="value(status.url)"
if ($LASTEXITCODE -ne 0) {
    throw "Could not read the deployed Cloud Run URL"
}
Write-Host ""
Write-Host "Cloud Run URL: $serviceUrl/v1/captures"
Write-Host "Runtime service account: $serviceAccount"
Write-Host "Device ID: $DeviceId"
Write-Host "Device token (copy to firmware/secrets.h): $DeviceToken"
Write-Host "Share the Google Doc with the runtime service account as Editor before testing."
