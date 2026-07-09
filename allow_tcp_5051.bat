@echo off
:: Right-click -> Run as administrator
netsh advfirewall firewall delete rule name="OpenEye TCP 5051" >nul 2>&1
netsh advfirewall firewall add rule name="OpenEye TCP 5051" dir=in action=allow protocol=TCP localport=5051 profile=any
if %errorlevel%==0 (
    echo OK - port 5051 is now allowed through Windows Firewall.
    netsh advfirewall firewall show rule name="OpenEye TCP 5051"
) else (
    echo FAILED - you must run this as Administrator.
)
pause
