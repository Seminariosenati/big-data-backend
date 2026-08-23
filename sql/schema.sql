-- =========================================================
-- Datalume — esquema inicial para Supabase
-- Ejecutar en: Supabase Dashboard > SQL Editor
-- =========================================================

-- Extensión para generar UUIDs
create extension if not exists "pgcrypto";

-- ---------------------------------------------------------
-- Perfiles de usuario (datos extra que no van en auth.users)
-- ---------------------------------------------------------
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  full_name text,
  company text,
  created_at timestamptz not null default now()
);

alter table public.profiles enable row level security;

create policy "Los usuarios pueden ver su propio perfil"
  on public.profiles for select
  using (auth.uid() = id);

create policy "Los usuarios pueden actualizar su propio perfil"
  on public.profiles for update
  using (auth.uid() = id);

-- ---------------------------------------------------------
-- Códigos OTP pendientes (login en dos pasos por correo)
-- El backend usa la service_role key para leer/escribir aquí,
-- por lo que no necesita políticas RLS abiertas al público.
-- ---------------------------------------------------------
create table if not exists public.login_otps (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  email text not null,
  code_hash text not null,
  attempts int not null default 0,
  max_attempts int not null default 5,
  -- tokens de sesión temporales, obtenidos tras validar la contraseña,
  -- y que solo se entregan al frontend cuando el OTP es correcto
  pending_access_token text,
  pending_refresh_token text,
  expires_at timestamptz not null,
  consumed_at timestamptz,
  created_at timestamptz not null default now()
);

alter table public.login_otps enable row level security;
-- Sin policies públicas: solo la service_role (backend) puede leer/escribir.

create index if not exists login_otps_user_id_idx on public.login_otps(user_id);
create index if not exists login_otps_expires_at_idx on public.login_otps(expires_at);

-- ---------------------------------------------------------
-- Archivos cargados por el usuario (CSV / Excel)
-- ---------------------------------------------------------
create table if not exists public.datasets (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  file_name text not null,
  storage_path text not null,
  company text,
  row_count int not null default 0,
  column_count int not null default 0,
  null_count int not null default 0,
  duplicate_count int not null default 0,
  quality_score numeric(5,2),
  columns_summary jsonb,
  status text not null default 'processing' check (status in ('processing', 'ok', 'warn', 'error')),
  size_bytes bigint not null default 0,
  created_at timestamptz not null default now()
);

alter table public.datasets enable row level security;

create policy "Los usuarios ven solo sus propios datasets"
  on public.datasets for select
  using (auth.uid() = user_id);

create policy "Los usuarios insertan sus propios datasets"
  on public.datasets for insert
  with check (auth.uid() = user_id);

create policy "Los usuarios borran sus propios datasets"
  on public.datasets for delete
  using (auth.uid() = user_id);

-- ---------------------------------------------------------
-- Trigger: crear perfil automáticamente al registrar un usuario
-- ---------------------------------------------------------
create or replace function public.handle_new_user()
returns trigger as $$
begin
  insert into public.profiles (id, full_name, company)
  values (
    new.id,
    new.raw_user_meta_data ->> 'full_name',
    new.raw_user_meta_data ->> 'company'
  );
  return new;
end;
$$ language plpgsql security definer;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

-- ---------------------------------------------------------
-- Storage bucket para los archivos subidos
-- (también se puede crear desde el Dashboard > Storage)
-- ---------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('datasets', 'datasets', false)
on conflict (id) do nothing;

create policy "Los usuarios suben a su propia carpeta"
  on storage.objects for insert
  with check (bucket_id = 'datasets' and (storage.foldername(name))[1] = auth.uid()::text);

create policy "Los usuarios leen su propia carpeta"
  on storage.objects for select
  using (bucket_id = 'datasets' and (storage.foldername(name))[1] = auth.uid()::text);
