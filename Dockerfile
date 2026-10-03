FROM python:3.12-slim

# LittleCMS 运行库由 Pillow 的 manylinux wheel 内置（liblcms2 静态链接），
# 但合成 CMYK 演示配置与 Lab 配置通过 ctypes 动态调用系统 liblcms2，
# 因此安装 liblcms2 共享库；无需其它系统依赖。
RUN apt-get update \
    && apt-get install -y --no-install-recommends liblcms2-2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 持久化目录（ICC 配置、任务产物、局部修色产物、SQLite、发布/门禁不可变存储）
RUN mkdir -p /data/profiles/source /data/profiles/press \
    /data/profiles/display /data/profiles/builtin /data/jobs \
    /data/retouch \
    /data/release/snapshots /data/release/baselines /data/release/gates
ENV SPD_DATA_DIR=/data \
    SPD_MAX_DIMENSION=3000 \
    SPD_MAX_UPLOAD_MB=50 \
    PYTHONUNBUFFERED=1

EXPOSE 8000
VOLUME ["/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
