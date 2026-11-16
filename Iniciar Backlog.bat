@echo off
cd /d "%~dp0"
title Backlog (localhost)
echo.
echo  Iniciando o Backlog...
echo.
set "PY="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo  O Python nao foi encontrado neste computador.
  echo  Instale em https://www.python.org/downloads/
  echo  Na instalacao, marque "Add python.exe to PATH". Depois abra este arquivo de novo.
  echo.
  pause
  exit /b 1
)
%PY% servidor.py
if errorlevel 1 (
  echo.
  echo  O servidor parou por causa de um erro. Copie a mensagem acima e envie.
  echo.
  pause
)
