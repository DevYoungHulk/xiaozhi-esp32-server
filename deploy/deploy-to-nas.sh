#!/bin/bash
# 从 xiaozhi-esp32-server 的 dev-nas 分支打包差异文件，部署到 NAS
set -e

NAS_HOST="crush@192.168.100.4"
NAS_ROOT="/vol1/1000/Docker/xiaozhi-server"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_REPO="$(dirname "$SCRIPT_DIR")"
COMPOSE_TEMPLATE="$SCRIPT_DIR/docker-compose.yml"
SRC_PREFIX="main/xiaozhi-server/"  # git repo 里的源码前缀
CTR_PREFIX="/opt/xiaozhi-esp32-server/"
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

echo "=== 1. 获取 dev-nas 相对 origin/main 的差异文件 ==="
cd "$SERVER_REPO"
git checkout dev-nas
DIFF_FILES=$(git diff origin/main --name-only | grep '\.py$' || true)

if [ -z "$DIFF_FILES" ]; then
    echo "没有差异文件，无需部署"
    exit 0
fi

echo "差异文件:"
echo "$DIFF_FILES"

echo ""
echo "=== 2. 打包差异文件到 $TEMP_DIR/fix/ ==="
FIX_DIR="$TEMP_DIR/fix"
echo "$DIFF_FILES" | while IFS= read -r file; do
    rel="${file#$SRC_PREFIX}"           # 去前缀: core/providers/asr/base.py
    mkdir -p "$FIX_DIR/$(dirname "$rel")"
    cp "$SERVER_REPO/$file" "$FIX_DIR/$rel"
    echo "  -> fix/$rel"
done

PACKAGE="xiaozhi-fix-$(date +%Y%m%d-%H%M%S).tar.gz"
cd "$TEMP_DIR"
tar czf "/tmp/$PACKAGE" fix/

echo ""
echo "=== 3. 生成 docker-compose.yml ==="
cp "$COMPOSE_TEMPLATE" "$TEMP_DIR/docker-compose.yml"

echo "$DIFF_FILES" | while IFS= read -r file; do
    rel="${file#$SRC_PREFIX}"
    MOUNT="      - ./fix/$rel:$CTR_PREFIX$rel"
    if ! grep -qF "$MOUNT" "$TEMP_DIR/docker-compose.yml"; then
        sed -i "s|      # === fix-mounts|${MOUNT}\n      # === fix-mounts|" "$TEMP_DIR/docker-compose.yml"
        echo "  mount: $rel"
    fi
done

echo ""
echo "=== 4. 上传到 NAS ==="
scp "/tmp/$PACKAGE" "$NAS_HOST:/tmp/$PACKAGE"
scp "$TEMP_DIR/docker-compose.yml" "$NAS_HOST:$NAS_ROOT/docker-compose.yml"
# 同步 deploy 目录下的配置文件 (voiceprint 等)
if [ -d "$SERVER_REPO/deploy/voiceprint" ]; then
    ssh "$NAS_HOST" "mkdir -p $NAS_ROOT/voiceprint/data"
    scp -r "$SERVER_REPO/deploy/voiceprint/"* "$NAS_HOST:$NAS_ROOT/voiceprint/"
    echo "  deploy/voiceprint -> NAS"
fi
if [ -f "$SERVER_REPO/deploy/.config.yaml" ]; then
    scp "$SERVER_REPO/deploy/.config.yaml" "$NAS_HOST:$NAS_ROOT/data/.config.yaml"
    echo "  deploy/.config.yaml -> NAS"
fi

echo ""
echo "=== 5. NAS 上解压并重启 ==="
ssh "$NAS_HOST" << REMOTE
    cd "$NAS_ROOT"
    tar xzf "/tmp/$PACKAGE"
    rm "/tmp/$PACKAGE"
    docker compose up -d xiaozhi-esp32-server
REMOTE

echo ""
echo "=== 部署完成 ==="
ssh "$NAS_HOST" "docker ps --filter name=xiaozhi-esp32-server --format 'table {{.Names}}\t{{.Status}}'"
