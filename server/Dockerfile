# ParcVue — image du serveur Flask (API + dashboard), servi par gunicorn.
FROM python:3.12-slim

# Évite les fichiers .pyc et force la sortie non bufferisée (logs lisibles).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dépendances système minimales (psycopg[binary] embarque le driver natif).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Installation des dépendances Python d'abord (cache de couche).
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copie du code applicatif.
COPY . /app

# Utilisateur non-root pour limiter la surface d'attaque.
RUN useradd --create-home --uid 10001 parcvue \
    && chown -R parcvue:parcvue /app
USER parcvue

EXPOSE 8000

# Healthcheck via l'endpoint /healthz.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# Lancement : 1 worker, 8 threads.
# IMPORTANT : un seul worker est volontaire. Le bootstrap (create_all + seed) et
# le thread de fond unique (alertes + purge) vivent DANS le processus applicatif ;
# plusieurs workers dupliqueraient ces boucles (alertes/n8n en double) et
# provoqueraient une course au seed. Pour ~100 agents (~3 req/s), 1 worker + 8
# threads est largement suffisant. Ne pas augmenter --workers sans déplacer le
# bootstrap/thread de fond vers un verrou inter-processus (cf. SPEC §7).
CMD ["gunicorn", "-b", "0.0.0.0:8000", "--workers", "1", "--threads", "8", \
     "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
