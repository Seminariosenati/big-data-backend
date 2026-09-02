import io
import json
import numpy as np
import pandas as pd


def parse_mixed_dates(series: pd.Series) -> pd.Series:
    """Convierte una columna de texto con fechas a datetime, soportando que
    el mismo archivo mezcle formatos ISO ('2026-01-07') y DD/MM/YYYY
    ('11/01/2026').

    OJO: pd.to_datetime(..., format="mixed", dayfirst=True) NO sirve para
    esto — pandas le aplica "día primero" también a las fechas ISO (que no
    lo necesitan porque YYYY-MM-DD no es ambiguo) e invierte mes/día por
    error (ej. '2026-01-07' termina en 1 de julio en vez de 7 de enero).

    Por eso cada valor se clasifica por su patrón antes de parsear:
    - 'YYYY-MM-DD' -> se parsea SIN dayfirst (el orden ya es explícito).
    - 'DD/MM/YYYY' (u otro con '/') -> se parsea CON dayfirst=True.
    - cualquier otro formato -> fallback genérico con dayfirst=True.
    """
    text = series.astype(str).str.strip()
    iso_mask = text.str.match(r"^\d{4}-\d{1,2}-\d{1,2}$")
    slash_mask = text.str.match(r"^\d{1,2}/\d{1,2}/\d{4}$")
    other_mask = ~iso_mask & ~slash_mask

    result = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    if iso_mask.any():
        result.loc[iso_mask] = pd.to_datetime(text[iso_mask], format="%Y-%m-%d", errors="coerce")
    if slash_mask.any():
        result.loc[slash_mask] = pd.to_datetime(text[slash_mask], format="%d/%m/%Y", errors="coerce")
    if other_mask.any():
        result.loc[other_mask] = pd.to_datetime(text[other_mask], errors="coerce", dayfirst=True)
    return result


def read_dataset(file_bytes: bytes, file_name: str) -> pd.DataFrame:
    """Lee un CSV o Excel a un DataFrame de pandas."""
    if file_name.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(file_bytes))
    return pd.read_csv(io.BytesIO(file_bytes))


def analyze_dataset(df: pd.DataFrame) -> dict:
    """
    Genera un resumen de calidad de datos usando pandas/numpy:
    - conteo de filas/columnas
    - valores nulos por columna
    - filas duplicadas
    - tipos de datos
    - score de calidad (0-100)
    """
    row_count = int(df.shape[0])
    column_count = int(df.shape[1])

    null_counts = df.isnull().sum()
    total_nulls = int(null_counts.sum())

    duplicate_count = int(df.duplicated().sum())

    columns_summary = []
    for col in df.columns:
        series = df[col]
        col_info = {
            "name": str(col),
            "dtype": str(series.dtype),
            "null_count": int(series.isnull().sum()),
            "null_pct": round(float(series.isnull().mean() * 100), 2),
            "unique_count": int(series.nunique()),
        }

        # Estadísticas adicionales para columnas numéricas
        if np.issubdtype(series.dtype, np.number):
            clean = series.dropna()
            if not clean.empty:
                col_info.update(
                    {
                        "mean": round(float(clean.mean()), 4),
                        "std": round(float(clean.std()), 4) if len(clean) > 1 else 0.0,
                        "min": round(float(clean.min()), 4),
                        "max": round(float(clean.max()), 4),
                    }
                )

        columns_summary.append(col_info)

    total_cells = row_count * column_count if column_count else 1
    null_ratio = total_nulls / total_cells if total_cells else 0
    duplicate_ratio = duplicate_count / row_count if row_count else 0

    # Score simple de calidad: penaliza nulos y duplicados.
    # null_ratio y duplicate_ratio ya son fracciones (0-1). Los pesos (60/40)
    # representan la penalización MÁXIMA en puntos si el ratio fuera 100%,
    # así que NO hay que volver a multiplicar por 100 (antes se hacía dos
    # veces y un dataset con apenas 1-2% de nulos/duplicados terminaba con
    # score 0, como si estuviera lleno de errores).
    quality_score = round(max(0.0, 100 - (null_ratio * 60 + duplicate_ratio * 40)), 2)

    if quality_score >= 90:
        status = "ok"
    elif quality_score >= 70:
        status = "warn"
    else:
        status = "error"

    return {
        "row_count": row_count,
        "column_count": column_count,
        "null_count": total_nulls,
        "duplicate_count": duplicate_count,
        "quality_score": quality_score,
        "status": status,
        "columns_summary": columns_summary,
    }


def clean_dataset_with_options(df: pd.DataFrame, options: dict) -> tuple[pd.DataFrame, dict]:
    """
    Limpieza real, controlada por opciones (lo que llega desde el frontend).
    Nunca borra datos en silencio: devuelve también un `log` con todo lo que
    se quitó/cambió (filas, columnas, valores), para poder guardarlo en la
    tabla `cleaning_logs` en vez de perderlo para siempre.

    options:
      remove_duplicates: bool
      null_strategy: 'ignore' | 'remove_row' | 'set_null' | 'zero' | 'average'
      convert_number: bool
      convert_dates: bool
      remove_empty_columns: bool
    """
    working = df.copy()
    log = {
        "duplicate_rows_removed": [],
        "empty_rows_removed": [],
        "columns_removed": [],
        "nulls_filled_count": 0,
        "nulls_filled_cells": [],
    }

    # Nombre de una columna "identificadora" (la primera) para poder mostrar
    # a qué fila pertenece cada celda modificada en el historial, sin tener
    # que exponer la fila completa.
    ref_col = working.columns[0] if len(working.columns) else None

    def _log_null_cells(mask: pd.DataFrame, after: pd.DataFrame) -> None:
        rows, cols = np.where(mask.to_numpy())
        for r, c in zip(rows, cols):
            col_name = mask.columns[c]
            row_label = mask.index[r]
            log["nulls_filled_cells"].append(
                {
                    "fila": str(working.loc[row_label, ref_col]) if ref_col is not None else str(row_label),
                    "columna": str(col_name),
                    "antes": "(vacío)",
                    "despues": None if pd.isna(after.loc[row_label, col_name]) else str(after.loc[row_label, col_name]),
                }
            )

    if options.get("remove_duplicates"):
        dup_mask = working.duplicated(keep="first")
        if dup_mask.any():
            log["duplicate_rows_removed"] = json.loads(working[dup_mask].to_json(orient="records"))
            working = working[~dup_mask]

    null_strategy = options.get("null_strategy", "ignore")

    if null_strategy == "remove_row":
        empty_mask = working.isnull().any(axis=1)
        if empty_mask.any():
            log["empty_rows_removed"] = json.loads(working[empty_mask].to_json(orient="records"))
            working = working[~empty_mask]

    elif null_strategy == "set_null":
        empty_mask = working.isnull()
        log["nulls_filled_count"] = int(empty_mask.sum().sum())
        # Texto literal "null" (visible), no un None/NaN invisible en la tabla.
        working = working.astype(object).mask(empty_mask, "null")
        _log_null_cells(empty_mask, working)

    elif null_strategy == "zero":
        numeric_cols = working.select_dtypes(include="number").columns
        empty_mask = working[numeric_cols].isnull()
        log["nulls_filled_count"] = int(empty_mask.sum().sum())
        working[numeric_cols] = working[numeric_cols].fillna(0)
        full_mask = pd.DataFrame(False, index=working.index, columns=working.columns)
        full_mask[numeric_cols] = empty_mask
        _log_null_cells(full_mask, working)

    elif null_strategy == "average":
        numeric_cols = working.select_dtypes(include="number").columns
        empty_mask = working[numeric_cols].isnull()
        log["nulls_filled_count"] = int(empty_mask.sum().sum())
        working[numeric_cols] = working[numeric_cols].fillna(working[numeric_cols].mean())
        full_mask = pd.DataFrame(False, index=working.index, columns=working.columns)
        full_mask[numeric_cols] = empty_mask
        _log_null_cells(full_mask, working)


    if options.get("convert_number"):
        for col in working.select_dtypes(include="object").columns:
            converted = pd.to_numeric(working[col], errors="coerce")
            if converted.notna().sum() > 0:
                working[col] = converted.where(converted.notna(), working[col])

    if options.get("convert_dates"):
        for col in working.select_dtypes(include="object").columns:
            non_null = working[col].dropna()
            non_null = non_null[non_null.astype(str).str.strip() != ""]
            if non_null.empty:
                continue

            # Si la columna es mayormente numérica (edad, id, etc.), no es una
            # columna de fechas: pandas interpretaría esos números como
            # nanosegundos desde 1970-01-01 y los convertiría por error.
            numeric_like = pd.to_numeric(non_null, errors="coerce")
            if numeric_like.notna().mean() > 0.5:
                continue

            converted = parse_mixed_dates(working[col])
            if converted.notna().sum() > 0:
                working[col] = converted.dt.strftime("%Y-%m-%d").where(converted.notna(), working[col])

    if options.get("remove_empty_columns"):
        empty_cols = [c for c in working.columns if working[c].isnull().all()]
        if empty_cols:
            log["columns_removed"] = empty_cols
            working = working.drop(columns=empty_cols)

    return working, log


def detect_chart_columns(dfs: list[pd.DataFrame]) -> list[dict]:
    """Une las columnas de todos los datasets ya limpios (`dfs`) y clasifica
    cada una como 'numeric' o 'categorical', para poblar el selector de
    columna de los gráficos del dashboard."""
    info: dict[str, dict] = {}

    for df in dfs:
        for col in df.columns:
            col = str(col)
            series = df[col]
            entry = info.setdefault(col, {"numeric_votes": 0, "total_votes": 0})
            entry["total_votes"] += 1
            if np.issubdtype(series.dtype, np.number):
                entry["numeric_votes"] += 1

    columns = [
        {"name": col, "type": "numeric" if entry["numeric_votes"] == entry["total_votes"] else "categorical"}
        for col, entry in info.items()
    ]
    return sorted(columns, key=lambda c: c["name"].lower())


def aggregate_chart_column(dfs: list[pd.DataFrame], column: str, bins: int = 8, top_n: int = 10) -> dict | None:
    """Combina los valores de `column` a través de TODOS los datasets ya
    limpios y devuelve datos listos para graficar: histograma si la columna
    es numérica, conteo de categorías (top N + 'Otros') si es texto.
    Devuelve None si la columna no existe en ningún dataset limpio."""
    matching = [df[column] for df in dfs if column in df.columns]
    if not matching:
        return None

    combined = pd.concat(matching, ignore_index=True).dropna()
    if combined.empty:
        return {"column": column, "type": "categorical", "data": []}

    if np.issubdtype(combined.dtype, np.number):
        unique_count = combined.nunique()
        bucket_count = max(1, min(bins, unique_count))
        counts, edges = np.histogram(combined, bins=bucket_count)
        data = [
            {"label": f"{edges[i]:.2f} – {edges[i + 1]:.2f}", "value": int(counts[i])}
            for i in range(len(counts))
        ]
        return {"column": column, "type": "numeric", "data": data}

    value_counts = combined.astype(str).value_counts()
    top = value_counts.head(top_n)
    data = [{"label": str(k), "value": int(v)} for k, v in top.items()]
    other_count = int(value_counts.iloc[top_n:].sum())
    if other_count > 0:
        data.append({"label": "Otros", "value": other_count})
    return {"column": column, "type": "categorical", "data": data}


def _detect_join_key_candidates(own_df: pd.DataFrame, other_df: pd.DataFrame, min_match_pct: float = 15.0) -> list[dict]:
    """Busca columnas que existen en AMBOS DataFrames (por nombre) y mide
    qué tanto se superponen sus valores reales, para sugerir cuál usar como
    "clave" al cruzar las filas de los dos datasets (ej. cliente_id,
    producto). Un nombre de columna igual no basta si el contenido no tiene
    nada que ver: por eso se exige un mínimo de coincidencia real.

    Además, prioriza columnas que de verdad IDENTIFICAN una fila (valores
    poco repetidos, ej. cliente_id) sobre columnas categóricas de pocos
    valores (ej. producto, categoria): usar una columna muy repetida como
    clave hace que cada fila se cruce con decenas de filas del otro
    archivo ("fan-out"), lo que arruina cualquier cálculo posterior.
    """
    own_cols = {str(c).strip().lower(): c for c in own_df.columns}
    other_cols = {str(c).strip().lower(): c for c in other_df.columns}
    shared = [key for key in own_cols if key in other_cols]

    candidates = []
    for key in shared:
        own_col = own_df[own_cols[key]].dropna().astype(str).str.strip()
        other_col = other_df[other_cols[key]].dropna().astype(str).str.strip()
        if own_col.empty or other_col.empty:
            continue

        own_values = set(own_col)
        other_values = set(other_col)
        overlap = own_values & other_values
        if not overlap:
            continue

        match_pct = round(len(overlap) / len(other_values) * 100, 1)
        if match_pct < min_match_pct:
            continue

        # qué tan "identificatoria" es esta columna en tu propio dataset:
        # cerca de 1.0 = casi cada fila tiene un valor distinto (buena
        # clave); cerca de 0 = muy pocos valores repetidos muchas veces
        # (mala clave, ej. una categoría).
        uniqueness = round(len(own_values) / len(own_col), 3)

        candidates.append(
            {
                "column": own_cols[key],
                "match_pct": match_pct,
                "own_unique_values": len(own_values),
                "other_unique_values": len(other_values),
                "uniqueness": uniqueness,
            }
        )

    # candidatas "buenas" primero (identifican filas puntuales), y entre
    # ellas, mejor coincidencia primero. Solo si NINGUNA columna compartida
    # es razonablemente identificatoria, se cae a mostrar todas ordenadas
    # solo por coincidencia (mejor tener algo que nada).
    good = [c for c in candidates if c["uniqueness"] >= 0.15]
    pool = good if good else candidates
    pool.sort(key=lambda c: (-c["uniqueness"], -c["match_pct"]))

    for c in pool:
        del c["uniqueness"]  # detalle interno, no hace falta mandarlo al frontend
    return pool


def build_enriched_preview(
    own_df: pd.DataFrame,
    other_df: pd.DataFrame,
    join_key: str,
    bring_columns: list[str],
    row_limit: int = 50,
) -> dict | None:
    """Arma una tabla TEMPORAL en memoria: le "pega" a `own_df` una o más
    columnas de `other_df` (ej. la oferta del día de otro CSV), cruzando las
    filas por `join_key` (ej. cliente_id, producto). Ninguno de los dos
    DataFrames originales se modifica, y nada de esto se persiste — se
    recalcula en cada request y se descarta al responder. Devuelve None si
    la columna clave no existe en ambos archivos.
    """
    own_cols_norm = {str(c).strip().lower(): c for c in own_df.columns}
    other_cols_norm = {str(c).strip().lower(): c for c in other_df.columns}

    key_norm = join_key.strip().lower()
    if key_norm not in own_cols_norm or key_norm not in other_cols_norm:
        return None

    own_key = own_cols_norm[key_norm]
    other_key = other_cols_norm[key_norm]

    valid_bring = [c for c in bring_columns if c in other_df.columns]
    if not valid_bring:
        return None

    left = own_df.copy()
    right = other_df[[other_key, *valid_bring]].copy()

    # Normaliza la clave a texto para poder cruzar aunque un archivo la
    # tenga como número y el otro como texto (ej. 1037 vs "1037").
    left["_join_key"] = left[own_key].astype(str).str.strip()
    right["_join_key"] = right[other_key].astype(str).str.strip()
    right = right.drop_duplicates(subset="_join_key", keep="first")

    # Si alguna de las columnas a traer ya existe con ese nombre, no la pisa:
    # la agrega con un sufijo para dejar claro que es la version temporal.
    rename_map: dict[str, str] = {}
    for col in valid_bring:
        final_name = col if col not in own_df.columns else f"{col} (temporal)"
        n = 2
        while final_name in own_df.columns or final_name in rename_map.values():
            final_name = f"{col} (temporal {n})"
            n += 1
        rename_map[col] = final_name

    right = right.rename(columns=rename_map)
    added_names = list(rename_map.values())

    merged = left.merge(right[["_join_key", *added_names]], on="_join_key", how="left")
    merged = merged.drop(columns=["_join_key"])

    matched_rows = int(merged[added_names[0]].notna().sum()) if added_names else 0

    return {
        "columns": [str(c) for c in merged.columns],
        "addedColumns": added_names,
        "totalRows": int(merged.shape[0]),
        "matchedRows": matched_rows,
        "rows": json.loads(merged.head(row_limit).to_json(orient="records", date_format="iso")),
    }


# ---------------------------------------------------------------------------
# Comparación entre empresas del mismo rubro (benchmarking)
#
# IMPORTANTE: nada de lo que hay aquí toca la base de datos. El CSV de la
# "otra empresa" solo vive como DataFrame en memoria durante esta función;
# no se guarda en Storage, no se inserta en `datasets`, no se sube a ningún
# lado. Al terminar la función (o la request), Python libera esa memoria.
# Esto es intencional: el rol 'analyst' puede comparar datos pero jamás
# puede escribir/borrar nada en el sistema.
# ---------------------------------------------------------------------------

SALES_COLUMN_CANDIDATES = ["ventas", "venta", "sales", "total_venta", "monto", "importe", "total"]
DATE_COLUMN_CANDIDATES = ["fecha", "date", "fecha_venta"]
CATEGORY_COLUMN_CANDIDATES = ["categoria", "categoría", "category", "producto", "product"]


def _find_sales_column(df: pd.DataFrame) -> str | None:
    """Busca una columna numérica que probablemente represente ventas."""
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for candidate in SALES_COLUMN_CANDIDATES:
        if candidate in lower_map:
            col = lower_map[candidate]
            if np.issubdtype(df[col].dtype, np.number):
                return col
    return None


def _find_date_column(df: pd.DataFrame) -> str | None:
    """Prioriza columnas ya convertidas a datetime; si no hay, busca por
    nombre entre las candidatas típicas."""
    for col in df.columns:
        if np.issubdtype(df[col].dtype, np.datetime64):
            return col

    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for candidate in DATE_COLUMN_CANDIDATES:
        if candidate in lower_map:
            return lower_map[candidate]
    return None


def _find_category_column(df: pd.DataFrame) -> str | None:
    """Busca una columna categórica (texto) típica de producto/categoría."""
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for candidate in CATEGORY_COLUMN_CANDIDATES:
        if candidate in lower_map:
            col = lower_map[candidate]
            if not np.issubdtype(df[col].dtype, np.number):
                return col
    return None


def compute_sales_summary(df: pd.DataFrame) -> dict | None:
    """Calcula KPIs de ventas reales (no de calidad de datos) a partir de la
    versión limpia de un dataset: ventas totales, ticket promedio, categoría
    top y tendencia mensual. Devuelve None si no se pudo identificar una
    columna de monto (ninguna de las candidatas de SALES_COLUMN_CANDIDATES
    está presente como columna numérica)."""
    sales_col = _find_sales_column(df)
    if sales_col is None:
        return None

    sales = pd.to_numeric(df[sales_col], errors="coerce").dropna()
    if sales.empty:
        return None

    total_sales = float(sales.sum())
    avg_ticket = float(sales.mean())
    row_count = int(sales.shape[0])

    top_category = None
    category_col = _find_category_column(df)
    if category_col is not None:
        grouped = df.groupby(category_col)[sales_col].sum(numeric_only=True).sort_values(ascending=False)
        grouped = grouped.dropna()
        if not grouped.empty:
            top_category = {"name": str(grouped.index[0]), "total": float(grouped.iloc[0])}

    monthly: list[dict] = []
    trend_pct = None
    has_daily_detail = False
    date_col = _find_date_column(df)
    if date_col is not None:
        working = df[[date_col, sales_col]].copy()
        working[date_col] = parse_mixed_dates(working[date_col])
        working[sales_col] = pd.to_numeric(working[sales_col], errors="coerce")
        working = working.dropna()
        if not working.empty:
            working["month"] = working[date_col].dt.to_period("M").astype(str)
            monthly_totals = working.groupby("month")[sales_col].sum().sort_index()
            monthly = [{"month": m, "total": float(v)} for m, v in monthly_totals.items()]
            if len(monthly_totals) >= 2:
                prev, last = monthly_totals.iloc[-2], monthly_totals.iloc[-1]
                if prev > 0:
                    trend_pct = round(((last - prev) / prev) * 100, 1)

            # El dataset "tiene detalle diario" si, dentro de al menos un
            # mes, existe más de un día calendario distinto. Si cada mes
            # solo trae una fecha (o todas caen en el mismo día), no hay
            # nada que desglosar por día y el frontend debe quedarse solo
            # con la vista mensual.
            days_per_month = working.groupby("month")[date_col].apply(lambda s: s.dt.day.nunique())
            has_daily_detail = bool((days_per_month > 1).any())

    return {
        "sales_column": str(sales_col),
        "date_column": str(date_col) if date_col is not None else None,
        "category_column": str(category_col) if category_col is not None else None,
        "total_sales": round(total_sales, 2),
        "avg_ticket": round(avg_ticket, 2),
        "row_count": row_count,
        "top_category": top_category,
        "monthly": monthly,
        "trend_pct": trend_pct,
        "has_daily_detail": has_daily_detail,
    }


def compute_period_breakdown(df: pd.DataFrame, month: str | None = None, day: str | None = None) -> dict | None:
    """Desglose para un periodo específico de la pestaña Ventas:

    - Si se pasa `month` (formato 'YYYY-MM') y el dataset tiene detalle
      diario, devuelve las ventas día a día de ESE mes ('daily_points').
    - Devuelve siempre el desglose por categoría ('categories') filtrado
      al periodo pedido: todo el dataset si no se pasa `month`, solo ese
      mes si se pasa `month`, o solo ese día si además se pasa `day`
      (formato 'YYYY-MM-DD').

    Devuelve None si no se pudo identificar una columna de monto.
    """
    sales_col = _find_sales_column(df)
    if sales_col is None:
        return None

    date_col = _find_date_column(df)
    category_col = _find_category_column(df)

    working = df.copy()
    working[sales_col] = pd.to_numeric(working[sales_col], errors="coerce")

    if date_col is not None:
        working[date_col] = parse_mixed_dates(working[date_col])

    scoped = working.dropna(subset=[sales_col])

    if date_col is not None and month:
        scoped = scoped.dropna(subset=[date_col])
        scoped = scoped[scoped[date_col].dt.to_period("M").astype(str) == month]
        if day:
            scoped = scoped[scoped[date_col].dt.date.astype(str) == day]

    daily_points = None
    if date_col is not None and month and not day:
        month_rows = scoped.dropna(subset=[date_col])
        if not month_rows.empty:
            daily_totals = month_rows.groupby(month_rows[date_col].dt.date)[sales_col].sum().sort_index()
            daily_points = [{"day": str(d), "total": float(v)} for d, v in daily_totals.items()]

    categories = None
    if category_col is not None:
        grouped = scoped.groupby(category_col)[sales_col].sum(numeric_only=True).sort_values(ascending=False)
        grouped = grouped.dropna()
        categories = [{"name": str(name), "total": float(v)} for name, v in grouped.items()]

    return {
        "daily_points": daily_points,
        "categories": categories,
    }


def compare_datasets_in_memory(own_df: pd.DataFrame, other_df: pd.DataFrame) -> dict:
    """Compara la estructura de dos tablas del MISMO rubro (ej. farmacia vs
    farmacia) y genera una recomendación simple:

    1. Detecta qué columnas tiene 'other_df' que 'own_df' NO tiene.
    2. Mide si esas columnas se asocian a mayores ventas, de DOS formas
       (usa la que se pueda):
       a) Cruzando filas por una columna clave en común (ej. cliente_id) y
          comparando TUS ventas históricas (own_df) entre las filas que sí
          tienen la columna extra vs las que no. Esto sirve para archivos
          "livianos" que no traen su propia columna de ventas — como una
          lista de ofertas del día — que es el caso más común.
       b) Si no hay clave en común, cae al método anterior: busca una
          columna de ventas DENTRO del archivo externo y compara ahí mismo
          (útil cuando comparas dos datasets completos de ventas, ej. tu
          farmacia vs la farmacia competencia).
    3. Devuelve columnas extra + recomendación en texto, listo para mostrar
       en el frontend del analista.

    Ninguno de los DataFrames se persiste: son parámetros en memoria y se
    descartan al retornar.
    """
    own_columns = {str(c).strip().lower() for c in own_df.columns}
    other_columns = [str(c) for c in other_df.columns]

    extra_columns = [c for c in other_columns if c.strip().lower() not in own_columns]

    join_key_candidates = _detect_join_key_candidates(own_df, other_df)
    own_sales_col = _find_sales_column(own_df)
    other_sales_col = _find_sales_column(other_df)

    # Si hay una clave en común Y tu propio dataset tiene una columna de
    # ventas, armamos UNA vez la tabla cruzada y la reusamos para medir el
    # impacto de cada columna extra contra TUS ventas reales.
    merged_for_impact = None
    if join_key_candidates and own_sales_col is not None:
        best_key = join_key_candidates[0]["column"]
        own_key_col = next((c for c in own_df.columns if str(c).strip().lower() == best_key.strip().lower()), None)
        other_key_col = next((c for c in other_df.columns if str(c).strip().lower() == best_key.strip().lower()), None)
        if own_key_col is not None and other_key_col is not None:
            left = own_df[[own_key_col, own_sales_col]].copy()
            left["_join_key"] = left[own_key_col].astype(str).str.strip()
            right = other_df.copy()
            right["_join_key"] = right[other_key_col].astype(str).str.strip()
            merged_for_impact = left.merge(right, on="_join_key", how="left", suffixes=("", "_otro"))

    def _has_value(series: pd.Series) -> pd.Series:
        has_value = series.notna()
        # columnas booleanas/texto tipo "si/no" también cuentan como activas
        if series.dtype == object:
            has_value = series.astype(str).str.strip().str.lower().isin(
                ["si", "sí", "yes", "true", "1", "x"]
            ) | (series.notna() & (series.astype(str).str.strip() != ""))
        return has_value

    # Columnas con una clave de cruce ya sugerida (match_pct >= umbral),
    # usado para saber si una recomendación es fácil de aplicar o no.
    join_key_columns_available = {c["column"].strip().lower() for c in join_key_candidates}

    def _priority_from_impact(impact_pct: float | None) -> str:
        """Clasifica el impacto en una prioridad legible para el analista."""
        if impact_pct is None or impact_pct <= 0:
            return "baja"
        if impact_pct > 15:
            return "alta"
        if impact_pct >= 5:
            return "media"
        return "baja"

    def _explain_cause(col: str, impact_pct: float) -> str:
        """Arma una frase de 'por qué' según el nombre/tipo de columna,
        en vez de mostrar solo el porcentaje en seco."""
        col_lower = col.strip().lower()
        if any(k in col_lower for k in ["oferta", "promo", "descuento", "cupon"]):
            causa = "probablemente porque incentiva a los clientes a comprar más o a decidirse más rápido"
        elif any(k in col_lower for k in ["fideliz", "membres", "club", "puntos"]):
            causa = "probablemente porque estos clientes tienen una relación más constante con el negocio"
        elif any(k in col_lower for k in ["categoria", "tipo", "linea", "familia"]):
            causa = "probablemente porque agrupa productos que por su naturaleza generan más ingreso por venta"
        elif any(k in col_lower for k in ["region", "zona", "sucursal", "ciudad", "distrito"]):
            causa = "probablemente porque refleja diferencias de demanda entre ubicaciones"
        elif any(k in col_lower for k in ["canal", "medio", "plataforma"]):
            causa = "probablemente porque ese canal atrae compras de mayor valor"
        else:
            causa = "aunque el motivo exacto conviene validarlo con más contexto del negocio"

        if impact_pct > 0:
            return (
                f"Las ventas suben {impact_pct}% cuando el registro tiene '{col}', "
                f"lo que sugiere que esta variable está asociada a mayor gasto — {causa}."
            )
        return (
            f"'{col}' no muestra una asociación clara con mayores ventas ({impact_pct}%), "
            "por lo que no parece ser un factor relevante para explicar diferencias de ingreso."
        )

    recommendations = []
    for col in extra_columns:
        entry = {"column": col, "impact_pct": None, "priority": "baja", "message": None, "method": None}

        # Método A: cruzando por clave, contra TUS ventas históricas.
        if merged_for_impact is not None and col in merged_for_impact.columns:
            has_value = _has_value(merged_for_impact[col])
            with_col = merged_for_impact.loc[has_value, own_sales_col].dropna()
            without_col = merged_for_impact.loc[~has_value, own_sales_col].dropna()

            if len(with_col) > 0 and len(without_col) > 0:
                avg_with = float(with_col.mean())
                avg_without = float(without_col.mean())
                if avg_without > 0:
                    impact_pct = round(((avg_with - avg_without) / avg_without) * 100, 1)
                    entry["impact_pct"] = impact_pct
                    entry["method"] = "cruce_historico"
                    entry["message"] = _explain_cause(col, impact_pct)

        # Método B (respaldo): columna de ventas dentro del archivo externo.
        if entry["message"] is None and other_sales_col is not None:
            has_value = _has_value(other_df[col])
            with_col = other_df.loc[has_value, other_sales_col].dropna()
            without_col = other_df.loc[~has_value, other_sales_col].dropna()

            if len(with_col) > 0 and len(without_col) > 0:
                avg_with = float(with_col.mean())
                avg_without = float(without_col.mean())
                if avg_without > 0:
                    impact_pct = round(((avg_with - avg_without) / avg_without) * 100, 1)
                    entry["impact_pct"] = impact_pct
                    entry["method"] = "archivo_externo"
                    entry["message"] = _explain_cause(col, impact_pct)

        if entry["message"] is None:
            entry["message"] = (
                f"La otra tabla tiene la columna '{col}' que tú no tienes, "
                "pero no se encontró una columna de ventas para medir su impacto."
            )

        entry["priority"] = _priority_from_impact(entry["impact_pct"])
        # Una recomendación es "fácil de aplicar" si ya existe (al menos)
        # una clave de cruce sugerida entre ambos datasets para traerla a
        # tu tabla con el botón "Agregar a mi tabla".
        entry["easy_to_apply"] = bool(join_key_columns_available)
        recommendations.append(entry)

    # ordena mostrando primero las de mayor impacto positivo
    recommendations.sort(key=lambda r: (r["impact_pct"] is None, -(r["impact_pct"] or 0)))

    # --- Resumen ejecutivo -------------------------------------------------
    positive_recs = [r for r in recommendations if r["impact_pct"] is not None and r["impact_pct"] > 0]
    top_recs = positive_recs[:3]

    if not extra_columns:
        headline = "La otra empresa no tiene columnas adicionales: no hay nuevas variables que evaluar por ahora."
    elif not positive_recs:
        headline = (
            f"Se detectaron {len(extra_columns)} columna(s) nueva(s), pero ninguna muestra una asociación "
            "clara con mayores ventas. Por ahora no se recomienda priorizar su incorporación."
        )
    else:
        top = top_recs[0]
        columnas_top = " y ".join(f"'{r['column']}'" for r in top_recs[:2])
        headline = (
            f"De {len(extra_columns)} columna(s) nueva(s) detectadas, {columnas_top} "
            f"muestra{'n' if len(top_recs[:2]) > 1 else ''} el mayor impacto en ventas "
            f"(+{top['impact_pct']}% la más fuerte). Se recomienda priorizar su incorporación "
            "para mejorar la toma de decisiones comerciales."
        )

    priority_recommendation = None
    if top_recs:
        best = top_recs[0]
        accion = (
            f"Prioriza incorporar '{best['column']}' — es la que más impacto tendría en tus ventas "
            f"(+{best['impact_pct']}%)"
        )
        if best.get("easy_to_apply") and join_key_candidates:
            accion += f", y ya se detectó una columna clave ('{join_key_candidates[0]['column']}') para cruzarla fácilmente con tu tabla."
        else:
            accion += "."
        priority_recommendation = {
            "column": best["column"],
            "impact_pct": best["impact_pct"],
            "priority": best["priority"],
            "action": accion,
        }

    executive_summary = {
        "headline": headline,
        "top_recommendations": [
            {"column": r["column"], "impact_pct": r["impact_pct"], "priority": r["priority"]}
            for r in top_recs
        ],
        "priority_recommendation": priority_recommendation,
    }

    return {
        "own_columns": sorted(own_columns),
        "other_columns": other_columns,
        "extra_columns": extra_columns,
        "sales_column_detected": own_sales_col or other_sales_col,
        "recommendations": recommendations,
        "executive_summary": executive_summary,
        # Columnas candidatas para cruzar filas entre ambos datasets (ej.
        # cliente_id, producto), usadas por la función de "traer columna" /
        # tabla temporal.
        "join_key_candidates": join_key_candidates,
    }