#!/usr/bin/env bash
# Tests de bout en bout : un Radarr factice, un serveur de test sur un dossier temporaire, puis les scénarios
# navigateur. Aucun chemin personnel ni vrai disque. Variables :
#   CHROME_PATH        (obligatoire) exécutable Chromium
#   PLAYWRIGHT_MODULE  (facultatif) fichier d'entrée du paquet playwright si non résolu par Node
#   PYTHON             (facultatif) interpréteur Python avec les dépendances du backend (défaut : python3)
#   E2E_PORT_BASE      (facultatif) premier port utilisé (défaut 18700)
set -euo pipefail
: "${CHROME_PATH:?CHROME_PATH doit indiquer le chemin de Chromium}"
PYTHON="${PYTHON:-python3}"
PORT_BASE="${E2E_PORT_BASE:-18700}"
DOSSIER="$(cd "$(dirname "$0")" && pwd)"
BASE="$(mktemp -d)"
PIDS=()
nettoyer() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  rm -rf "$BASE"
}
trap nettoyer EXIT

attendre() {  # attendre QUOI : une adresse HTTP qui répond
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null "$1"; then return 0; fi
    sleep 0.5
  done
  echo "Le service $1 ne répond pas." >&2
  exit 1
}

"$PYTHON" "$DOSSIER/faux_radarr.py" "$((PORT_BASE + 1))" "$BASE/radarr.log" &
PIDS+=($!)
attendre "http://127.0.0.1:$((PORT_BASE + 1))/api/v3/system/status"

E2E_BASE="$BASE" E2E_PORT="$PORT_BASE" E2E_RADARR_URL="http://127.0.0.1:$((PORT_BASE + 1))" \
  "$PYTHON" "$DOSSIER/serveur.py" &
PIDS+=($!)
attendre "http://127.0.0.1:$PORT_BASE/"

E2E_URL="http://127.0.0.1:$PORT_BASE/" E2E_LOG="$BASE/radarr.log" CHROME_PATH="$CHROME_PATH" \
  PLAYWRIGHT_MODULE="${PLAYWRIGHT_MODULE:-playwright}" node "$DOSSIER/scenarios.mjs"
