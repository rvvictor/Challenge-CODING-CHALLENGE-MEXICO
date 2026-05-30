FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN npm --prefix frontend ci

COPY . .
RUN npm --prefix frontend run build

ENV NODE_ENV=production
ENV MARKET_MODE=auto
EXPOSE 8000

CMD ["python", "-m", "backend.app.main"]
