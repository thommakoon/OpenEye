' Double-click -> click Yes on the Administrator prompt
Set shell = CreateObject("Shell.Application")
shell.ShellExecute "cmd.exe", "/c netsh advfirewall firewall delete rule name=""OpenEye TCP 5051"" >nul 2>&1 & netsh advfirewall firewall add rule name=""OpenEye TCP 5051"" dir=in action=allow protocol=TCP localport=5051 profile=any & pause", "", "runas", 1
