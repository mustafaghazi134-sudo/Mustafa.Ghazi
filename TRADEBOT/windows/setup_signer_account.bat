@echo off
REM ONE-TIME, read-only with respect to money: creates the separate signer account, its locked folder, and a NEW
REM dedicated test wallet inside it. Funds nothing, signs nothing, sends nothing. Prints only the PUBLIC address.
REM Run from TRADEBOT\windows as Administrator (right-click cmd.exe, "Run as administrator").
REM Design: docs/LIVE_EXECUTION_ADAPTER.md section 6. The key file is readable only by the account "tradebot-signer"
REM and SYSTEM; your normal account (where the trading bot runs) cannot read it.
setlocal
set ACCT=tradebot-signer
set HOME_DIR=C:\tradebot-signer
set KEY=%HOME_DIR%\test-wallet.json
cd /d "%~dp0\.."
net session >nul 2>&1 || (echo Please run this from an ADMINISTRATOR command prompt. & pause & exit /b 1)
if not exist .venv\Scripts\python.exe (echo Run windows\setup_windows.bat first. & pause & exit /b 1)
if exist "%KEY%" (echo A wallet already exists at %KEY%. Nothing changed. & type "%HOME_DIR%\PUBLIC_ADDRESS.txt" & pause & exit /b 0)

echo 1/4 Creating the standard (non-administrator) Windows account %ACCT% ...
net user %ACCT% >nul 2>&1 || (echo Choose a password for %ACCT% when asked. & net user %ACCT% * /add || (echo could not create the account & pause & exit /b 1))

echo 2/4 Creating %HOME_DIR% ...
if not exist "%HOME_DIR%" mkdir "%HOME_DIR%"

echo 3/4 Generating a NEW wallet key file inside it (the key is never printed) ...
.venv\Scripts\python.exe -I -c "from solders.keypair import Keypair; import json,sys; k=Keypair(); open(sys.argv[1],'x').write(json.dumps(list(bytes(k)))); open(sys.argv[2],'w').write(str(k.pubkey())+'\n')" "%KEY%" "%HOME_DIR%\PUBLIC_ADDRESS.txt" || (echo key generation failed & pause & exit /b 1)

echo 4/4 Locking the folder: only %ACCT% and SYSTEM may access it ...
icacls "%HOME_DIR%" /inheritance:r /grant:r "%ACCT%:(OI)(CI)F" "SYSTEM:(OI)(CI)F" >nul || (echo icacls failed & pause & exit /b 1)
icacls "%HOME_DIR%\PUBLIC_ADDRESS.txt" /grant "%USERNAME%:R" >nul
echo.
echo Check: your account should now be DENIED access to the key file:
type "%KEY%" >nul 2>&1 && echo   WARNING: the key file is still readable from this account. Do not fund it; tell Claude. || echo   OK: access denied.
echo.
echo PUBLIC address of the new test wallet (safe to share; this is not the key):
type "%HOME_DIR%\PUBLIC_ADDRESS.txt"
echo.
echo Do NOT send funds yet. Send this public address to Claude.
pause
