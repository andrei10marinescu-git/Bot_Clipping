# Instaleaza tot ce are nevoie ClipBot, DOAR in folderul acesta.
# Rulare: powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg nu e in PATH. Instaleaza-l cu: winget install Gyan.FFmpeg  (sau pune ffmpeg.exe + ffprobe.exe in tools\)" -ForegroundColor Yellow
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creez mediul Python in .venv ..."
    python -m venv .venv
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
foreach ($f in "Montserrat-Black.ttf", "Montserrat-ExtraBold.ttf") {
    if (-not (Test-Path "assets\fonts\$f")) {
        curl.exe -sSL -o "assets\fonts\$f" "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/$f"
    }
}
Write-Host "Gata. Porneste botul cu start.bat" -ForegroundColor Green
