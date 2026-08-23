# Datalume — Backend (Python + FastAPI)

Backend en Python que conecta el frontend con Supabase (Auth + Base de datos + Storage), y usa **pandas / numpy** para limpiar y analizar los archivos que suben los usuarios.

## Flujo de login (igual al anterior, ahora en Python)

1. `POST /auth/register` — crea el usuario en Supabase Auth
2. `POST /auth/login` — valida correo + contraseña, genera OTP de 6 dígitos, lo envía por correo. No entrega sesión todavía.
3. `POST /auth/verify-otp` — valida el código y entrega `access_token` / `refresh_token`
4. `POST /auth/resend-otp` — reenvía el código

## Procesamiento de datos (pandas + numpy)

`POST /datasets/upload` (requiere `Authorization: Bearer <access_token>`, `multipart/form-data` con el archivo):

1. Lee el CSV/Excel con `pandas`
2. Calcula con pandas/numpy: filas, columnas, nulos por columna, duplicados, tipos de dato, estadísticas (media, desviación estándar, min/max) de columnas numéricas
3. Calcula un `quality_score` (0–100) penalizando nulos y duplicados
4. Sube el archivo original a Supabase Storage (bucket `datasets`)
5. Guarda el resumen en la tabla `datasets`

Esta lógica vive en [`app/utils/data_cleaning.py`](./app/utils/data_cleaning.py) — ahí es donde puedes seguir agregando limpieza más avanzada (imputación de nulos, detección de outliers con numpy, normalización, etc.)

Otros endpoints:
- `GET /datasets` — lista los datasets del usuario autenticado
- `GET /datasets/stats` — estadísticas agregadas para alimentar las tarjetas del dashboard (`StatCard`, gráfico de calidad, etc.)
- `GET /profile/me` — perfil del usuario autenticado

## 1. Configura Supabase

1. Crea un proyecto en [supabase.com](https://supabase.com)
2. En **Project Settings > API** copia `Project URL`, `anon public key` y `service_role key`
3. En **SQL Editor**, corre [`sql/schema.sql`](./sql/schema.sql). Crea las tablas `profiles`, `login_otps`, `datasets` (con columnas de calidad: `null_count`, `duplicate_count`, `quality_score`, `columns_summary`) y el bucket de Storage `datasets`

## 2. Configura el envío de correos

Igual que antes: cualquier proveedor SMTP (Gmail con contraseña de aplicación, Resend, SendGrid, Mailtrap...). Completa las variables `SMTP_*` en tu `.env`.

## 3. Instala y corre

```bash
cd backend-py
cp .env.example .env
# completa las variables

python3 -m venv venv
source venv/bin/activate   # en Windows: venv\Scripts\activate

pip install -r requirements.txt
uvicorn app.main:app --reload --port 4000
```

El servidor queda en `http://localhost:4000`. La documentación interactiva (Swagger) queda disponible automáticamente en `http://localhost:4000/docs`.

## 4. Conecta el frontend

En la carpeta del frontend:

```bash
cp .env.example .env
# VITE_API_URL=http://localhost:4000
```

`src/lib/api.ts` ya incluye `uploadDataset`, `listDatasets` y `getDashboardStats` listos para usar.

## Endpoints

| Método | Ruta                | Descripción                                        |
|--------|---------------------|-----------------------------------------------------|
| POST   | `/auth/register`     | Crea una cuenta nueva                                |
| POST   | `/auth/login`         | Valida correo+contraseña y envía el OTP              |
| POST   | `/auth/verify-otp`    | Valida el OTP y entrega la sesión                    |
| POST   | `/auth/resend-otp`    | Reenvía un nuevo código OTP                          |
| GET    | `/profile/me`         | Perfil del usuario autenticado                       |
| POST   | `/datasets/upload`    | Sube y procesa un CSV/Excel con pandas/numpy         |
| GET    | `/datasets`           | Lista los datasets del usuario                       |
| GET    | `/datasets/stats`     | Estadísticas agregadas para el dashboard             |
| GET    | `/health`             | Chequeo de salud                                     |
