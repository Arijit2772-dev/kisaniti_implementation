"""messaging/v1 amendment A1.1: provider delivery events from the WhatsApp status webhook.

Revision ID: 0013_delivery_events
Revises: 0012_handover

Append-only and tenant-owned. An attempt row never changes; its effective status is its own status raised by these
events (never lowered). The recipient's number in a provider event is not stored. Contract:
kisaniti/docs/contracts/L5_MESSAGING_v1_AMENDMENT_A1_CLOUD_API.md.
"""

from alembic import op

revision = "0013_delivery_events"
down_revision = "0012_handover"
branch_labels = None
depends_on = None

TENANT = "fpo_id = nullif(current_setting('app.fpo_id', true), '')"


def upgrade() -> None:
    op.execute("ALTER TABLE delivery_attempts ADD CONSTRAINT delivery_attempts_id_fpo UNIQUE (id, fpo_id)")
    op.execute(
        r"""
        CREATE TABLE delivery_events (
            id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            attempt_id   text NOT NULL,
            fpo_id       text NOT NULL,
            status       text NOT NULL CHECK (status IN ('SENT', 'DELIVERED', 'READ', 'FAILED')),
            provider_at  timestamptz NOT NULL,
            error_codes  integer[] NOT NULL DEFAULT '{}',
            body_sha256  text NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
            received_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
            request_id   text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            FOREIGN KEY (attempt_id, fpo_id) REFERENCES delivery_attempts (id, fpo_id),
            UNIQUE (attempt_id, status)
        )
        """
    )
    op.execute(
        "CREATE FUNCTION delivery_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'delivery_events is append-only'; END $$"
    )
    op.execute(
        "CREATE TRIGGER delivery_events_no_change BEFORE UPDATE OR DELETE ON delivery_events "
        "FOR EACH ROW EXECUTE FUNCTION delivery_events_append_only()"
    )
    op.execute("ALTER TABLE delivery_events ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON delivery_events USING ({TENANT}) WITH CHECK ({TENANT})")
    op.execute("GRANT SELECT ON delivery_events TO kisaniti_app")
    op.execute(
        "CREATE UNIQUE INDEX delivery_attempts_provider_id ON delivery_attempts (provider_message_id) "
        "WHERE channel = 'WHATSAPP' AND provider_message_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX delivery_attempts_provider_id")
    op.execute("DROP TABLE delivery_events")
    op.execute("DROP FUNCTION delivery_events_append_only()")
    op.execute("ALTER TABLE delivery_attempts DROP CONSTRAINT delivery_attempts_id_fpo")
