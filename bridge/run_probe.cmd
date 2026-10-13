@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python bloomberg_quotes.py "IBM US Equity" "EURUSD Curncy"
) else (
    py -3 bloomberg_quotes.py "IBM US Equity" "EURUSD Curncy"
)
echo.
echo See README.md if Python, blpapi, or the Terminal connection is unavailable.
pause
