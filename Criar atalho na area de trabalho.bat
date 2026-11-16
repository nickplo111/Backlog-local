@echo off
cd /d "%~dp0"
echo.
echo  Criando o atalho "Backlog" na area de trabalho...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$q=[char]34; $b='%~dp0Iniciar Backlog.bat'; $s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Backlog.lnk'); $s.TargetPath=$env:ComSpec; $s.Arguments='/c '+$q+$q+$b+$q+$q; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0icone.ico'; $s.WindowStyle=7; $s.Description='Backlog (localhost)'; $s.Save()"
if errorlevel 1 (
  echo  Nao consegui criar o atalho. Siga o passo a passo do LEIA-ME.
) else (
  echo  Pronto! O atalho "Backlog" esta na area de trabalho, com o icone novo.
  echo  Para fixar na barra de tarefas: botao direito no atalho ^> Mostrar mais opcoes ^> Fixar na barra de tarefas.
)
echo.
pause
