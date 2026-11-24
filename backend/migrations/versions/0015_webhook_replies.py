"""messaging/v1 amendment A1.3: farmers' WhatsApp replies arrive through the webhook.

Revision ID: 0015_webhook_replies
Revises: 0014_whatsapp_send

A reply from the webhook keeps Meta's message ID, so a webhook Meta delivers twice stores the reply once. A reply typed
in by staff has none. The sender's number is not stored: the reply belongs to an instruction, which knows its member.
Contract: kisaniti/docs/contracts/L5_MESSAGING_v1_AMENDMENT_A1_CLOUD_API.md.
"""

from alembic import op

revision = "0015_webhook_replies"
down_revision = "0014_whatsapp_send"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE replies
            ADD COLUMN provider_message_id text CHECK (length(provider_message_id) BETWEEN 1 AND 200),
            ADD CONSTRAINT replies_webhook_has_id CHECK ((source = 'WEBHOOK') = (provider_message_id IS NOT NULL)),
            ADD CONSTRAINT replies_webhook_has_no_user CHECK (source <> 'WEBHOOK' OR recorded_by IS NULL)
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX replies_provider_id ON replies (provider_message_id) WHERE provider_message_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX replies_provider_id")
    op.execute(
        "ALTER TABLE replies DROP CONSTRAINT replies_webhook_has_no_user, DROP CONSTRAINT replies_webhook_has_id, "
        "DROP COLUMN provider_message_id"
    )
