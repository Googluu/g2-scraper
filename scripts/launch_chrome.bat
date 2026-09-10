@REM navegador externo (windows)
@echo off
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "PROFILE=%USERPROFILE%\.g2-scraper\chrome-profile"
if not exist "%PROFILE%" mkdir "%PROFILE%"

echo Chrome : %CHROME%
echo Perfil : %PROFILE%
echo CDP    : http://localhost:9222

start "" "%CHROME%" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%PROFILE%" ^
  --no-first-run ^
  --no-default-browser-check ^
  --disable-blink-features=AutomationControlled ^
  --window-size=1440,900 ^
  about:blank