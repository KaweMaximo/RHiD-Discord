FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

# Dependências básicas
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg unzip fonts-liberation \
  && rm -rf /var/lib/apt/lists/*

# Repositório oficial do Google Chrome
RUN mkdir -p /usr/share/keyrings \
  && wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
     | gpg --dearmor -o /usr/share/keyrings/google-linux.gpg \
  && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
     > /etc/apt/sources.list.d/google-chrome.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends google-chrome-stable \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requisitos Python
COPY requirements.txt .
RUN pip install -r requirements.txt

# Código do projeto
COPY apps ./apps

# Variáveis padrões úteis pro Selenium
ENV CHROME_BINARY=/usr/bin/google-chrome
# Em container, o headless é recomendado
ENV HEADLESS=true

ENV PYTHONPATH=/app

CMD ["python", "apps/discord-bot/main.py"]
