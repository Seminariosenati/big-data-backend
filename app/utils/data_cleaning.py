import io
import json
import numpy as np
import pandas as pd


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

    # Score simple de calidad: penaliza nulos y duplicados
    quality_score = round(max(0.0, 100 - (null_ratio * 60 + duplicate_ratio * 40) * 100), 2)

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

            converted = pd.to_datetime(working[col], errors="coerce", format="mixed", dayfirst=True)
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
    date_col = _find_date_column(df)
    if date_col is not None:
        working = df[[date_col, sales_col]].copy()
        working[date_col] = pd.to_datetime(working[date_col], errors="coerce", format="mixed", dayfirst=True)
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
    }


def compare_datasets_in_memory(own_df: pd.DataFrame, other_df: pd.DataFrame) -> dict:
    """Compara la estructura de dos tablas del MISMO rubro (ej. farmacia vs
    farmacia) y genera una recomendación simple:

    1. Detecta qué columnas tiene 'other_df' que 'own_df' NO tiene.
    2. Si en 'other_df' existe una columna de ventas, calcula si las filas
       donde la columna extra está "activa" (no nula / valor truthy) tienen
       en promedio más ventas que las filas donde no está.
    3. Devuelve columnas extra + recomendación en texto, listo para mostrar
       en el frontend del analista.

    Ninguno de los DataFrames se persiste: son parámetros en memoria y se
    descartan al retornar.
    """
    own_columns = {str(c).strip().lower() for c in own_df.columns}
    other_columns = [str(c) for c in other_df.columns]

    extra_columns = [c for c in other_columns if c.strip().lower() not in own_columns]

    sales_col = _find_sales_column(other_df)

    recommendations = []
    for col in extra_columns:
        entry = {"column": col, "impact_pct": None, "message": None}

        if sales_col is not None:
            series = other_df[col]
            has_value = series.notna()
            # columnas booleanas/texto tipo "si/no" también cuentan como activas
            if series.dtype == object:
                has_value = series.astype(str).str.strip().str.lower().isin(
                    ["si", "sí", "yes", "true", "1", "x"]
                ) | (series.notna() & (series.astype(str).str.strip() != ""))

            with_col = other_df.loc[has_value, sales_col].dropna()
            without_col = other_df.loc[~has_value, sales_col].dropna()

            if len(with_col) > 0 and len(without_col) > 0:
                avg_with = float(with_col.mean())
                avg_without = float(without_col.mean())
                if avg_without > 0:
                    impact_pct = round(((avg_with - avg_without) / avg_without) * 100, 1)
                    entry["impact_pct"] = impact_pct
                    if impact_pct > 0:
                        entry["message"] = (
                            f"Las filas con '{col}' muestran un promedio de ventas "
                            f"{impact_pct}% mayor. Te recomendamos incorporar esta columna."
                        )
                    else:
                        entry["message"] = (
                            f"'{col}' no muestra una mejora clara en ventas "
                            f"({impact_pct}%). No es prioritario incorporarla."
                        )

        if entry["message"] is None:
            entry["message"] = (
                f"La otra tabla tiene la columna '{col}' que tú no tienes, "
                "pero no se encontró una columna de ventas para medir su impacto."
            )

        recommendations.append(entry)

    # ordena mostrando primero las de mayor impacto positivo
    recommendations.sort(key=lambda r: (r["impact_pct"] is None, -(r["impact_pct"] or 0)))

    return {
        "own_columns": sorted(own_columns),
        "other_columns": other_columns,
        "extra_columns": extra_columns,
        "sales_column_detected": sales_col,
        "recommendations": recommendations,
    }
    