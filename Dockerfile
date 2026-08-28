FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_URL=sqlite:////data/money_machine.db

RUN addgroup --system money-machine && adduser --system --ingroup money-machine money-machine
RUN mkdir /data && chown money-machine:money-machine /data
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN python -m pip install --no-cache-dir .

USER money-machine
EXPOSE 8000
CMD ["money-machine", "serve", "--host", "0.0.0.0", "--port", "8000"]
