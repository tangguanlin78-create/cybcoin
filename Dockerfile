# ERC20 转账监控 —— Docker 镜像
# 多阶段：先用 slim 基础镜像 + 非 root 用户 + 最小依赖
FROM python:3.12-slim AS base

# 时区设为 Asia/Shanghai（日志时间戳可读）
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 1) 先复制 requirements.txt，利用 Docker 层缓存（改代码不重装依赖）
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 2) 复制程序与非敏感资源文件
#    monitor.py            主程序
#    custom_labels.json    自定义标签库（HEMI 巨鲸 / Bridge 等）
#    sources.example.json  同步源模板（CI 用，容器内仅作参考）
COPY monitor.py custom_labels.json sources.example.json ./

# 3) 创建非 root 运行用户 + 持久化目录
RUN useradd -r -u 1000 -m -d /home/monitor monitor \
    && mkdir -p /data \
    && chown -R monitor:monitor /data /app

USER monitor

# 默认把状态文件指向 /data（docker-compose 会挂载卷持久化）
ENV STATE_FILE=/data/monitor_state.json \
    LOG_FILE=/data/alerts.log

# 健康检查：进程能响应就视为健康
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os; os.path.exists('/data/monitor_state.json') or exit(1)"

ENTRYPOINT ["python", "monitor.py"]
