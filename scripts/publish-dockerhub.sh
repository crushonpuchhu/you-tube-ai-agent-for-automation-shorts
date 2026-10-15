#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build the latest Shorts Automation Agent image and push it to Docker Hub.
#
#   ./scripts/publish-dockerhub.sh <dockerhub-user>/<repo> [tag]
#
# Examples:
#   ./scripts/publish-dockerhub.sh mittal/shorts-agent           # -> :latest
#   ./scripts/publish-dockerhub.sh mittal/shorts-agent v2        # -> :v2 + :latest
#
# Prerequisites (run once beforehand):
#   1. docker access for your user :  sudo usermod -aG docker $USER   (then re-login)
#   2. Docker Hub login            :  docker login -u <your-user>
#
# What is NOT in the image (guaranteed by .dockerignore): .env, auth/*.json,
# data/tokens/*, *.db — those are always provided at `docker run` time via
# --env-file and -v mounts, never baked into a layer.
# ---------------------------------------------------------------------------
set -euo pipefail

IMAGE="${1:?Usage: $0 <dockerhub-user>/<repo> [tag]   e.g.  $0 mittal/shorts-agent latest}"
TAG="${2:-latest}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> [1/3] Building ${IMAGE}:${TAG} from ${REPO_ROOT}/Dockerfile"
docker build -t "${IMAGE}:${TAG}" .

echo "==> [2/3] Verifying no secrets are inside the image"
if docker history "${IMAGE}:${TAG}" --no-trunc 2>/dev/null | grep -Eiq 'TELEGRAM_BOT_TOKEN|GEMINI_API_KEY=' ; then
    echo "ERROR: something secret-looking ended up in a layer." >&2
    exit 1
fi
echo "    OK — .env / auth / token / db are excluded by .dockerignore"

echo "==> [3/3] Pushing ${IMAGE}:${TAG}"
docker push "${IMAGE}:${TAG}"
if [ "$TAG" != "latest" ]; then
    docker tag "${IMAGE}:${TAG}" "${IMAGE}:latest"
    docker push "${IMAGE}:latest"
fi

SHORT_SHA="$(git rev-parse --short HEAD 2>/dev/null || true)"
if [ -n "$SHORT_SHA" ]; then
    docker tag "${IMAGE}:${TAG}" "${IMAGE}:git-${SHORT_SHA}"
    docker push "${IMAGE}:git-${SHORT_SHA}"
    echo "    also pushed ${IMAGE}:git-${SHORT_SHA} (rollback pointer)"
fi

echo ""
echo "✅ Pushed. On the target server:"
cat <<EOF
    mkdir -p ~/shorts-agent/{auth,data/tokens} ~/.shorts-agent
    # copy these 4 files into ~/shorts-agent/ :
    #   .env   auth/client_secret.json   data/tokens/youtube_token.json   docker-compose.hub.yml
    cd ~/shorts-agent
    sed -i "s|^    image: .*|    image: ${IMAGE}:${TAG}|" docker-compose.hub.yml   # point at YOUR image
    docker compose -f docker-compose.hub.yml pull
    docker compose -f docker-compose.hub.yml up -d
EOF
