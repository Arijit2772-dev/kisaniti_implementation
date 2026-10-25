"""Storage-node readings (contract L3B v1 §3): readings, conflicts and rejected raw messages.

Revision ID: 0002_sensor_readings
Revises: 0001_fpo_tenant

Owned here, not by the ingestion service: the service writes to these tables but does not create them.
No `fpo_id` column on purpose: the contract resolves the FPO from the device-assignment history at `measured_at`
(IC-DOMAIN-004), never from the device. Tenant-scoped access therefore needs the device registry and assignments
(H-4); until then these tables are written by the ingestion service only and are not exposed over HTTP.
"""

from alembic import op

revision = "0002_sensor_readings"
down_revision = "0001_fpo_tenant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE sensor_readings (
            reading_id        text PRIMARY KEY,
            device_id         text NOT NULL CHECK (device_id ~ '^[a-z0-9][a-z0-9-]{2,31}$'),
            boot_id           text NOT NULL CHECK (boot_id ~ '^[0-9a-f]{8}$'),
            seq               bigint NOT NULL CHECK (seq >= 0),
            measured_at       timestamptz,
            received_at       timestamptz NOT NULL,
            ingest_status     text NOT NULL CHECK (ingest_status IN ('ACCEPTED', 'QUARANTINED')),
            quarantine_reason text,
            content_sha256    text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
            doc               jsonb NOT NULL,
            CHECK (reading_id = device_id || ':' || boot_id || ':' || seq),
            CHECK ((ingest_status = 'QUARANTINED') = (quarantine_reason IS NOT NULL))
        )
        """
    )
    op.execute("CREATE INDEX sensor_readings_device_time ON sensor_readings (device_id, measured_at)")
    op.execute(
        r"""
        CREATE TABLE sensor_reading_conflicts (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            reading_id  text NOT NULL REFERENCES sensor_readings (reading_id),
            received_at timestamptz NOT NULL,
            doc         jsonb NOT NULL
        )
        """
    )
    op.execute(
        r"""
        CREATE TABLE sensor_rejected_messages (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            received_at timestamptz NOT NULL,
            topic       text NOT NULL,
            reason      text NOT NULL,
            detail      text NOT NULL,
            device_id   text,
            boot_id     text,
            seq         bigint,
            raw_sha256  text NOT NULL CHECK (raw_sha256 ~ '^[0-9a-f]{64}$'),
            raw         bytea NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE sensor_rejected_messages")
    op.execute("DROP TABLE sensor_reading_conflicts")
    op.execute("DROP TABLE sensor_readings")
