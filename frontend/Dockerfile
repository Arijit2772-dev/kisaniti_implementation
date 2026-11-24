FROM node:22.22.2-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG KN_BUILD_COMMIT=unknown
ENV NEXT_TELEMETRY_DISABLED=1 KN_BUILD_COMMIT=$KN_BUILD_COMMIT
RUN npm run build

FROM node:22.22.2-bookworm-slim
WORKDIR /app
ARG KN_BUILD_COMMIT=unknown
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0 KN_BUILD_COMMIT=$KN_BUILD_COMMIT
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
