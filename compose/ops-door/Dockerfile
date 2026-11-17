# ops-door - the ops queue's door: an Alertmanager-compatible webhook becomes a
# lifekit-stack GitHub issue (docs/runbook.md "Ops door"). Node only, no
# dependencies. The GitHub token comes from the env, never the image.

FROM node:22-trixie-slim

WORKDIR /app
COPY package.json server.js app.js /app/

ENV OPS_DOOR_PORT=8097

EXPOSE 8097

USER node
CMD ["node", "server.js"]
