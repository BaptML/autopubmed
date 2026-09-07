@echo off
REM Lanceur double-clic d'AutoPubMed. Messages sans accent : la console
REM batch n'est pas fiable en UTF-8 avant le chcp ci-dessous.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title AutoPubMed
set "PYTHONUTF8=1"

where uv >nul 2>&1
if errorlevel 1 (
  echo.
  echo   uv est introuvable.
  echo   Installe-le depuis https://docs.astral.sh/uv/ puis relance ce fichier.
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  echo.
  echo   Fichier .env absent.
  echo   Copie .env.example en .env et renseigne NCBI_EMAIL et OPENALEX_MAILTO.
  echo.
  pause
  exit /b 1
)

echo   Verification des dependances...
uv sync --quiet
if errorlevel 1 (
  echo.
  echo   Echec de "uv sync". Voir le message ci-dessus.
  echo.
  pause
  exit /b 1
)

uv run autopubmed

REM Le serveur s'est arrete : garder la fenetre ouverte pour lire l'erreur.
echo.
pause
