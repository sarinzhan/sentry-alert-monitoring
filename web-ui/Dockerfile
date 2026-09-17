# --- stage 1: build the React app ---
FROM node:20-slim AS build

WORKDIR /ui
COPY package.json package-lock.json ./
# The corporate proxy intercepts TLS with a self-signed CA, so npm can't verify
# registry.npmjs.org's cert (same as pip's --trusted-host in the app image).
RUN npm config set strict-ssl false && npm ci
COPY . .
# vite.config.js outDir is ../app/web (for the in-app deployment); here the
# build stays inside this image, so override it
RUN npm run build -- --outDir dist --emptyOutDir

# --- stage 2: nginx serves the static files and proxies /api to the bot ---
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /ui/dist /usr/share/nginx/html/admin-web
