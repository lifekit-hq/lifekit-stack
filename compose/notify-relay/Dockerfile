# notify-relay — turns devclaw's notify_url POST into a Telegram message.
#
# Direct call to the Telegram Bot API (api.telegram.org/bot<TOKEN>/sendMessage).
# No docker socket, no inter-container exec, no extra binaries.
# Just Node + HTTP. The bot token comes from $TELEGRAM_BOT_TOKEN in the env.

FROM node:22-trixie-slim

WORKDIR /app
COPY server.js render.js task-row.js /app/

ENV NOTIFY_RELAY_PORT=8090

EXPOSE 8090

USER node
CMD ["node", "server.js"]
