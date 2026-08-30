# Airflow + DataHub 血缘插件 + 本地验证要用的 provider。
#
# 预装而非用 _PIP_ADDITIONAL_REQUIREMENTS：后者每次容器启动都跑一遍 pip，本机到 PyPI
# 首字节约 9s，启动会被拖成分钟级，且失败只体现在日志里。
#
# 版本对照（已核实，2026-08）：
#   本机 DataHub = v1.6.0（GET http://localhost:8080/config 确认）
#   acryl-datahub-airflow-plugin==1.6.0     → apache-airflow >=2.5.0,<4.0.0（Airflow 2/3 都可）
#   acryl-datahub-airflow-plugin==1.6.0.17  → apache-airflow >=3.0.0,<4.0.0（仅 Airflow 3）
# 故默认 Airflow 2.10.5 + 插件 1.6.0：与 DataHub 主版本对齐，且 REST 为 /api/v1。
# 若改用 Airflow 3，REST 变为 /api/v2，影响面限于 app/connectors/airflow.py 一个模块。

ARG AIRFLOW_IMAGE=apache/airflow:2.10.5
FROM ${AIRFLOW_IMAGE}

ARG DATAHUB_PLUGIN_VERSION=1.6.0

USER airflow
RUN pip install --no-cache-dir \
      "acryl-datahub-airflow-plugin==${DATAHUB_PLUGIN_VERSION}"
