# learn-mcp - the browser study coach's claude.ai connector over learnclaw's
# learning record (docs/runbook.md, "learn-mcp (the study coach's connector)").
# Two MCP tools over streamable HTTP; no secret in the image (the bearer comes
# from the env).
#
# The skills are copied from the repo's skills/ (the compose file's `skills`
# build context), so the connector writes through the very record.py learnclaw
# uses. git and the POSIX tools (sh, awk, grep, sed, find) are the vault
# lint's, which record.py runs after every append.

FROM python:3.13-slim-trixie

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY --from=skills learning-record/record.py /app/skills/learning-record/record.py
COPY --from=skills leetcode-daily/daily.py leetcode-daily/problems.json /app/skills/leetcode-daily/
COPY server.py context.py /app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LEARN_MCP_PORT=8099 \
    HOME=/tmp

EXPOSE 8099

# The vault's owner (lifekit, uid 1000): rows land as the same user the sync
# timer commits as.
USER 1000:1000
CMD ["python", "/app/server.py"]
