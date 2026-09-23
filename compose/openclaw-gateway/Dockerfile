# Extends upstream OpenClaw with ffmpeg + openai-whisper for voice transcription.
# Without this, OpenClaw's tools.media.audio config has nothing to shell out to
# and Telegram voice notes silently fail to transcribe.
#
# The OpenClaw version is THIS tag and nothing else. It is a literal (no ARG,
# no compose build-arg) so Dependabot's docker updater can track it
# (.github/dependabot.yml) and open the bump PR; merging that PR is the
# upgrade, CI's deploy job ships it. Do not layer `npm i -g openclaw@x` on top:
# that hid a stale `:latest` base (cached June 2026 while the pin said 6.11)
# and left node/native deps at whatever the old base carried.
#
# In-container `openclaw update` (and the dashboard's "Update now") is the
# wrong surface here on purpose: the image is the unit of deployment, and
# anything installed inside a running container is gone on the next recreate.
FROM ghcr.io/openclaw/openclaw:2026.9.5

USER root

# Enforce pipefail in any RUN that pipes commands (hadolint DL4006).
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        jq \
        python3 \
        python3-venv \
        make \
        g++ \
        libvips-dev \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv keeps us out of PEP 668 territory and away from any
# system-python conflicts inside the upstream image.
RUN python3 -m venv /opt/whisper \
    && /opt/whisper/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/whisper/bin/pip install --no-cache-dir openai-whisper \
    && ln -s /opt/whisper/bin/whisper /usr/local/bin/whisper

# Pre-fetch the `base` model into the runtime user's cache so first
# invocation is instant and works without network. Mirrors PC config.
RUN mkdir -p /home/node/.cache \
    && XDG_CACHE_HOME=/home/node/.cache /opt/whisper/bin/python -c "import whisper; whisper.load_model('base')" \
    && chown -R node:node /home/node/.cache

# Video transcription wrapper: ffmpeg strips audio → whisper transcribes.
# Used by tools.media.video CLI fallback since Claude Pro doesn't cover
# video understanding (matrix is Google/Qwen/Moonshot only).
COPY video-transcribe /usr/local/bin/video-transcribe
RUN chmod 0755 /usr/local/bin/video-transcribe

# gh CLI for the code-task skill (Phase 5.5 lightweight `kind: code` runner —
# clones repos, opens PRs). Auth is bind-mounted from host /home/lifekit/.config/gh.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Headless-Chromium system libraries, required by the Playwright MCP and the
# chrome-devtools MCP. Without these the bundled Chromium fails to launch on a
# bare debian-slim base with cryptic "error while loading shared libraries"
# diagnostics. Install before the USER node switch so apt has the privileges
# it needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libxcomposite1 libxdamage1 libatspi2.0-0 libdrm2 libxkbcommon0 \
        libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Skill CLIs (nutrition-claw, life-state, workout-claw) stay entrypoint-installed
# on every container start (compose/docker-compose.yml): they are bind-mounted
# host checkouts (nutrition-claw/life-state rsync'd under the workspace,
# workout-claw's source repo is archived read-only upstream so /srv/workout-claw
# is the only copy), not present in the build context, so there is nothing for
# the image to bake. This profile.d entry puts the persisted npm-global bin dir
# on the login-shell PATH — Codex runs tool commands via `bash -lc` (login
# shell), whose PATH is rebuilt from /etc/profile and drops the image ENV PATH,
# so the linked CLIs would otherwise be "command not found" under the Codex
# runtime — durable across recreates/rebuilds.
RUN printf 'export PATH="/home/node/.npm-global/bin:$PATH"\n' > /etc/profile.d/zz-skill-clis.sh \
    && chmod 0644 /etc/profile.d/zz-skill-clis.sh

USER node

# Agent CLIs (claude-code, clawhub, summarize) are npm-published, so unlike the
# skill CLIs above they ARE baked into the image: pinned in package.json here,
# installed globally at build time, so a fresh recreate no longer depends on
# the npm registry being reachable or installs whatever version happened to be
# current (lifekit-stack#146). `npm ci` first validates package-lock.json
# against package.json and resolves the exact dependency graph into a local
# node_modules/, offline-reproducible from the lockfile; `npm install -g` from
# those local paths then links the CLIs into NPM_CONFIG_PREFIX without a
# second registry round-trip. claude-code needs --allow-scripts because its
# postinstall fetches the platform-native binary (npm >= 12 blocks install
# scripts by default); without it the CLI installs but every invocation fails
# with "claude native binary not installed" (observed 2026-09-13 at container
# start, same failure mode this build-time install now avoids). Version bumps
# ride Dependabot's npm ecosystem on this directory (.github/dependabot.yml),
# the same systemic route the OpenClaw base tag above uses, rather than
# hand-pinned versions in this RUN line.
ENV NPM_CONFIG_PREFIX=/home/node/.npm-global
ENV PATH="/home/node/.npm-global/bin:${PATH}"
COPY --chown=node:node package.json package-lock.json /home/node/.npm-global-src/
RUN mkdir -p /home/node/.npm-global \
    && cd /home/node/.npm-global-src \
    && npm ci --omit=dev \
    && npm install --global --no-save \
        --allow-scripts=@anthropic-ai/claude-code \
        ./node_modules/@anthropic-ai/claude-code \
        ./node_modules/clawhub \
        ./node_modules/@steipete/summarize \
    && cd /home/node \
    && rm -rf /home/node/.npm-global-src

# Pre-fetch the Playwright Chromium build into the runtime user's cache so
# the playwright-mcp / chrome-devtools-mcp servers don't have to download
# ~150MB on first invocation (and so first calls work without network).
# Runs as `node` so the browser lands under /home/node/.cache/ms-playwright.
RUN npx -y playwright@latest install chromium
