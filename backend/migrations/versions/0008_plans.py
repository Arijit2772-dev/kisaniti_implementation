"""Plan slice plan/v1: lot-to-sensor links and immutable plan proposals.

Revision ID: 0008_plans
Revises: 0007_org

Both tables are tenant-owned (row-level security, composite keys so a row can never name another FPO's lot or device)
and append-only: a link is superseded by a newer link, a proposal is never edited (approval is a separate object in a
later slice). Contract: kisaniti/docs/contracts/L3_PLAN_CONTRACT_v1.md.
"""

from alembic import op

revision = "0008_plans"
down_revision = "0007_org"
branch_labels = None
depends_on = None

TENANT = "fpo_id = nullif(current_setting('app.fpo_id', true), '')"


def _tenant(table: str, grants: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT}) WITH CHECK ({TENANT})")
    op.execute(f"GRANT {grants} ON {table} TO kisaniti_app")


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE FUNCTION {table}_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ "
        f"BEGIN RAISE EXCEPTION '{table} is append-only'; END $$"
    )
    op.execute(
        f"CREATE TRIGGER {table}_no_change BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {table}_append_only()"
    )


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE sensor_links (
            id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            lot_id     text NOT NULL,
            fpo_id     text NOT NULL,
            device_id  text,
            linked_at  timestamptz NOT NULL,
            created_by text REFERENCES users (id),
            created_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (lot_id, fpo_id) REFERENCES lots (id, fpo_id),
            FOREIGN KEY (device_id, fpo_id) REFERENCES devices (device_id, fpo_id),
            UNIQUE (lot_id, linked_at)
        )
        """
    )
    op.execute("CREATE INDEX sensor_links_lot ON sensor_links (lot_id, linked_at DESC)")
    _append_only("sensor_links")
    _tenant("sensor_links", "SELECT, INSERT")
    op.execute("GRANT USAGE ON SEQUENCE sensor_links_id_seq TO kisaniti_app")

    op.execute(
        r"""
        CREATE TABLE plans (
            id                 text PRIMARY KEY CHECK (id ~ '^pln_[0-9A-HJKMNP-TV-Z]{26}$'),
            fpo_id             text NOT NULL REFERENCES fpo (id),
            commodity_id       text NOT NULL REFERENCES commodities (id),
            variety            text NOT NULL CHECK (variety IN ('1121', 'Basmati 1509')),
            status             text NOT NULL CHECK (status IN ('PROPOSED', 'NOT_PROPOSED')),
            origin             text NOT NULL CHECK (origin IN ('REAL', 'POLICY', 'SYNTHETIC')),
            input_sha256       text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
            doc                jsonb NOT NULL,
            request_key        text CHECK (length(request_key) BETWEEN 8 AND 120),
            request_sha256     text CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
            created_by         text NOT NULL REFERENCES users (id),
            created_at         timestamptz NOT NULL DEFAULT now(),
            UNIQUE (id, fpo_id),
            CHECK ((request_key IS NULL) = (request_sha256 IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX plans_fpo_time ON plans (fpo_id, created_at DESC)")
    op.execute("CREATE UNIQUE INDEX plans_idempotency ON plans (fpo_id, request_key) WHERE request_key IS NOT NULL")
    _append_only("plans")
    _tenant("plans", "SELECT, INSERT")


def downgrade() -> None:
    for t in ("plans", "sensor_links"):
        op.execute(f"DROP TABLE {t}")
        op.execute(f"DROP FUNCTION {t}_append_only()")
