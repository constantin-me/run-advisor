FROM node:22-alpine AS webapp-build
WORKDIR /webapp
COPY webapp/package.json webapp/package-lock.json* ./
RUN npm install
COPY webapp/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir uv

COPY backend/pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml || uv pip install --system --no-cache .

COPY backend/app ./app
COPY backend/alembic.ini ./alembic.ini
COPY backend/migrations ./migrations
COPY --from=webapp-build /webapp/dist ./static

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
