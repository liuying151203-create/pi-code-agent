FROM python:3.12-slim
WORKDIR /opt/pi-code-agent
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /work
USER 65534:65534
ENTRYPOINT ["python", "-I", "-c", "import time; time.sleep(86400)"]
