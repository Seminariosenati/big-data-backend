from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from app.config.settings import get_supabase_admin, get_settings
from app.utils.auth_dependency import require_auth
from app.utils.data_cleaning import read_dataset, analyze_dataset

router = APIRouter(prefix="/datasets", tags=["datasets"])

ALLOWED_EXTENSIONS = (".csv", ".xlsx", ".xls")


@router.post("/upload")
async def upload_dataset(file: UploadFile = File(...), auth=Depends(require_auth)):
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
    user = auth["user"]
    supabase = get_supabase_admin()

    result = (
        supabase.table("datasets")
        .select("*")
        .eq("user_id", user.id)
        .order("created_at", desc=True)
        .execute()
    )

    return {"datasets": result.data}


@router.get("/stats")
def get_stats(auth=Depends(require_auth)):
    """Estadísticas agregadas para alimentar los StatCard / gráficos del dashboard."""
    user = auth["user"]
    supabase = get_supabase_admin()

    result = supabase.table("datasets").select("*").eq("user_id", user.id).execute()
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
