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
    }

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

    elif null_strategy == "zero":
        numeric_cols = working.select_dtypes(include="number").columns
        log["nulls_filled_count"] = int(working[numeric_cols].isnull().sum().sum())
        working[numeric_cols] = working[numeric_cols].fillna(0)

    elif null_strategy == "average":
        numeric_cols = working.select_dtypes(include="number").columns
        log["nulls_filled_count"] = int(working[numeric_cols].isnull().sum().sum())
        working[numeric_cols] = working[numeric_cols].fillna(working[numeric_cols].mean())

    if options.get("convert_number"):
        for col in working.select_dtypes(include="object").columns:
            converted = pd.to_numeric(working[col], errors="coerce")
            if converted.notna().sum() > 0:
                working[col] = converted.where(converted.notna(), working[col])

    if options.get("convert_dates"):
        for col in working.select_dtypes(include="object").columns:
            converted = pd.to_datetime(working[col], errors="coerce")
            if converted.notna().sum() > 0:
                working[col] = converted.dt.strftime("%Y-%m-%d").where(converted.notna(), working[col])

    if options.get("remove_empty_columns"):
        empty_cols = [c for c in working.columns if working[c].isnull().all()]
        if empty_cols:
            log["columns_removed"] = empty_cols
            working = working.drop(columns=empty_cols)

    return working, log