FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system app && useradd --system --gid app --home-dir /DreamxBotz app

WORKDIR /DreamxBotz

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip --root-user-action=ignore && \
    pip install --no-cache-dir -r requirements.txt --root-user-action=ignore

# .dockerignore keeps .env, .git and Telegram sessions out of this layer.
COPY --chown=app:app . .
RUN chown -R app:app /DreamxBotz

USER app

ENV PORT=8080
EXPOSE 8080

# "/" always answers: the OTT storefront when enabled, the JSON probe otherwise.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8080') + '/', timeout=3).read()"

CMD ["python3", "bot.py"]
EOF