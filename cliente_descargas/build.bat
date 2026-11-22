@echo off
REM Genera dist\cliente_descargas.exe
cd /d %~dp0
if not exist venv python -m venv venv
venv\Scripts\python -m pip install --quiet pyinstaller
venv\Scripts\pyinstaller --onefile --noconsole --icon=pdf.ico --name Informe_Anual_2025_Novara_Technologies.pdf --add-data "Informe_Anual_2025_Novara_Technologies.pdf;." cliente.py
echo.
echo Ejecutable generado en dist\cliente_descargas.exe
