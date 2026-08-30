from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
import json

from app.config.settings import get_supabase_admin, get_settings
from app.utils.auth_dependency import require_auth, require_role
import io
import pandas as pd

from app.utils.data_cleaning import (
    read_dataset,
    analyze_dataset,
    clean_dataset_with_options,
    detect_chart_columns,
    aggregate_chart_column,
    compare_datasets_in_memory,
    compute_sales_summary,
)

router = APIRouter(prefix="/datasets", tags=["datasets"])

ALLOWED_EXTENSIONS = (".csv", ".xlsx", ".xls")
PREVIEW_ROW_LIMIT = 50


class CleaningOptions(BaseModel):
    remove_duplicates: bool = False
    null_strategy: str = "ignore"  # ignore | remove_row | set_null | zero | average
    convert_number: bool = False
    convert_dates: bool = False
    remove_empty_columns: bool = False


def _accessible_dataset_ids(supabase, auth: dict) -> list[str]:
    """IDs de datasets que este usuario puede ver:
    - admin: los suyos (los que subió)
    - analyst: los del admin dueño, pero solo los que le habilitaron en
      analyst_dataset_access (tabla que gestiona el admin desde /users)
    """
    if auth["role"] == "admin":
        result = supabase.table("datasets").select("id").eq("user_id", auth["user"].id).execute()
        return [d["id"] for d in (result.data or [])]

    owner_id = auth.get("owner_id")
    if not owner_id:
        return []

    access = (
        supabase.table("analyst_dataset_access")
        .select("dataset_id")
        .eq("analyst_id", auth["user"].id)
        .execute()
    )
    allowed_ids = [row["dataset_id"] for row in (access.data or [])]
    if not allowed_ids:
        return []

    # Verificación de seguridad: solo datasets que sigan siendo del admin.
    result = (
        supabase.table("datasets")
        .select("id")
        .eq("user_id", owner_id)
        .in_("id", allowed_ids)
        .execute()
    )
    return [d["id"] for d in (result.data or [])]


def _get_owned_dataset(supabase, dataset_id: str, auth: dict) -> dict:
    if dataset_id not in _accessible_dataset_ids(supabase, auth):
        raise HTTPException(status_code=404, detail="Dataset no encontrado")
    result = supabase.table("datasets").select("*").eq("id", dataset_id).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Dataset no encontrado")
    return result.data[0]


def _download_dataset_df(supabase, settings, dataset: dict):
    try:
        file_bytes = supabase.storage.from_(settings.datasets_bucket).download(dataset["storage_path"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo leer el archivo: {exc}")

    try:
        return read_dataset(file_bytes, dataset["file_name"])
    except Exception:
        raise HTTPException(status_code=400, detail="No se pudo procesar el archivo")


def _df_to_preview_payload(df, file_name: str):
    return {
        "fileName": file_name,
        "columns": [str(c) for c in df.columns],
        "totalRows": int(df.shape[0]),
        "rows": json.loads(df.head(PREVIEW_ROW_LIMIT).to_json(orient="records", date_format="iso")),
    }


@router.post("/upload")
async def upload_dataset(file: UploadFile = File(...), auth=Depends(require_role("admin"))):
    user = auth["user"]

    if not file.filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos .csv, .xlsx o .xls")

    file_bytes = await file.read()

    try:
        df = read_dataset(file_bytes, file.filename)
    except Exception:
        raise HTTPException(status_code=400, detail="No se pudo leer el archivo. Verifica el formato.")

    if df.empty:
        raise HTTPException(status_code=400, detail="El archivo no contiene filas")

    # --- Limpieza y análisis con pandas / numpy ---
    analysis = analyze_dataset(df)

    settings = get_settings()
    supabase = get_supabase_admin()

    storage_path = f"{user.id}/{file.filename}"

    try:
        supabase.storage.from_(settings.datasets_bucket).upload(
            storage_path,
            file_bytes,
            {"content-type": file.content_type or "application/octet-stream", "upsert": "true"},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo guardar el archivo: {exc}")

    insert_result = (
        supabase.table("datasets")
        .insert(
            {
                "user_id": user.id,
                "file_name": file.filename,
                "storage_path": storage_path,
                "row_count": analysis["row_count"],
                "column_count": analysis["column_count"],
                "null_count": analysis["null_count"],
                "duplicate_count": analysis["duplicate_count"],
                "quality_score": analysis["quality_score"],
                "columns_summary": analysis["columns_summary"],
                "status": analysis["status"],
                "size_bytes": len(file_bytes),
            }
        )
        .execute()
    )

    return {
        "message": "Archivo procesado correctamente",
        "dataset": insert_result.data[0] if insert_result.data else None,
        "analysis": analysis,
    }


@router.get("")
def list_datasets(auth=Depends(require_auth)):
    supabase = get_supabase_admin()

    ids = _accessible_dataset_ids(supabase, auth)
    if not ids:
        return {"datasets": []}

    result = (
        supabase.table("datasets")
        .select("*")
        .in_("id", ids)
        .order("created_at", desc=True)
        .execute()
    )

    return {"datasets": result.data}


@router.get("/stats")
def get_stats(auth=Depends(require_auth)):
    """Estadísticas agregadas para alimentar los StatCard / gráficos del dashboard."""
    supabase = get_supabase_admin()

    ids = _accessible_dataset_ids(supabase, auth)
    datasets = []
    if ids:
        result = supabase.table("datasets").select("*").in_("id", ids).execute()
        datasets = result.data or []

    total_rows = sum(d["row_count"] for d in datasets)
    total_files = len(datasets)
    total_errors = sum(d["null_count"] + d["duplicate_count"] for d in datasets)

    ok = sum(1 for d in datasets if d["status"] == "ok")
    warn = sum(1 for d in datasets if d["status"] == "warn")
    error = sum(1 for d in datasets if d["status"] == "error")

    return {
        "totalRows": total_rows,
        "totalFiles": total_files,
        "totalErrors": total_errors,
        "qualityBreakdown": {"ok": ok, "warn": warn, "error": error},
    }


def _get_cleaned_dfs(supabase, settings, dataset_ids: list[str]) -> list[pd.DataFrame]:
    """Descarga la versión limpia más reciente de cada dataset visible
    (para agregar sus valores en los gráficos del dashboard)."""
    if not dataset_ids:
        return []

    cleaned = (
        supabase.table("cleaned_datasets")
        .select("*")
        .in_("dataset_id", dataset_ids)
        .order("created_at", desc=True)
        .execute()
    )

    latest_by_dataset: dict[str, dict] = {}
    for row in cleaned.data or []:
        latest_by_dataset.setdefault(row["dataset_id"], row)

    dfs: list[pd.DataFrame] = []
    for row in latest_by_dataset.values():
        try:
            file_bytes = supabase.storage.from_(settings.datasets_bucket).download(row["cleaned_file_path"])
            dfs.append(pd.read_csv(io.BytesIO(file_bytes)))
        except Exception:
            continue
    return dfs


@router.get("/charts/columns")
def get_chart_columns(auth=Depends(require_auth)):
    """Columnas disponibles (agregando TODOS los datasets ya limpios que este
    usuario puede ver) para poblar el selector de columna de los gráficos."""
    supabase = get_supabase_admin()
    settings = get_settings()

    dfs = _get_cleaned_dfs(supabase, settings, _accessible_dataset_ids(supabase, auth))
    return {"columns": detect_chart_columns(dfs)}


@router.get("/charts/data")
def get_chart_data(column: str, auth=Depends(require_auth)):
    """Datos agregados (histograma o conteo de categorías) de una columna,
    combinando TODOS los datasets ya limpios que este usuario puede ver."""
    supabase = get_supabase_admin()
    settings = get_settings()

    dfs = _get_cleaned_dfs(supabase, settings, _accessible_dataset_ids(supabase, auth))
    if not dfs:
        raise HTTPException(status_code=404, detail="Todavía no tienes datasets limpios")

    result = aggregate_chart_column(dfs, column)
    if result is None:
        raise HTTPException(status_code=404, detail="Esa columna no existe en tus datasets limpios")
    return result


def _get_latest_cleaned_df_for_dataset(supabase, settings, dataset_id: str) -> pd.DataFrame | None:
    """Descarga la versión limpia más reciente de UN dataset puntual (no de
    todos los del usuario), para que los gráficos puedan mostrar solo el
    archivo que el usuario tiene seleccionado en el dashboard."""
    cleaned = (
        supabase.table("cleaned_datasets")
        .select("*")
        .eq("dataset_id", dataset_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not cleaned.data:
        return None

    row = cleaned.data[0]
    try:
        file_bytes = supabase.storage.from_(settings.datasets_bucket).download(row["cleaned_file_path"])
        return pd.read_csv(io.BytesIO(file_bytes))
    except Exception:
        return None


@router.get("/{dataset_id}/cleaned-preview")
def get_cleaned_dataset_preview(dataset_id: str, auth=Depends(require_auth)):
    """Columnas + filas de la versión YA LIMPIA de este dataset, para el
    Dashboard (que debe mostrar datos después de la limpieza, no el archivo
    tal como se subió). Si el dataset todavía no pasó por limpieza, 404."""
    user = auth["user"]
    supabase = get_supabase_admin()
    settings = get_settings()

    dataset = _get_owned_dataset(supabase, dataset_id, auth)  # valida dueño / 404
    df = _get_latest_cleaned_df_for_dataset(supabase, settings, dataset_id)
    if df is None:
        raise HTTPException(status_code=404, detail="Este dataset todavía no tiene una versión limpia")

    return _df_to_preview_payload(df, dataset["file_name"])


@router.get("/{dataset_id}/charts/columns")
def get_chart_columns_for_dataset(dataset_id: str, auth=Depends(require_auth)):
    """Columnas disponibles de la versión limpia de ESTE dataset (para que el
    selector de columna del gráfico refleje solo el archivo elegido)."""
    user = auth["user"]
    supabase = get_supabase_admin()
    settings = get_settings()

    _get_owned_dataset(supabase, dataset_id, auth)  # valida dueño / 404
    df = _get_latest_cleaned_df_for_dataset(supabase, settings, dataset_id)

    return {"columns": detect_chart_columns([df] if df is not None else [])}


@router.get("/{dataset_id}/charts/data")
def get_chart_data_for_dataset(dataset_id: str, column: str, auth=Depends(require_auth)):
    """Datos agregados (histograma o conteo de categorías) de una columna,
    usando SOLO la versión limpia de este dataset (no todos los del usuario),
    para que el gráfico cambie al cambiar de dataset en el dashboard."""
    user = auth["user"]
    supabase = get_supabase_admin()
    settings = get_settings()

    _get_owned_dataset(supabase, dataset_id, auth)
    df = _get_latest_cleaned_df_for_dataset(supabase, settings, dataset_id)
    if df is None:
        raise HTTPException(status_code=404, detail="Este dataset todavía no tiene una versión limpia")

    result = aggregate_chart_column([df], column)
    if result is None:
        raise HTTPException(status_code=404, detail="Esa columna no existe en la versión limpia de este dataset")
    return result


@router.get("/{dataset_id}/sales-summary")
def get_sales_summary_for_dataset(dataset_id: str, auth=Depends(require_auth)):
    """KPIs reales de ventas (no de calidad de datos) para la pestaña Ventas:
    ventas totales, ticket promedio, categoría top y tendencia mensual.
    Detecta automáticamente la columna de monto/fecha/categoría por nombre."""
    supabase = get_supabase_admin()
    settings = get_settings()

    _get_owned_dataset(supabase, dataset_id, auth)
    df = _get_latest_cleaned_df_for_dataset(supabase, settings, dataset_id)
    if df is None:
        raise HTTPException(status_code=404, detail="Este dataset todavía no tiene una versión limpia")

    summary = compute_sales_summary(df)
    if summary is None:
        raise HTTPException(
            status_code=404,
            detail="No se encontró una columna de monto reconocible (ventas, monto, importe, total, etc.) en este dataset",
        )
    return summary


@router.get("/{dataset_id}/charts/raw/columns")
def get_raw_chart_columns_for_dataset(dataset_id: str, auth=Depends(require_auth)):
    """Columnas disponibles del archivo TAL COMO SE SUBIÓ (sin limpiar), para
    que también se puedan graficar los datos con errores/valores sucios."""
    user = auth["user"]
    supabase = get_supabase_admin()
    settings = get_settings()

    dataset = _get_owned_dataset(supabase, dataset_id, auth)
    df = _download_dataset_df(supabase, settings, dataset)

    return {"columns": detect_chart_columns([df])}


@router.get("/{dataset_id}/charts/raw/data")
def get_raw_chart_data_for_dataset(dataset_id: str, column: str, auth=Depends(require_auth)):
    """Datos agregados de una columna usando el archivo original SIN limpiar
    (incluye nulos, duplicados y valores mal formateados tal cual llegaron)."""
    user = auth["user"]
    supabase = get_supabase_admin()
    settings = get_settings()

    dataset = _get_owned_dataset(supabase, dataset_id, auth)
    df = _download_dataset_df(supabase, settings, dataset)

    result = aggregate_chart_column([df], column)
    if result is None:
        raise HTTPException(status_code=404, detail="Esa columna no existe en este dataset")
    return result


@router.get("/{dataset_id}/cleaning-logs")
def get_cleaning_logs(dataset_id: str, auth=Depends(require_auth)):
    """Historial de todo lo que se quitó/cambió al limpiar este dataset
    (nunca se borra, queda guardado en cleaning_logs)."""
    supabase = get_supabase_admin()

    _get_owned_dataset(supabase, dataset_id, auth)  # valida acceso / 404

    result = (
        supabase.table("cleaning_logs")
        .select("*")
        .eq("dataset_id", dataset_id)
        .order("created_at", desc=True)
        .execute()
    )

    return {"logs": result.data}


@router.get("/{dataset_id}/preview")
def get_dataset_preview(dataset_id: str, auth=Depends(require_auth)):
    """Devuelve columnas + filas (muestra) de un dataset ya subido, para la
    vista previa en 'Limpieza de datos'."""
    user = auth["user"]
    supabase = get_supabase_admin()
    settings = get_settings()

    dataset = _get_owned_dataset(supabase, dataset_id, auth)
    df = _download_dataset_df(supabase, settings, dataset)

    return _df_to_preview_payload(df, dataset["file_name"])


@router.post("/{dataset_id}/clean-preview")
def preview_clean_dataset(dataset_id: str, options: CleaningOptions, auth=Depends(require_role("admin"))):
    """Simula la limpieza con las opciones actuales y devuelve el resultado
    real (no un cálculo aproximado en el frontend), SIN guardar nada ni
    tocar el archivo original. Se llama cada vez que el usuario cambia una
    opción, para que el 'Después' del preview sea exacto."""
    user = auth["user"]
    supabase = get_supabase_admin()
    settings = get_settings()

    dataset = _get_owned_dataset(supabase, dataset_id, auth)
    df = _download_dataset_df(supabase, settings, dataset)

    cleaned_df, log = clean_dataset_with_options(df, options.model_dump())

    payload = _df_to_preview_payload(cleaned_df, dataset["file_name"])
    payload["summary"] = {
        "duplicatesRemoved": len(log["duplicate_rows_removed"]),
        "emptyRowsRemoved": len(log["empty_rows_removed"]),
        "columnsRemoved": log["columns_removed"],
        "nullsFilled": log["nulls_filled_count"],
    }
    return payload


@router.post("/{dataset_id}/clean")
def apply_clean_dataset(dataset_id: str, options: CleaningOptions, auth=Depends(require_role("admin"))):
    """Aplica la limpieza de verdad: guarda el archivo limpio en storage y
    en `cleaned_datasets`, y registra TODO lo que se quitó o cambió en
    `cleaning_logs` (nunca se borra en silencio)."""
    user = auth["user"]
    supabase = get_supabase_admin()
    settings = get_settings()

    dataset = _get_owned_dataset(supabase, dataset_id, auth)
    df = _download_dataset_df(supabase, settings, dataset)

    cleaned_df, log = clean_dataset_with_options(df, options.model_dump())

    log_rows = []
    for row in log["duplicate_rows_removed"]:
        log_rows.append({"dataset_id": dataset_id, "user_id": user.id, "action": "duplicate_removed", "row_data": row})
    for row in log["empty_rows_removed"]:
        log_rows.append({"dataset_id": dataset_id, "user_id": user.id, "action": "empty_row_removed", "row_data": row})
    for col in log["columns_removed"]:
        log_rows.append({"dataset_id": dataset_id, "user_id": user.id, "action": "column_removed", "row_data": {"column": col}})
    if log["nulls_filled_cells"]:
        for cell in log["nulls_filled_cells"]:
            log_rows.append({
                "dataset_id": dataset_id,
                "user_id": user.id,
                "action": "nulls_filled",
                "row_data": {"estrategia": options.null_strategy, **cell},
            })
    elif log["nulls_filled_count"]:
        log_rows.append({
            "dataset_id": dataset_id,
            "user_id": user.id,
            "action": "nulls_filled",
            "row_data": {"strategy": options.null_strategy, "count": log["nulls_filled_count"]},
        })

    if log_rows:
        supabase.table("cleaning_logs").insert(log_rows).execute()

    cleaned_bytes = cleaned_df.to_csv(index=False).encode("utf-8")
    cleaned_path = f"{user.id}/cleaned/{dataset_id}.csv"

    try:
        supabase.storage.from_(settings.datasets_bucket).upload(
            cleaned_path, cleaned_bytes, {"content-type": "text/csv", "upsert": "true"}
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo guardar el archivo limpio: {exc}")

    supabase.table("cleaned_datasets").insert(
        {"dataset_id": dataset_id, "cleaned_file_path": cleaned_path, "status": "Procesado"}
    ).execute()

    # Recalcular calidad/estado con los datos YA limpios y reflejarlo en el
    # dataset original: si no hacemos esto, el Dashboard, la tabla de
    # "Archivos subidos" y el donut de calidad siguen mostrando los valores
    # de antes de limpiar (0%, "Con errores"), aunque el archivo ya esté ok.
    analysis = analyze_dataset(cleaned_df)
    supabase.table("datasets").update(
        {
            "row_count": analysis["row_count"],
            "column_count": analysis["column_count"],
            "null_count": analysis["null_count"],
            "duplicate_count": analysis["duplicate_count"],
            "quality_score": analysis["quality_score"],
            "status": analysis["status"],
        }
    ).eq("id", dataset_id).execute()

    payload = _df_to_preview_payload(cleaned_df, dataset["file_name"])
    payload["summary"] = {
        "duplicatesRemoved": len(log["duplicate_rows_removed"]),
        "emptyRowsRemoved": len(log["empty_rows_removed"]),
        "columnsRemoved": log["columns_removed"],
        "nullsFilled": log["nulls_filled_count"],
    }
    return payload

@router.post("/{dataset_id}/compare")
async def compare_with_external_dataset(
    dataset_id: str,
    file: UploadFile = File(...),
    auth=Depends(require_role("admin", "analyst")),
):
    """Compara TU dataset (ya limpio) contra un CSV de otra empresa del mismo
    rubro, subido aquí mismo por el analista o el admin.

    CLAVE: el CSV externo NUNCA se guarda. No se sube a Supabase Storage, no
    se inserta en la tabla `datasets`, no se persiste en ningún lado. Se lee
    a un DataFrame de pandas en memoria, se compara, se devuelve el
    resultado, y ahí muere. Esto permite que el rol 'analyst' pueda comparar
    datos sin tener permiso para escribir/borrar nada en el sistema.
    """
    user = auth["user"]
    supabase = get_supabase_admin()
    settings = get_settings()

    if not file.filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos .csv, .xlsx o .xls")

    # dataset propio: se descarga la versión ya limpia (no la cruda)
    dataset = _get_owned_dataset(supabase, dataset_id, auth)
    own_df = _get_latest_cleaned_df_for_dataset(supabase, settings, dataset_id)
    if own_df is None:
        raise HTTPException(
            status_code=400,
            detail="Este dataset todavía no tiene una versión limpia. Límpialo primero.",
        )

    # CSV externo: solo en memoria, jamás tocado con Storage/BD
    file_bytes = await file.read()
    try:
        other_df = read_dataset(file_bytes, file.filename)
    except Exception:
        raise HTTPException(status_code=400, detail="No se pudo leer el archivo de comparación.")

    if other_df.empty:
        raise HTTPException(status_code=400, detail="El archivo de comparación no contiene filas")

    result = compare_datasets_in_memory(own_df, other_df)
    result["ownFileName"] = dataset["file_name"]
    result["comparedFileName"] = file.filename
    return result