# TrueSight Remote — image du relais WebSocket de bureau à distance (asyncio).
FROM python:3.12-slim

# Évite les fichiers .pyc et force la sortie non bufferisée (logs lisibles).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Installation des dépendances Python d'abord (cache de couche).
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copie du code du relais.
COPY . /app

# Utilisateur non-root pour limiter la surface d'attaque.
RUN useradd --create-home --uid 10002 truesight \
    && chown -R truesight:truesight /app
USER truesight

EXPOSE 8765

# Lancement du relais asyncio (lit DATABASE_URL depuis l'environnement).
CMD ["python", "relay.py"]
