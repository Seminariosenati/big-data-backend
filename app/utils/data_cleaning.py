import io
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


def clean_dataset(df: pd.DataFrame) -> pd.DataFrame:
    """
    Limpieza básica: elimina filas totalmente vacías, quita duplicados exactos,
    y limpia espacios en columnas de texto.
    """
    cleaned = df.copy()
    cleaned = cleaned.dropna(how="all")
    cleaned = cleaned.drop_duplicates()

    for col in cleaned.select_dtypes(include=["object"]).columns:
        cleaned[col] = cleaned[col].astype(str).str.strip()

    return cleaned
