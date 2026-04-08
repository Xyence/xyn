# Artifact Code Review MVP

This MVP adds source-inspection and static-analysis APIs plus MCP tools so ChatGPT can review/refactor artifact codebases (including very large files).

## New API Endpoints

- `GET /api/v1/artifacts/source-tree`
  - Query: `artifact_id` or `artifact_slug`, `include_line_counts`
- `GET /api/v1/artifacts/source-file`
  - Query: `artifact_id` or `artifact_slug`, `path`, optional `start_line`, `end_line`
- `GET /api/v1/artifacts/source-search`
  - Query: `artifact_id` or `artifact_slug`, `query`, optional `path_glob`, `file_extensions`, `regex`, `case_sensitive`, `limit`
- `GET /api/v1/artifacts/analyze-codebase`
  - Query: `artifact_id` or `artifact_slug`, optional `mode` (`general` or `python_api`)
- `GET /api/v1/artifacts/analyze-python-api`
  - Query: `artifact_id` or `artifact_slug` (alias for `analyze-codebase?mode=python_api`)
- `GET /api/v1/artifacts/module-metrics`
  - Query: `artifact_id` or `artifact_slug`, optional `top_n`

All endpoints require existing artifact read capability (`app.artifacts.read`).

## New MCP Tools

- `get_artifact_source_tree`
- `read_artifact_source_file`
- `search_artifact_source`
- `analyze_artifact_codebase`
- `analyze_python_api_artifact`
- `get_artifact_module_metrics`

All tools support `artifact_id` or `artifact_slug`.

## Example: `get_artifact_source_tree`

```json
{
  "artifact": {"id": "6b4f...", "slug": "app.net-inventory"},
  "file_count": 3,
  "files": [
    {"path": "xyn_orchestrator/xyn_api.py", "kind": "file", "language": "python", "size_bytes": 3120000, "line_count": 46012, "sha256": "..."},
    {"path": "xyn_orchestrator/models.py", "kind": "file", "language": "python", "size_bytes": 480000, "line_count": 9200, "sha256": "..."}
  ],
  "tree": {"path": "/", "name": "/", "kind": "dir", "children": [{"path": "/xyn_orchestrator", "name": "xyn_orchestrator", "kind": "dir", "children": []}]}
}
```

## Example: `read_artifact_source_file`

```json
{
  "artifact": {"id": "6b4f...", "slug": "app.net-inventory"},
  "path": "xyn_orchestrator/xyn_api.py",
  "language": "python",
  "total_lines": 46012,
  "returned_start_line": 1200,
  "returned_end_line": 1400,
  "sha256": "...",
  "content": "def workspace_artifacts_collection(...):\n    ..."
}
```

## Example: `analyze_artifact_codebase`

```json
{
  "artifact": {"id": "6b4f...", "slug": "app.net-inventory"},
  "analysis_version": "mvp.v1",
  "languages_detected": ["python", "json"],
  "framework_fingerprint": [
    {"framework": "django", "evidence_file_count": 12},
    {"framework": "fastapi", "evidence_file_count": 2},
    {"framework": "sqlalchemy", "evidence_file_count": 5}
  ],
  "largest_files_by_line_count": [
    {"path": "xyn_orchestrator/xyn_api.py", "line_count": 46012, "language": "python"}
  ],
  "architectural_risks": [
    {"severity": "high", "category": "oversized_file", "path": "xyn_orchestrator/xyn_api.py", "detail": "Largest file has 46012 lines; likely monolithic hotspot."}
  ],
  "recommended_refactor_seams": [
    {"priority": "high", "title": "Extract bounded modules from oversized file", "target_path": "xyn_orchestrator/xyn_api.py", "suggested_slices": ["routing", "schema/models", "service layer", "persistence", "cross-cutting utils"]}
  ],
  "confidence_notes": ["Analysis is heuristic and static-only."]
}
```

## Example: `analyze_python_api_artifact` (`mode=python_api`)

```json
{
  "artifact": {"id": "6b4f...", "slug": "app.net-inventory"},
  "analysis_version": "mvp.v1",
  "analysis_mode": "python_api",
  "python_api_assessment": {
    "oversized_file_report": {
      "thresholds": [500, 1000, 3000, 10000],
      "files_over_threshold": {
        "10000": [
          {"path": "xyn_orchestrator/xyn_api.py", "line_count": 46012, "language": "python"}
        ]
      },
      "api_entrypoint_candidates": [
        {"path": "xyn_orchestrator/xyn_api.py", "score": 100, "fan_in": 20, "fan_out": 85, "route_count": 180, "line_count": 46012}
      ]
    },
    "framework_fingerprint": {
      "django": {"detected": true, "evidence": {"urls": [{"path": "xyn_orchestrator/xyn_api.py"}]}},
      "fastapi": {"detected": true, "evidence": {"routers": [{"path": "xyn_orchestrator/xyn_api.py"}]}},
      "mixed_framework_detected": true,
      "files_with_both_frameworks": ["xyn_orchestrator/xyn_api.py"]
    },
    "route_inventory": {
      "count": 180,
      "routes_in_oversized_files": [
        {"framework": "fastapi", "method": "GET", "route": "/health", "file": "xyn_orchestrator/xyn_api.py", "line": 300, "file_line_count": 46012}
      ]
    },
    "monolith_risk_scores": {
      "file_size_risk": {"score": 100, "rating": "critical"},
      "framework_mixing_risk": {"score": 100, "rating": "critical"}
    },
    "suggested_extraction_plan": [
      {"step": 1, "title": "Freeze API entrypoint behavior"},
      {"step": 2, "title": "Extract routers/views by domain"}
    ]
  }
}
```
