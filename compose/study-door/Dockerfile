# study-door - the browser study panel's door (docs/runbook.md "Study panel and
# study door"): one authenticated POST /v1/turn in, one gated reply
# out. Node only, no dependencies. Its secrets come from the env, never the image.

FROM node:22-trixie-slim

WORKDIR /app
COPY package.json server.js app.js http.js schema.js sanitize.js gates.js prompt.js gateway.js /app/

# The audit spool's mount point: a fresh named volume copies this ownership.
RUN mkdir /spool && chown node:node /spool && chmod 700 /spool

ENV STUDY_DOOR_PORT=8098

EXPOSE 8098

USER node
CMD ["node", "server.js"]
