"""Device registry slice devices/v1: devices, append-only assignments, tenant-scoped access to readings.

Revision ID: 0004_devices
Revises: 0003_access

Contract: kisaniti/docs/contracts/L3B_DEVICE_REGISTRY_CONTRACT_v1.md.
Readings carry no `fpo_id` (the device decides the FPO, never the payload). The restricted role `kisaniti_app` may
read a reading only if the reading's device belongs to the FPO set in `app.fpo_id`: the policy looks the device up
through the same row-level security as every other tenant table.
"""

from alembic import op

revision = "0004_devices"
down_revision = "0003_access"
branch_labels = None
depends_on = None

TENANT = "fpo_id = nullif(current_setting('app.fpo_id', true), '')"


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE devices (
            device_id     text PRIMARY KEY CHECK (device_id ~ '^[a-z0-9][a-z0-9-]{2,31}$'),
            fpo_id        text NOT NULL REFERENCES fpo (id),
            source_origin text NOT NULL CHECK (source_origin IN ('DEVICE', 'WOKWI_TWIN', 'SIMULATOR')),
            active        boolean NOT NULL DEFAULT true,
            registered_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (device_id, fpo_id)
        )
        """
    )
    op.execute("CREATE INDEX devices_fpo ON devices (fpo_id)")
    op.execute("ALTER TABLE devices ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON devices USING ({TENANT}) WITH CHECK ({TENANT})")
    op.execute("GRANT SELECT, INSERT ON devices TO kisaniti_app")
    op.execute("GRANT UPDATE (active) ON devices TO kisaniti_app")

    op.execute(
        r"""
        CREATE TABLE device_assignments (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            device_id        text NOT NULL,
            fpo_id           text NOT NULL,
            placement        text NOT NULL CHECK (placement IN ('IN_GRAIN', 'AMBIENT')),
            crop             text NOT NULL CHECK (crop IN ('PADDY', 'WHEAT')),
            assigned_at      timestamptz NOT NULL,
            sample_interval_s integer NOT NULL CHECK (sample_interval_s BETWEEN 1 AND 86400),
            created_at       timestamptz NOT NULL DEFAULT now(),
            created_by       text REFERENCES users (id),
            FOREIGN KEY (device_id, fpo_id) REFERENCES devices (device_id, fpo_id),
            UNIQUE (device_id, assigned_at)
        )
        """
    )
    op.execute("CREATE INDEX device_assignments_device_time ON device_assignments (device_id, assigned_at DESC)")
    op.execute(
        r"""
        CREATE FUNCTION device_assignments_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'device_assignments is append-only';
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER device_assignments_no_change BEFORE UPDATE OR DELETE ON device_assignments "
        "FOR EACH ROW EXECUTE FUNCTION device_assignments_append_only()"
    )
    op.execute("ALTER TABLE device_assignments ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON device_assignments USING ({TENANT}) WITH CHECK ({TENANT})")
    op.execute("GRANT SELECT, INSERT ON device_assignments TO kisaniti_app")
    op.execute("GRANT USAGE ON SEQUENCE device_assignments_id_seq TO kisaniti_app")

    op.execute("ALTER TABLE sensor_readings ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON sensor_readings FOR SELECT USING "
        "(EXISTS (SELECT 1 FROM devices d WHERE d.device_id = sensor_readings.device_id))"
    )
    op.execute(
        "GRANT SELECT (reading_id, device_id, measured_at, received_at, ingest_status, doc) "
        "ON sensor_readings TO kisaniti_app"
    )


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON sensor_readings")
    op.execute("ALTER TABLE sensor_readings DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON sensor_readings FROM kisaniti_app")
    op.execute("DROP TABLE device_assignments")
    op.execute("DROP FUNCTION device_assignments_append_only()")
    op.execute("DROP TABLE devices")
