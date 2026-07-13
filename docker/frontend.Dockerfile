# Easy Week frontend (Angular PWA) → раздаётся через nginx
FROM node:22-alpine AS build

# playwright (devDep для скриншотов) не должен тянуть браузер в сборке
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build
# Локальная разработка: гасим service worker (иначе кэширует старый бандл).
# safety-worker разрегистрирует уже установленный SW у клиента. На Пае PWA остаётся.
RUN cp dist/frontend/browser/safety-worker.js dist/frontend/browser/ngsw-worker.js \
    && rm -f dist/frontend/browser/ngsw.json

# --- Раздача статики ---
FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/frontend/browser /usr/share/nginx/html
EXPOSE 80
