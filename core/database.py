"""Database configuration and session management."""
import os
from sqlalchemy import create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://xyn:xyn_dev_password@localhost:5432/xyn")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency to get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize database tables.

    In production, use migrations instead of create_all().
    Set XYN_AUTO_CREATE_SCHEMA=true to enable automatic schema creation (dev only).
    Set XYN_REQUIRED_MIGRATIONS to enforce specific migrations (comma-separated).
    """
    auto_create = os.getenv("XYN_AUTO_CREATE_SCHEMA", "false").lower() in ("true", "1", "yes")
    required = os.getenv("XYN_REQUIRED_MIGRATIONS", "001_initial_schema").split(",")

    if auto_create:
        from core import models  # noqa
        Base.metadata.create_all(bind=engine)
        _apply_dev_schema_upgrades()
        return

    # Production mode: tables must exist via migrations
    from sqlalchemy import inspect, text
    inspector = inspect(engine)

    if "schema_migrations" not in inspector.get_table_names():
        raise RuntimeError(
            "Database schema not initialized. "
            "Run migrations with scripts/apply_migrations.sh or set XYN_AUTO_CREATE_SCHEMA=true for dev."
        )

    # Require baseline (and optionally more)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM schema_migrations")).fetchall()
        applied = {r[0] for r in rows}

    missing = [m.strip() for m in required if m.strip() and m.strip() not in applied]
    if missing:
        raise RuntimeError(
            "Database migrations missing: "
            + ", ".join(missing)
            + ". Run scripts/apply_migrations.sh."
        )


def _apply_dev_schema_upgrades() -> None:
    """Apply idempotent schema upgrades for local auto-create mode.

    SQLAlchemy create_all() creates missing tables but does not alter existing
    tables. These guarded statements keep local dev databases aligned with the
    current models when old schemas already exist.
    """
    statements = [
        """
        CREATE TABLE IF NOT EXISTS workspaces (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          slug VARCHAR(255) NOT NULL UNIQUE,
          title VARCHAR(255) NOT NULL DEFAULT 'Default Workspace',
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_workspaces_slug ON workspaces(slug)",
        """
        INSERT INTO workspaces (id, slug, title, created_at, updated_at)
        VALUES (
          '00000000-0000-0000-0000-000000000001'::uuid,
          'default',
          'Default Workspace',
          NOW(),
          NOW()
        )
        ON CONFLICT (slug) DO NOTHING
        """,
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS workspace_id UUID",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS type VARCHAR(100)",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS title VARCHAR(255)",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS content_json JSON",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS created_by VARCHAR(255)",
        "UPDATE drafts SET workspace_id = COALESCE(workspace_id, (SELECT id FROM workspaces WHERE slug = 'default' LIMIT 1))",
        """
        DO $$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='kind'
          ) THEN
            UPDATE drafts SET type = COALESCE(type, kind);
          END IF;
        END $$;
        """,
        "UPDATE drafts SET type = COALESCE(type, 'app_intent')",
        """
        DO $$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='name'
          ) THEN
            UPDATE drafts SET title = COALESCE(title, name);
          END IF;
        END $$;
        """,
        "UPDATE drafts SET title = COALESCE(title, 'Untitled Draft')",
        """
        DO $$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='definition'
          ) THEN
            UPDATE drafts SET content_json = COALESCE(content_json, definition);
          END IF;
        END $$;
        """,
        "UPDATE drafts SET content_json = COALESCE(content_json, '{}'::json)",
        "UPDATE drafts SET created_by = COALESCE(created_by, 'system')",
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
              SELECT 1
              FROM pg_constraint
              WHERE conname = 'fk_drafts_workspace_id'
          ) THEN
            ALTER TABLE drafts
              ADD CONSTRAINT fk_drafts_workspace_id
              FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT;
          END IF;
        END $$;
        """,
        "ALTER TABLE drafts ALTER COLUMN workspace_id SET NOT NULL",
        "ALTER TABLE drafts ALTER COLUMN type SET NOT NULL",
        "ALTER TABLE drafts ALTER COLUMN title SET NOT NULL",
        "ALTER TABLE drafts ALTER COLUMN content_json SET NOT NULL",
        "ALTER TABLE drafts ALTER COLUMN created_by SET NOT NULL",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS status_v2 VARCHAR(32)",
        """
        DO $$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='status'
          ) THEN
            UPDATE drafts
            SET status_v2 = COALESCE(
              status_v2,
              CASE UPPER(COALESCE(status::text, 'DRAFT'))
                WHEN 'DRAFT' THEN 'draft'
                WHEN 'VALIDATED' THEN 'ready'
                WHEN 'PROMOTED' THEN 'submitted'
                ELSE 'draft'
              END
            );
          END IF;
        END $$;
        """,
        "UPDATE drafts SET status_v2 = COALESCE(status_v2, 'draft')",
        "ALTER TABLE drafts ALTER COLUMN status_v2 SET NOT NULL",
        """
        DO $$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='status'
          ) THEN
            ALTER TABLE drafts DROP COLUMN status;
          END IF;
        END $$;
        """,
        """
        DO $$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='status_v2'
          ) THEN
            ALTER TABLE drafts RENAME COLUMN status_v2 TO status;
          END IF;
        END $$;
        """,
        """
        DO $$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='kind'
          ) THEN
            ALTER TABLE drafts DROP COLUMN kind;
          END IF;
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='name'
          ) THEN
            ALTER TABLE drafts DROP COLUMN name;
          END IF;
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='definition'
          ) THEN
            ALTER TABLE drafts DROP COLUMN definition;
          END IF;
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='trigger_event_type'
          ) THEN
            ALTER TABLE drafts DROP COLUMN trigger_event_type;
          END IF;
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='notes'
          ) THEN
            ALTER TABLE drafts DROP COLUMN notes;
          END IF;
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='source_run_id'
          ) THEN
            ALTER TABLE drafts DROP COLUMN source_run_id;
          END IF;
          IF EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name='drafts' AND column_name='revision'
          ) THEN
            ALTER TABLE drafts DROP COLUMN revision;
          END IF;
        END $$;
        """,
        "CREATE INDEX IF NOT EXISTS ix_drafts_workspace_id ON drafts(workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_drafts_status ON drafts(status)",
        "CREATE INDEX IF NOT EXISTS ix_drafts_type ON drafts(type)",
        """
        CREATE TABLE IF NOT EXISTS jobs (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
          type VARCHAR(100) NOT NULL,
          status VARCHAR(32) NOT NULL DEFAULT 'queued',
          input_json JSON NOT NULL DEFAULT '{}'::json,
          output_json JSON,
          logs_text TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_jobs_workspace_id ON jobs(workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status)",
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
