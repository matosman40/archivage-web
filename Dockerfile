FROM python:3.12-slim

# rsync : nécessaire pour les copies/déplacements.
# p7zip-full : archives 7z chiffrées pour la sauvegarde 1fichier.
# Pas besoin de findmnt/util-linux : la détection des disques lit /proc/mounts
# directement en Python (voir backend/app/disks.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends rsync p7zip-full tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY frontend ./frontend

ENV FRONTEND_DIR=/app/frontend
EXPOSE 8080

# Le conteneur ne doit pas planter si une source/destination n'est pas encore
# montée : toutes les vérifications de chemins se font à la volée dans le
# code (os.path.isdir(...) avant chaque scan), pas au démarrage.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
