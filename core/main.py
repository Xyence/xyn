"""Compatibility entrypoint.

Phase 1 routes startup through kernel_app so legacy product imports are optional.
"""

from core.kernel_app import app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("core.kernel_app:app", host="0.0.0.0", port=8000, reload=True)
