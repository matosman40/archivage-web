#!/usr/bin/env bash
# Sauvegarde, restauration et droits du volume de données d'Archivage (version 51).
# À lancer depuis le dossier qui contient docker-compose.yml.
#
#   scripts/volume-archivage.sh sauvegarde [DOSSIER]   archive .tgz horodatée (défaut : dossier courant)
#   scripts/volume-archivage.sh restaure ARCHIVE       restaure le volume (conteneur arrêté ; FORCE=1 si non vide)
#   scripts/volume-archivage.sh proprietaire           remet le volume au PUID:PGID de .env
#   scripts/volume-archivage.sh nom                    affiche le nom du volume
#
# Variables : ARCHIVAGE_VOLUME (nom du volume, sinon déduit du nom du dossier comme Docker Compose).
# La sauvegarde contient les mots de passe 1fichier : garde-la hors de tout dossier partagé.
set -euo pipefail

IMAGE="${ARCHIVAGE_IMAGE:-alpine:3.20}"
CONTENEUR="archivage-films"

if [[ ! -f docker-compose.yml ]]; then
  echo "Lance ce script depuis le dossier qui contient docker-compose.yml." >&2
  exit 1
fi

# Valeur d'une variable de .env, ou la valeur par défaut.
lire_env() {
  local valeur
  valeur="$(grep -E "^$1=" .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r' || true)"
  echo "${valeur:-$2}"
}

# Même règle que Docker Compose : nom du dossier en minuscules, caractères autorisés seulement.
nom_volume() {
  if [[ -n "${ARCHIVAGE_VOLUME:-}" ]]; then
    echo "$ARCHIVAGE_VOLUME"
    return
  fi
  local projet
  projet="$(lire_env COMPOSE_PROJECT_NAME "$(basename "$(pwd)")")"
  projet="$(echo "$projet" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
  echo "${projet}_archivage-data"
}

VOL="$(nom_volume)"

verifier_volume() {
  if ! docker volume inspect "$VOL" >/dev/null 2>&1; then
    echo "Volume introuvable : $VOL (voir « docker volume ls », ou ARCHIVAGE_VOLUME)." >&2
    exit 1
  fi
}

sauvegarde() {
  local dossier="${1:-$(pwd)}"
  local nom="archivage-data-$(date +%Y%m%d-%H%M%S).tgz"
  verifier_volume
  mkdir -p "$dossier"
  dossier="$(cd "$dossier" && pwd)"
  docker run --rm -v "$VOL:/data:ro" -v "$dossier:/sauvegarde" "$IMAGE" \
    sh -c "tar czf '/sauvegarde/$nom' -C /data . && chmod 600 '/sauvegarde/$nom'"
  echo "Sauvegarde : $dossier/$nom"
  echo "Elle contient les mots de passe 1fichier : garde-la hors de tout dossier partagé."
}

proprietaire() {
  local uid gid
  uid="$(lire_env PUID 1000)"
  gid="$(lire_env PGID 1000)"
  if [[ ! "$uid" =~ ^[0-9]+$ || ! "$gid" =~ ^[0-9]+$ ]]; then
    echo "PUID et PGID doivent être des nombres dans .env (actuels : $uid / $gid)." >&2
    exit 1
  fi
  verifier_volume
  docker run --rm -v "$VOL:/data" "$IMAGE" chown -R "$uid:$gid" /data
  echo "Droits du volume $VOL : $uid:$gid"
}

restaure() {
  local archive="${1:-}"
  if [[ -z "$archive" || ! -f "$archive" ]]; then
    echo "Indique un fichier .tgz existant : scripts/volume-archivage.sh restaure ARCHIVE" >&2
    exit 1
  fi
  if docker ps --format '{{.Names}}' | grep -qx "$CONTENEUR"; then
    echo "Arrête d'abord le conteneur : docker compose stop archivage" >&2
    exit 1
  fi
  docker volume inspect "$VOL" >/dev/null 2>&1 || docker volume create "$VOL" >/dev/null

  local present
  present="$(docker run --rm -v "$VOL:/data" "$IMAGE" sh -c 'ls -A /data | wc -l')"
  if [[ "$present" != "0" ]]; then
    if [[ "${FORCE:-}" != "1" ]]; then
      echo "Le volume $VOL contient déjà $present élément(s). Relance avec FORCE=1 pour le remplacer." >&2
      echo "Une copie de sécurité est faite avant le remplacement." >&2
      exit 1
    fi
    echo "Copie de sécurité du volume actuel avant remplacement :"
    sauvegarde "./avant-restauration-$(date +%Y%m%d-%H%M%S)"
  fi

  local dossier nom
  dossier="$(cd "$(dirname "$archive")" && pwd)"
  nom="$(basename "$archive")"
  docker run --rm -v "$VOL:/data" -v "$dossier:/entree:ro" "$IMAGE" \
    sh -c "rm -rf /data/* /data/.[!.]* 2>/dev/null; tar xzf '/entree/$nom' -C /data"
  echo "Volume $VOL restauré depuis $archive."
  proprietaire
  echo "Relance ensuite : docker compose up -d"
}

case "${1:-}" in
  sauvegarde) shift; sauvegarde "${1:-}" ;;
  restaure) shift; restaure "${1:-}" ;;
  proprietaire) proprietaire ;;
  nom) echo "$VOL" ;;
  *)
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
