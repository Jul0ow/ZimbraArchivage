# Image épinglée par empreinte (Dependabot la tient à jour)
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin archivage
USER 10001

EXPOSE 8000

# Adresses des proxys dont les en-têtes X-Forwarded-For sont pris en compte (ingress, frontend),
# pour limiter les tentatives de connexion par adresse IP réelle
ENV FORWARDED_ALLOW_IPS=127.0.0.1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
