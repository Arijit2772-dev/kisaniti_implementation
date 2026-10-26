"""Handover slice handover/v1: hand-overs, their items and the manager's reports.

Revision ID: 0012_handover
Revises: 0011_sales

Tenant-owned and append-only like the messaging tables. A link (which contains a phone number and the message text) is
never stored: only the instruction, the frozen contact version and the text hash are. The access log learns one new
reason, PHONE_LINK_ISSUED. Contract: kisaniti/docs/contracts/L5_HANDOVER_CONTRACT_v1.md.
"""

from alembic import op

revision = "0012_handover"
down_revision = "0011_sales"
branch_labels = None
depends_on = None

TENANT = "fpo_id = nullif(current_setting('app.fpo_id', true), '')"


def _tenant(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT}) WITH CHECK ({TENANT})")
    op.execute(f"GRANT SELECT, INSERT ON {table} TO kisaniti_app")


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
    op.execute("ALTER TABLE personal_data_access_log DROP CONSTRAINT personal_data_access_log_what_check")
    op.execute(
        "ALTER TABLE personal_data_access_log ADD CONSTRAINT personal_data_access_log_what_check "
        "CHECK (what IN ('PHONE_VIEWED', 'PHONE_HISTORY_VIEWED', 'PHONE_LINK_ISSUED'))"
    )
    op.execute(
        r"""
        CREATE TABLE handovers (
            id                      text PRIMARY KEY CHECK (id ~ '^hnd_[0-9A-HJKMNP-TV-Z]{26}$'),
            plan_id                 text NOT NULL,
            fpo_id                  text NOT NULL,
            kind                    text NOT NULL CHECK (kind IN ('SALE_INSTRUCTION', 'CANCELLATION')),
            created_by              text NOT NULL REFERENCES users (id),
            created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
            recipient_count         integer NOT NULL CHECK (recipient_count >= 1),
            acknowledged_unreviewed boolean NOT NULL,
            request_id              text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            request_key             text CHECK (length(request_key) BETWEEN 8 AND 120),
            request_sha256          text CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
            FOREIGN KEY (plan_id, fpo_id) REFERENCES plans (id, fpo_id),
            UNIQUE (id, fpo_id),
            CHECK ((request_key IS NULL) = (request_sha256 IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX handovers_plan ON handovers (plan_id, created_at DESC)")
    op.execute(
        "CREATE UNIQUE INDEX handovers_idempotency ON handovers (fpo_id, request_key) WHERE request_key IS NOT NULL"
    )
    _append_only("handovers")
    _tenant("handovers")

    op.execute(
        r"""
        CREATE TABLE handover_items (
            handover_id     text NOT NULL,
            instruction_id  text NOT NULL,
            fpo_id          text NOT NULL,
            member_id       text NOT NULL,
            status          text NOT NULL CHECK (status IN ('ISSUED', 'BLOCKED')),
            issues          text[] NOT NULL DEFAULT '{}',
            contact_version integer,
            language        text CHECK (language IN ('pa', 'hi', 'en')),
            review_status   text CHECK (review_status IN ('UNREVIEWED', 'REVIEWED')),
            text_sha256     text CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
            PRIMARY KEY (handover_id, instruction_id),
            FOREIGN KEY (handover_id, fpo_id) REFERENCES handovers (id, fpo_id),
            FOREIGN KEY (instruction_id, fpo_id) REFERENCES instructions (id, fpo_id),
            FOREIGN KEY (member_id, fpo_id) REFERENCES members (id, fpo_id),
            CHECK ((status = 'BLOCKED') = (cardinality(issues) > 0)),
            CHECK (status = 'BLOCKED' OR (contact_version IS NOT NULL AND text_sha256 IS NOT NULL
                                           AND language IS NOT NULL AND review_status IS NOT NULL))
        )
        """
    )
    _append_only("handover_items")
    _tenant("handover_items")

    op.execute(
        r"""
        CREATE TABLE handover_reports (
            id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            handover_id    text NOT NULL,
            instruction_id text NOT NULL,
            fpo_id         text NOT NULL,
            outcome        text NOT NULL CHECK (outcome IN ('SENT', 'NOT_SENT')),
            reported_by    text NOT NULL REFERENCES users (id),
            reported_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
            request_id     text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            FOREIGN KEY (handover_id, instruction_id) REFERENCES handover_items (handover_id, instruction_id),
            FOREIGN KEY (handover_id, fpo_id) REFERENCES handovers (id, fpo_id)
        )
        """
    )
    op.execute("CREATE INDEX handover_reports_item ON handover_reports (handover_id, instruction_id, id DESC)")
    _append_only("handover_reports")
    _tenant("handover_reports")
    op.execute("GRANT USAGE ON SEQUENCE handover_reports_id_seq TO kisaniti_app")


def downgrade() -> None:
    for t in ("handover_reports", "handover_items", "handovers"):
        op.execute(f"DROP TABLE {t}")
        op.execute(f"DROP FUNCTION {t}_append_only()")
    op.execute("ALTER TABLE personal_data_access_log DROP CONSTRAINT personal_data_access_log_what_check")
    op.execute(
        "ALTER TABLE personal_data_access_log ADD CONSTRAINT personal_data_access_log_what_check "
        "CHECK (what IN ('PHONE_VIEWED', 'PHONE_HISTORY_VIEWED'))"
    )
