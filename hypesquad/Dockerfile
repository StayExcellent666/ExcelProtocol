FROM python:3.11-slim-bookworm

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

ENV HYPESQUAD_HOST=0.0.0.0
ENV HYPESQUAD_PORT=8091

EXPOSE 8091

CMD ["excelprotocol-hypesquad"]

