# Instaleaza tot ce are nevoie ClipBot, DOAR in folderul acesta.
# Rulare: powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Continue"   # checks below call tools that may write to stderr
Set-Location $PSScriptRoot

# Python 3.10 - 3.13 (testat pe 3.13). Preferam launcher-ul "py", altfel "python" din PATH.
$py = $null
foreach ($cand in @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("py", "-3.10"), @("python"))) {
    $exe = $cand[0]; $args0 = @($cand | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $ver = & $exe @args0 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $ver -in @("3.10", "3.11", "3.12", "3.13")) { $py = $cand; break }
}
if (-not $py) {
    Write-Host "Lipseste Python 3.10-3.13. Instaleaza-l cu: winget install Python.Python.3.13  (apoi redeschide terminalul)" -ForegroundColor Red
    exit 1
}
Write-Host "Folosesc Python $ver"

$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ff -and -not (Test-Path "tools\ffmpeg.exe")) {
    Write-Host "Lipseste ffmpeg. Instaleaza-l cu: winget install Gyan.FFmpeg  (apoi redeschide terminalul)" -ForegroundColor Red
    exit 1
}
if ($ff -and -not ((& ffmpeg -hide_banner -filters 2>&1) -match " ass ")) {
    Write-Host "ffmpeg-ul tau nu are libass (subtitrari). Foloseste build-ul de la: winget install Gyan.FFmpeg" -ForegroundColor Yellow
}
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Write-Host "Nu am gasit o placa NVIDIA: botul merge si pe procesor, dar mult mai incet." -ForegroundColor Yellow
}
$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creez mediul Python in .venv ..."
    $exe = $py[0]; $args0 = @($py | Select-Object -Skip 1)
    & $exe @args0 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
# faster-whisper pulls the CPU-only onnxruntime; the GPU build (same module) replaces it.
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m pip uninstall -y onnxruntime
& .\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps onnxruntime-gpu

New-Item -ItemType Directory -Force tools, assets\fonts, models\face | Out-Null
if (-not (Test-Path "models\face\face_detection_yunet_2023mar.onnx")) {
    Write-Host "Descarc detectorul de fete (YuNet) ..."
    curl.exe -sSL -o models\face\face_detection_yunet_2023mar.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
}
if (-not (Test-Path "models\aligner\onnx\model_fp16.onnx")) {
    Write-Host "Descarc modelul de sincronizare a cuvintelor (~630 MB) ..."
    $env:HF_HOME = "$PSScriptRoot\models\huggingface"
    & .\.venv\Scripts\python.exe -c "from huggingface_hub import hf_hub_download as d; [d('onnx-community/mms-300m-1130-forced-aligner-ONNX', f, local_dir=r'models\aligner') for f in ('config.json','vocab.json','preprocessor_config.json','onnx/model_fp16.onnx')]"
}
if (-not (Test-Path "tools\deno.exe")) {
    Write-Host "Descarc deno (necesar pentru yt-dlp pe YouTube) ..."
    curl.exe -sSL -o tools\deno.zip https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip
    Expand-Archive -Force tools\deno.zip tools
    Remove-Item tools\deno.zip
}
if (-not (Test-Path "tools\llama\llama-server.exe")) {
    Write-Host "Descarc llama.cpp (AI local pentru titluri, ruleaza pe procesor) ..."
    $rel = Invoke-RestMethod https://api.github.com/repos/ggml-org/llama.cpp/releases/latest
    $asset = $rel.assets | Where-Object { $_.name -match "^llama-.*-bin-win-cpu-x64\.zip$" } | Select-Object -First 1
    New-Item -ItemType Directory -Force tools\llama | Out-Null
    curl.exe -sSL -o tools\llama.zip $asset.browser_download_url
    Expand-Archive -Force tools\llama.zip tools\llama
    Remove-Item tools\llama.zip
}
if (-not (Test-Path "models\llm\Qwen3-4B-Instruct-2507-Q4_K_M.gguf")) {
    Write-Host "Descarc modelul pentru titluri (Qwen3-4B, ~2.5 GB) ..."
    $env:HF_HOME = "$PSScriptRoot\models\huggingface"
    & .\.venv\Scripts\python.exe -c "from huggingface_hub import hf_hub_download as d; d('unsloth/Qwen3-4B-Instruct-2507-GGUF', 'Qwen3-4B-Instruct-2507-Q4_K_M.gguf', local_dir=r'models\llm')"
}
# Better Romanian transcripts (eek/whisper-turbo-ro-en, MIT). The repo is gated: it needs a free Hugging Face account
# that accepted the terms on its page, and a read token in HF_TOKEN. The token is only used for this download.
if (-not (Test-Path "models\whisper-ro\ctranslate2-int8\model.bin")) {
    if ($env:HF_TOKEN) {
        Write-Host "Descarc modelul Whisper pentru romana (~820 MB) ..."
        $env:HF_HOME = "$PSScriptRoot\models\huggingface"
        & .\.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download as d; d('eek/whisper-turbo-ro-en', revision='ft-w12', allow_patterns=['ctranslate2-int8/*', 'LICENSE'], local_dir=r'models\whisper-ro')"
        Remove-Item -Recurse -Force "models\whisper-ro\.cache" -ErrorAction SilentlyContinue
    } else {
        Write-Host "Optional: pentru transcript mai bun in romana vezi README (modelul eek/whisper-turbo-ro-en, cere HF_TOKEN)." -ForegroundColor Yellow
    }
}
foreach ($f in "Montserrat-Black.ttf", "Montserrat-ExtraBold.ttf") {
    if (-not (Test-Path "assets\fonts\$f")) {
        curl.exe -sSL -o "assets\fonts\$f" "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/$f"
    }
}
Write-Host "Gata. Porneste botul cu start.bat" -ForegroundColor Green
