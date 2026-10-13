# s13-B 新增：只制作固定工具环境，运行时挂载由宿主入口选择，不 COPY 项目源码。
FROM python:3.12-slim
WORKDIR /opt/zero2pi
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir ".[dev]"
COPY s02_tool_use/code.py ./base_tools.py
COPY s13_a_sandbox_copy/__init__.py s13_a_sandbox_copy/snapshot.py s13_a_sandbox_copy/sandbox.py ./s13_a_sandbox_copy/
COPY s13_b_sandbox_workspace/__init__.py s13_b_sandbox_workspace/workspace_access.py s13_b_sandbox_workspace/tool_worker.py ./s13_b_sandbox_workspace/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp PYTHONPATH=/work:/work/src
WORKDIR /work
USER 65534:65534
ENTRYPOINT ["python", "-I", "-c", "import time; time.sleep(86400)"]
