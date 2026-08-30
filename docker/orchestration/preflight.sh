#!/usr/bin/env bash
# 起栈前的前置检查：镜像能不能拉、已有服务在不在、端口有没有被占。
# 全部只读，不改任何东西。任一项失败都给出可执行的下一步，不只是报错。
set -uo pipefail

cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a

ok=0; warn=0; fail=0
say_ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; ok=$((ok+1)); }
say_warn() { printf "  \033[33m!\033[0m %s\n" "$1"; warn=$((warn+1)); }
say_fail() { printf "  \033[31m✗\033[0m %s\n" "$1"; fail=$((fail+1)); }

echo "== 1. 已在本机运行的依赖服务 =="
if curl -sf -m 8 "${DATAHUB_HEALTH_URL:-http://localhost:8080/config}" >/dev/null; then
  ver=$(curl -s -m 8 "${DATAHUB_HEALTH_URL:-http://localhost:8080/config}" \
        | python3 -c 'import json,sys;print(json.load(sys.stdin)["versions"]["acryldata/datahub"]["version"])' 2>/dev/null || echo "?")
  say_ok "DataHub GMS 可达（$ver）—— 血缘上报目标"
else
  say_fail "DataHub GMS 不可达（:8080）。先起 DataHub quickstart，M11 血浴验证依赖它"
fi
for n in "${DATAHUB_NETWORK:-datahub_network}" "${ERPNEXT_NETWORK:-erpnext_frappe_network}"; do
  if docker network inspect "$n" >/dev/null 2>&1; then say_ok "network $n 存在"
  else say_fail "network $n 不存在 —— compose 里是 external，起不来。用 docker network ls 核对真实名字后改 .env"; fi
done

echo "== 2. 端口占用 =="
for p in 8081 8030 9030 8040; do
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    say_fail "端口 $p 已被占用 —— 改 docker-compose.yml 的端口映射"
  else say_ok "端口 $p 空闲"; fi
done

echo "== 3. 镜像可拉性（本机 Docker Hub 直连实测极慢，务必先过这一关）=="
probe="${IMG_PROBE:-alpine:3.20}"
if docker image inspect "$probe" >/dev/null 2>&1; then
  say_ok "探针镜像 $probe 已在本地，跳过测速"
else
  echo "  正在测速（拉 $probe，最多等 90s）…"
  start=$(date +%s)
  if timeout_bin=$(command -v gtimeout || command -v timeout); then
    "$timeout_bin" 90 docker pull "$probe" >/dev/null 2>&1
  else
    docker pull "$probe" >/dev/null 2>&1 &
    pid=$!; sleep 90; kill $pid 2>/dev/null
  fi
  elapsed=$(( $(date +%s) - start ))
  if docker image inspect "$probe" >/dev/null 2>&1 && [ "$elapsed" -lt 60 ]; then
    say_ok "镜像拉取正常（${elapsed}s）"
  else
    say_fail "镜像拉取过慢/失败（已耗 ${elapsed}s）。airflow/doris 按此速率拉不下来：
       给 .env 的 IMG_* 加镜像源前缀（如 docker.m.daocloud.io/apache/airflow:2.10.5），
       或在 Docker Desktop → Settings → Docker Engine 配 registry-mirrors"
  fi
fi

echo "== 4. 本地已有可复用的镜像 =="
for i in "${IMG_POSTGRES:-postgres:16-alpine}" "${IMG_AIRFLOW:-apache/airflow:2.10.5}" \
         "${IMG_DORIS:-apache/doris:doris-all-in-one-2.1.0}"; do
  if docker image inspect "$i" >/dev/null 2>&1; then say_ok "$i 已在本地"
  else say_warn "$i 需要拉取"; fi
done

printf "\n合计：%d 通过 / %d 提醒 / %d 失败\n" "$ok" "$warn" "$fail"
[ "$fail" -eq 0 ] || { echo "有失败项，先按上面的提示处理再起栈。"; exit 1; }
