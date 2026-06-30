FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# 1Password CLI — resolves the op:// references in .env.render.tpl at container start (see CMD).
# Arch-aware (dpkg arch matches 1Password's amd64/arm64 naming) so the image builds on Render
# (amd64) and locally on Apple Silicon (arm64) alike. The download is checksum-verified against the
# pinned version's known SHA256 so a tampered/substituted artifact fails the build. curl/unzip are
# purged to keep the layer lean.
ARG OP_VERSION=v2.30.0
ARG OP_SHA256_amd64=cd5361b074cd40eb2b332885f35a4d61c74369919ced95190c885f4d4f739dc7
ARG OP_SHA256_arm64=c0618a4d4defa5d61606dfb4eaf7d5f39cf6361382c4943449df95fa1f7cc310
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && ARCH="$(dpkg --print-architecture)" \
 && curl -sSfLo /tmp/op.zip "https://cache.agilebits.com/dist/1P/op2/pkg/${OP_VERSION}/op_linux_${ARCH}_${OP_VERSION}.zip" \
 && eval "EXPECTED=\$OP_SHA256_${ARCH}" \
 && echo "${EXPECTED}  /tmp/op.zip" | sha256sum -c - \
 && unzip -od /usr/local/bin /tmp/op.zip op \
 && rm /tmp/op.zip \
 && apt-get purge -y curl unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml constraints.txt ./
COPY app ./app
COPY scripts ./scripts
COPY alembic.ini ./
COPY alembic ./alembic
COPY .env.render.tpl ./
# Constraints pin the ML stack to the versions the committed model pickle was built with.
RUN pip install --upgrade pip && pip install . -c constraints.txt

# op run injects the resolved secrets into the env, then the inner shell applies migrations and
# serves. ${PORT} comes from Render's injected env (op run passes the existing env through), not
# from 1Password. Migration is idempotent — `upgrade head` is a no-op when already current.
# Requires the OP_SERVICE_ACCOUNT_TOKEN env var (set in Render); local docker-compose overrides
# this CMD, so it never needs the token.
CMD ["sh", "-c", "op run --env-file=.env.render.tpl -- sh -c 'alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-3000}'"]
