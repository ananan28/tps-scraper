@echo off
rem 无人值守：程序退出（断电、崩溃、断网卡死）后 30 秒再拉起来，自动接着上次没跑完的地址继续
cd /d "%~dp0"
:loop
echo [%date% %time%] 启动续跑 >> 续跑记录.txt
start "" /wait "新2.0版本.exe" --resume
echo [%date% %time%] 程序退出，30 秒后再拉起 >> 续跑记录.txt
timeout /t 30 /nobreak >/dev/null
goto loop
