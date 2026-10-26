"""Messaging slice messaging/v1: contacts, consents, access log, instructions and delivery attempts.

Revision ID: 0010_messaging
Revises: 0009_approval

All tables are tenant-owned (row-level security, composite keys to the member, plan and instruction of the same FPO)
and append-only, also for the owner. There is deliberately no column for Aadhaar, bank, land, caste, religion or
income (privacy contract §2). Phone numbers live only in `member_contacts` and are shown masked by the API unless the
caller may see them; every unmasked view is written to `personal_data_access_log`. Contract:
kisaniti/docs/contracts/L5_MESSAGING_CONTRACT_v1.md.
"""

from alembic import op

revision = "0010_messaging"
down_revision = "0009_approval"
branch_labels = None
depends_on = None

TENANT = "fpo_id = nullif(current_setting('app.fpo_id', true), '')"


def _tenant(table: str, grants: str = "SELECT, INSERT") -> None:
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
        CREATE TABLE member_contacts (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            member_id   text NOT NULL,
            fpo_id      text NOT NULL,
            version     integer NOT NULL CHECK (version >= 1),
            phone_e164  text NOT NULL CHECK (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
            source      text NOT NULL DEFAULT 'STAFF_ENTERED' CHECK (source = 'STAFF_ENTERED'),
            recorded_by text NOT NULL REFERENCES users (id),
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            request_id  text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            FOREIGN KEY (member_id, fpo_id) REFERENCES members (id, fpo_id),
            UNIQUE (member_id, version)
        )
        """
    )
    _append_only("member_contacts")
    _tenant("member_contacts")
    op.execute("GRANT USAGE ON SEQUENCE member_contacts_id_seq TO kisaniti_app")

    op.execute(
        r"""
        CREATE TABLE member_consents (
            id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            member_id      text NOT NULL,
            fpo_id         text NOT NULL,
            event_no       integer NOT NULL CHECK (event_no >= 1),
            kind           text NOT NULL CHECK (kind IN ('ADVICE', 'WHATSAPP', 'RESEARCH')),
            action         text NOT NULL CHECK (action IN ('GRANTED', 'WITHDRAWN')),
            notice_version text NOT NULL CHECK (notice_version ~ '^[A-Z0-9][A-Z0-9._-]{2,40}$'),
            method         text NOT NULL CHECK (method IN ('STAFF_ASSISTED_ORAL', 'PAPER_FORM', 'DIGITAL_FORM')),
            recorded_by    text NOT NULL REFERENCES users (id),
            recorded_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
            request_id     text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            FOREIGN KEY (member_id, fpo_id) REFERENCES members (id, fpo_id),
            UNIQUE (member_id, event_no)
        )
        """
    )
    _append_only("member_consents")
    _tenant("member_consents")
    op.execute("GRANT USAGE ON SEQUENCE member_consents_id_seq TO kisaniti_app")

    op.execute(
        r"""
        CREATE TABLE personal_data_access_log (
            id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            fpo_id        text NOT NULL REFERENCES fpo (id),
            actor_user_id text NOT NULL REFERENCES users (id),
            member_id     text NOT NULL,
            what          text NOT NULL CHECK (what IN ('PHONE_VIEWED', 'PHONE_HISTORY_VIEWED')),
            request_id    text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            occurred_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
            FOREIGN KEY (member_id, fpo_id) REFERENCES members (id, fpo_id)
        )
        """
    )
    _append_only("personal_data_access_log")
    _tenant("personal_data_access_log")
    op.execute("GRANT USAGE ON SEQUENCE personal_data_access_log_id_seq TO kisaniti_app")

    op.execute(
        r"""
        CREATE TABLE instructions (
            id                text PRIMARY KEY CHECK (id ~ '^ins_[0-9A-HJKMNP-TV-Z]{26}$'),
            plan_id           text NOT NULL,
            fpo_id            text NOT NULL,
            member_id         text NOT NULL,
            revision_no       integer NOT NULL CHECK (revision_no >= 1),
            kind              text NOT NULL CHECK (kind IN ('SALE_INSTRUCTION', 'CANCELLATION')),
            version           integer NOT NULL CHECK (version >= 1),
            status            text NOT NULL CHECK (status IN ('PREPARED', 'BLOCKED')),
            issues            text[] NOT NULL DEFAULT '{}',
            language          text CHECK (language IN ('pa', 'hi', 'en')),
            template_id       text,
            template_version  integer,
            template_review   text CHECK (template_review IN ('UNREVIEWED', 'REVIEWED')),
            template_sha256   text CHECK (template_sha256 ~ '^[0-9a-f]{64}$'),
            body              text,
            facts             jsonb NOT NULL,
            contact_version   integer,
            consent_event_no  integer,
            supersedes        text REFERENCES instructions (id),
            prepared_by       text NOT NULL REFERENCES users (id),
            prepared_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
            request_id        text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            FOREIGN KEY (plan_id, fpo_id) REFERENCES plans (id, fpo_id),
            FOREIGN KEY (member_id, fpo_id) REFERENCES members (id, fpo_id),
            UNIQUE (id, fpo_id),
            UNIQUE (plan_id, member_id, kind, version),
            CHECK ((status = 'PREPARED') = (body IS NOT NULL)),
            CHECK ((status = 'BLOCKED') = (cardinality(issues) > 0))
        )
        """
    )
    op.execute("CREATE INDEX instructions_plan ON instructions (plan_id, member_id, kind, version DESC)")
    _append_only("instructions")
    _tenant("instructions")

    op.execute(
        r"""
        CREATE TABLE delivery_attempts (
            id                  text PRIMARY KEY CHECK (id ~ '^att_[0-9A-HJKMNP-TV-Z]{26}$'),
            instruction_id      text NOT NULL,
            fpo_id              text NOT NULL,
            channel             text NOT NULL CHECK (channel IN ('PREVIEW', 'WHATSAPP')),
            status              text NOT NULL CHECK (status IN ('PREVIEWED', 'BLOCKED', 'QUEUED', 'SENT', 'DELIVERED',
                                                                 'READ', 'FAILED')),
            issues              text[] NOT NULL DEFAULT '{}',
            provider_message_id text,
            simulated           boolean NOT NULL,
            preview             jsonb,
            contact_version     integer,
            attempted_by        text NOT NULL REFERENCES users (id),
            attempted_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
            request_id          text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            request_key         text CHECK (length(request_key) BETWEEN 8 AND 120),
            request_sha256      text CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
            FOREIGN KEY (instruction_id, fpo_id) REFERENCES instructions (id, fpo_id),
            CHECK (channel <> 'PREVIEW' OR (simulated AND status IN ('PREVIEWED', 'BLOCKED')
                                            AND provider_message_id IS NULL)),
            CHECK ((status = 'BLOCKED') = (cardinality(issues) > 0)),
            CHECK ((request_key IS NULL) = (request_sha256 IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX delivery_attempts_instruction ON delivery_attempts (instruction_id, attempted_at DESC)")
    op.execute(
        "CREATE UNIQUE INDEX delivery_attempts_idempotency ON delivery_attempts (fpo_id, request_key) "
        "WHERE request_key IS NOT NULL"
    )
    _append_only("delivery_attempts")
    _tenant("delivery_attempts")


def downgrade() -> None:
    for t in ("delivery_attempts", "instructions", "personal_data_access_log", "member_consents", "member_contacts"):
        op.execute(f"DROP TABLE {t}")
        op.execute(f"DROP FUNCTION {t}_append_only()")
