FROM python:3.12-slim

# rsync : nécessaire pour les copies/déplacements.
# p7zip-full : archives 7z chiffrées pour la sauvegarde 1fichier.
# Pas besoin de findmnt/util-linux : la détection des disques lit /proc/mounts
# directement en Python (voir backend/app/disks.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends rsync p7zip-full tzdata \
    && rm -rf /var/lib/apt/lists/*

# Utilisateur non root (version 45). Le processus tourne avec PUID/PGID (voir docker-compose.yml) :
# ce sont les identifiants du propriétaire de tes médias sur l'hôte. Les valeurs ci-dessous ne servent
# qu'au premier démarrage du volume /data.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} archivage \
    && useradd -u ${APP_UID} -g ${APP_GID} -M -s /usr/sbin/nologin archivage \
    && mkdir -p /data \
    && chown ${APP_UID}:${APP_GID} /data

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY frontend ./frontend

ENV FRONTEND_DIR=/app/frontend
EXPOSE 8080

# Vérification de santé : l'API répond (stdlib uniquement, pas de curl dans l'image).
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/config', timeout=4)" || exit 1

# Le conteneur ne doit pas planter si une source/destination n'est pas encore
# montée : toutes les vérifications de chemins se font à la volée dans le
# code (os.path.isdir(...) avant chaque scan), pas au démarrage.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
