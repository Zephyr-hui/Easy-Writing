param([string]$Python = '', [string]$DistPath = 'dist')

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (!$Python) { $Python = Join-Path $repo '.venv/Scripts/python.exe' }
Push-Location $repo
try {
    & $Python -c "import PyInstaller, tkinter, tesserocr"
    if ($LASTEXITCODE -ne 0) { throw "安装构建依赖后再打包：pip install -e '.[desktop-build,ocr]'" }
    $runtime = Join-Path $repo ('build/desktop-tk-' + [guid]::NewGuid().ToString('N'))
    & $Python scripts/stage_desktop_tk.py $runtime
    if ($LASTEXITCODE -ne 0) { throw 'Tcl/Tk 运行时资源准备失败。' }
    & $Python -m PyInstaller --noconfirm --onedir --windowed --name EasyWriting `
        --paths src --specpath build --workpath build/desktop --distpath $DistPath `
        --add-data "${runtime}/tcl:_tcl_data" --add-data "${runtime}/tk:_tk_data" `
        --collect-data easy_writing --collect-all pypdfium2_raw --collect-data pypdfium2 `
        --collect-submodules langgraph --copy-metadata easy-writing `
        --exclude-module streamlit --exclude-module torch --exclude-module transformers `
        --exclude-module sentence_transformers --exclude-module scipy --exclude-module pandas `
        src/easy_writing/desktop/__main__.py
    if ($LASTEXITCODE -ne 0) { throw '桌面打包失败。' }
    Write-Output (Join-Path (Resolve-Path -LiteralPath $DistPath).Path 'EasyWriting/EasyWriting.exe')
}
finally { Pop-Location }
