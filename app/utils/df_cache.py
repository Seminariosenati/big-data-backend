"""Caché en memoria (por proceso) para DataFrames ya leídos.

Varios endpoints de /datasets piden el mismo archivo (crudo o limpio) por
separado en cuestión de segundos — por ejemplo, al entrar al Dashboard se
piden la vista previa, las columnas del gráfico y los datos del gráfico
del mismo dataset casi al mismo tiempo, y cada uno descargaba el CSV de
Supabase Storage y lo volvía a parsear con pandas desde cero. Este módulo
evita ese trabajo repetido guardando el DataFrame ya leído por un rato
corto (TTL), y se invalida explícitamente en cuanto el dataset cambia de
verdad (se vuelve a limpiar).

Vive en memoria del proceso: si el backend corre en un solo worker (lo
normal en un plan gratuito/pequeño) esto ya ayuda mucho; si en el futuro
corre con varios workers, cada uno tendría su propia copia del caché, lo
cual sigue siendo correcto (nunca sirve datos más viejos que el TTL), solo
que el ahorro sería por worker en vez de global.
"""

import threading
import time

_lock = threading.Lock()
_store: dict[str, tuple[float, object]] = {}

DEFAULT_TTL_SECONDS = 120


def cache_get(key: str):
    with _lock:
        entry = _store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            del _store[key]
            return None
        return value


def cache_set(key: str, value, ttl: int = DEFAULT_TTL_SECONDS) -> None:
    with _lock:
        _store[key] = (time.time() + ttl, value)


def cache_invalidate(key: str) -> None:
    with _lock:
        _store.pop(key, None)


def cache_invalidate_prefix(prefix: str) -> None:
    """Borra todas las entradas cuyo key empiece con `prefix` (p. ej. todo
    lo relacionado a un dataset puntual, sin tener que conocer cada key)."""
    with _lock:
        for key in [k for k in _store if k.startswith(prefix)]:
            del _store[key]