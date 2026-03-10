"""Debug API endpoints for development"""
import uuid
from typing import List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from core.capability_manifest import load_workspace_capability_manifest
from core.database import get_db
from core.palette_commands import workspace_palette_capability_diagnostics

router = APIRouter()


@router.get("/debug/blueprints", response_model=List[str])
async def list_blueprints():
    """List all registered blueprints.

    Returns:
        List of blueprint references
    """
    from core.blueprints.registry import list_blueprints
    return list_blueprints()


@router.get("/debug/db/schemas", response_model=List[str])
async def list_schemas(db: Session = Depends(get_db)):
    """List all database schemas.

    Returns:
        List of schema names
    """
    result = db.execute(text("""
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
        ORDER BY schema_name
    """))

    schemas = [row[0] for row in result]
    return schemas


@router.get("/debug/db/schema/{schema_name}/tables", response_model=List[Dict[str, Any]])
async def list_tables_in_schema(schema_name: str, db: Session = Depends(get_db)):
    """List all tables in a specific schema with column information.

    Args:
        schema_name: Name of the schema to inspect

    Returns:
        List of tables with their columns
    """
    # Verify schema exists
    schema_check = db.execute(text("""
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = :schema_name
    """), {"schema_name": schema_name})

    if not schema_check.fetchone():
        raise HTTPException(status_code=404, detail=f"Schema '{schema_name}' not found")

    # Get tables in schema
    tables_result = db.execute(text("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = :schema_name
        AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """), {"schema_name": schema_name})

    tables = []
    for table_row in tables_result:
        table_name = table_row[0]

        # Get columns for this table
        columns_result = db.execute(text("""
            SELECT
                column_name,
                data_type,
                is_nullable,
                column_default
            FROM information_schema.columns
            WHERE table_schema = :schema_name
            AND table_name = :table_name
            ORDER BY ordinal_position
        """), {"schema_name": schema_name, "table_name": table_name})

        columns = []
        for col_row in columns_result:
            columns.append({
                "name": col_row[0],
                "type": col_row[1],
                "nullable": col_row[2] == "YES",
                "default": col_row[3]
            })

        tables.append({
            "table_name": table_name,
            "columns": columns
        })

    return tables


@router.get("/debug/capabilities", response_model=Dict[str, Any])
async def debug_capabilities(workspace_id: uuid.UUID, db: Session = Depends(get_db)):
    return {
        "workspace_id": str(workspace_id),
        "manifest": load_workspace_capability_manifest(db, workspace_id=workspace_id),
        "diagnostics": workspace_palette_capability_diagnostics(db, workspace_id=workspace_id),
    }
