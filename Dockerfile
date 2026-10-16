# ---- 阶段 1: 构建器 (Builder) ----
# 使用一个功能完整的镜像，它包含编译工具或可以轻松安装它们
FROM python:3.12-bookworm AS builder

# 安装编译 uvloop 和 httptools 所需的系统依赖 (C编译器等)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制需求文件
COPY requirements.txt .

# 在这个具备编译环境的阶段安装所有 Python 依赖
# 安装到一个独立的目录 /install 中，以便后续复制
RUN pip install --no-cache-dir --require-hashes --prefix="/install" -r requirements.txt

# ---- 阶段 2: 最终镜像 (Final Image) ----
# 使用轻量级 slim 镜像作为最终的运行环境
FROM python:3.12-slim-bookworm

# 设置工作目录
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1

# 添加元数据标签
LABEL name="XHS-Downloader" authors="JoeanAmier" repository="https://github.com/JoeanAmier/XHS-Downloader"

# 从构建器阶段，将已经安装好的依赖包复制到最终镜像的系统路径中
COPY --from=builder /install /usr/local

# 复制项目代码和相关文件
COPY locale /app/locale
COPY source /app/source
COPY static /app/static
COPY LICENSE /app/LICENSE
COPY main.py /app/main.py

# 暴露端口
EXPOSE 5556

# 创建普通运行用户，仅为数据目录授予写权限
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /app/Volume --shell /usr/sbin/nologin app \
    && mkdir -p /app/Volume \
    && chown 10001:10001 /app/Volume

# 创建挂载点
VOLUME /app/Volume

USER 10001:10001

# 设置容器启动命令
CMD ["python", "main.py", "TUI"]
