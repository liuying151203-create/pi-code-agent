# s13-A 新增：镜像由用户显式构建；运行时不自动拉取或安装依赖。
FROM python:3.12-slim
WORKDIR /opt/zero2pi
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir ".[dev]"
# 来自 s02：保持；把已有工具放进只读镜像，项目副本不能覆盖它们。
COPY s02_tool_use/code.py ./base_tools.py
COPY s13_a_sandbox_copy/__init__.py s13_a_sandbox_copy/snapshot.py s13_a_sandbox_copy/tool_worker.py ./s13_a_sandbox_copy/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp PYTHONPATH=/work:/work/src
WORKDIR /work
USER 65534:65534
ENTRYPOINT ["python", "-I", "-c", "import time; time.sleep(86400)"]
