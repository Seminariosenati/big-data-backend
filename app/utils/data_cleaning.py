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
