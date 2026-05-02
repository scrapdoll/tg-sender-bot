FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md main.py alembic.ini /app/
COPY tg_spam_agent /app/tg_spam_agent
COPY alembic /app/alembic

RUN pip install --no-cache-dir .

CMD ["tg-spam-agent", "run-manager"]
