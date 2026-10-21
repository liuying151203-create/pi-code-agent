# s21 新增：由用户手动构建，复用已准备的 s13-A；运行时不拉取、不安装。
FROM zero2pi-sandbox:s13-a
USER root
# Git 仅用于容器当前 .gitignore 语义，不复制业务 .git，也不访问远程仓库。
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY s21_sandbox_repair/__init__.py s21_sandbox_repair/worker_snapshot.py s21_sandbox_repair/tool_worker.py /opt/zero2pi/s21_sandbox_repair/
USER 65534:65534
