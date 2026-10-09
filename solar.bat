cd /d "%~dp0" & start "" cmd /c "timeout /t 4 /nobreak >nul & start http://localhost:8080" & python goodwe_dashboard.py & pause
