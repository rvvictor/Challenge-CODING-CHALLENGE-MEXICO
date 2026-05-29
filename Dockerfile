FROM node:20-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY . .

ENV NODE_ENV=production
ENV MARKET_MODE=auto
EXPOSE 3000

CMD ["npm", "start"]
