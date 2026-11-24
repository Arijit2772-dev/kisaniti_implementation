"""messaging/v1 amendment A1.2: why a WhatsApp attempt failed.

Revision ID: 0014_whatsapp_send
Revises: 0013_delivery_events

A FAILED attempt states its cause: PROVIDER_REFUSED (Meta answered with an error; its codes are kept, no provider
text) or PROVIDER_OUTCOME_UNKNOWN (no clear answer, so the message may have gone out; the attempt blocks a resend).
No existing row is FAILED, because no channel could fail before this revision. Contract:
kisaniti/docs/contracts/L5_MESSAGING_v1_AMENDMENT_A1_CLOUD_API.md.
"""

from alembic import op

revision = "0014_whatsapp_send"
down_revision = "0013_delivery_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE delivery_attempts
            ADD COLUMN failure text CHECK (failure IN ('PROVIDER_REFUSED', 'PROVIDER_OUTCOME_UNKNOWN')),
            ADD COLUMN error_codes integer[] NOT NULL DEFAULT '{}',
            ADD CONSTRAINT delivery_attempts_failure CHECK ((status = 'FAILED') = (failure IS NOT NULL)),
            ADD CONSTRAINT delivery_attempts_error_codes
                CHECK (coalesce(failure, '') = 'PROVIDER_REFUSED' OR cardinality(error_codes) = 0)
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE delivery_attempts DROP CONSTRAINT delivery_attempts_error_codes, "
        "DROP CONSTRAINT delivery_attempts_failure, DROP COLUMN error_codes, DROP COLUMN failure"
    )
