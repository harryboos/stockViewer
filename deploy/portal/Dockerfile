ARG NODE_BASE_IMAGE=node:22-bookworm-slim
FROM ${NODE_BASE_IMAGE}

ENV NODE_ENV=production \
    PORT=3000

WORKDIR /app
COPY --chown=node:node package.json server.mjs ./
COPY --chown=node:node public ./public

USER node
EXPOSE 3000
CMD ["node", "server.mjs"]
