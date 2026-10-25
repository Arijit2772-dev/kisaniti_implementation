"""Stored storage assessments with persisted hysteresis state (devices/v1 amendment A1, CHAT-42).

Revision ID: 0005_storage_assessments
Revises: 0004_devices

Written only by the assessment job (the owner connection); a tenant can read the rows of its own devices and nothing
else. Append-only: a stored assessment is never edited.
"""

from alembic import op

revision = "0005_storage_assessments"
down_revision = "0004_devices"
branch_labels = None
depends_on = None

TENANT = "fpo_id = nullif(current_setting('app.fpo_id', true), '')"


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE storage_assessments (
            id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            device_id              text NOT NULL,
            fpo_id                 text NOT NULL,
            assessed_at            timestamptz NOT NULL,
            assigned_at            timestamptz,
            category               text NOT NULL CHECK (category IN ('LOW', 'MEDIUM', 'HIGH', 'UNKNOWN')),
            hysteresis_level       text CHECK (hysteresis_level IN ('LOW', 'MEDIUM', 'HIGH')),
            hysteresis_below_since timestamptz,
            hysteresis_assigned_at timestamptz,
            doc                    jsonb NOT NULL,
            created_at             timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (device_id, fpo_id) REFERENCES devices (device_id, fpo_id),
            UNIQUE (device_id, assessed_at)
        )
        """
    )
    op.execute("CREATE INDEX storage_assessments_device_time ON storage_assessments (device_id, assessed_at DESC)")
    op.execute(
        r"""
        CREATE FUNCTION storage_assessments_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'storage_assessments is append-only';
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER storage_assessments_no_change BEFORE UPDATE OR DELETE ON storage_assessments "
        "FOR EACH ROW EXECUTE FUNCTION storage_assessments_append_only()"
    )
    op.execute("ALTER TABLE storage_assessments ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON storage_assessments USING ({TENANT}) WITH CHECK ({TENANT})")
    op.execute("GRANT SELECT ON storage_assessments TO kisaniti_app")


def downgrade() -> None:
    op.execute("DROP TABLE storage_assessments")
    op.execute("DROP FUNCTION storage_assessments_append_only()")
