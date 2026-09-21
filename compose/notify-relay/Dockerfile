# notify-relay — renders message envelopes (docs/message-format.md) and the
# legacy devclaw payloads to Telegram HTML and posts them to the Bot API.
#
# Direct call to the Telegram Bot API (api.telegram.org/bot<TOKEN>/sendMessage).
# No docker socket, no inter-container exec, no extra binaries.
# Just Node + HTTP. The bot token comes from $TELEGRAM_BOT_TOKEN in the env.

FROM node:22-trixie-slim

WORKDIR /app
COPY package.json server.js app.js render.js /app/

ENV NOTIFY_RELAY_PORT=8090

EXPOSE 8090

USER node
CMD ["node", "server.js"]
