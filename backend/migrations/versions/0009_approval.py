"""Approval slice approval/v1: plan revisions, the manager's decision, reservations and releases.

Revision ID: 0009_approval
Revises: 0008_plans

All four tables are tenant-owned (row-level security, composite keys to the plan and the lots of the same FPO) and
append-only, also for the owner. The reservation itself is the ledger's RESERVED event (org/v1); `plan_reservations`
links it to the plan and the approved revision. A decision is unique per plan: that, and an advisory lock taken by the
endpoints, serialise concurrent decisions. Contract: kisaniti/docs/contracts/L4_APPROVAL_CONTRACT_v1.md.
"""

from alembic import op

revision = "0009_approval"
down_revision = "0008_plans"
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
    op.execute(
        r"""
        CREATE TABLE plan_revisions (
            id               text PRIMARY KEY CHECK (id ~ '^rev_[0-9A-HJKMNP-TV-Z]{26}$'),
            plan_id          text NOT NULL,
            fpo_id           text NOT NULL,
            revision_no      integer NOT NULL CHECK (revision_no >= 2),
            base_revision_no integer NOT NULL CHECK (base_revision_no >= 1 AND base_revision_no < revision_no),
            doc              jsonb NOT NULL,
            request_key      text CHECK (length(request_key) BETWEEN 8 AND 120),
            request_sha256   text CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
            created_by       text NOT NULL REFERENCES users (id),
            created_at       timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (plan_id, fpo_id) REFERENCES plans (id, fpo_id),
            UNIQUE (plan_id, revision_no),
            CHECK ((request_key IS NULL) = (request_sha256 IS NULL))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX plan_revisions_idempotency ON plan_revisions (fpo_id, request_key) "
        "WHERE request_key IS NOT NULL"
    )
    _append_only("plan_revisions")
    _tenant("plan_revisions")

    op.execute(
        r"""
        CREATE TABLE plan_decisions (
            plan_id         text PRIMARY KEY,
            fpo_id          text NOT NULL,
            revision_no     integer NOT NULL CHECK (revision_no >= 1),
            decision        text NOT NULL CHECK (decision IN ('APPROVED', 'REJECTED')),
            reason_code     text CHECK (reason_code IN ('PRICE_CONCERN', 'QUANTITY_CONCERN', 'MARKET_CONCERN',
                                                         'STORAGE_CONCERN', 'OTHER')),
            acknowledged    text[] NOT NULL DEFAULT '{}',
            context         jsonb NOT NULL,
            decided_by      text NOT NULL REFERENCES users (id),
            decided_at      timestamptz NOT NULL DEFAULT now(),
            request_id      text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            request_key     text CHECK (length(request_key) BETWEEN 8 AND 120),
            request_sha256  text CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
            FOREIGN KEY (plan_id, fpo_id) REFERENCES plans (id, fpo_id),
            CHECK ((decision = 'REJECTED') = (reason_code IS NOT NULL)),
            CHECK (decision = 'APPROVED' OR cardinality(acknowledged) = 0),
            CHECK ((request_key IS NULL) = (request_sha256 IS NULL))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX plan_decisions_idempotency ON plan_decisions (fpo_id, request_key) "
        "WHERE request_key IS NOT NULL"
    )
    _append_only("plan_decisions")
    _tenant("plan_decisions")

    op.execute(
        r"""
        CREATE TABLE plan_reservations (
            plan_id        text NOT NULL,
            fpo_id         text NOT NULL,
            lot_id         text NOT NULL,
            revision_no    integer NOT NULL,
            quantity_g     bigint NOT NULL CHECK (quantity_g > 0),
            stock_event_id bigint NOT NULL REFERENCES stock_events (id),
            created_at     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (plan_id, lot_id),
            FOREIGN KEY (plan_id, fpo_id) REFERENCES plans (id, fpo_id),
            FOREIGN KEY (lot_id, fpo_id) REFERENCES lots (id, fpo_id)
        )
        """
    )
    _append_only("plan_reservations")
    _tenant("plan_reservations")

    op.execute(
        r"""
        CREATE TABLE plan_releases (
            plan_id        text NOT NULL,
            fpo_id         text NOT NULL,
            lot_id         text NOT NULL,
            quantity_g     bigint NOT NULL CHECK (quantity_g > 0),
            stock_event_id bigint NOT NULL REFERENCES stock_events (id),
            reason_code    text NOT NULL CHECK (reason_code IN ('PLAN_CANCELLED', 'STOCK_NEEDED_ELSEWHERE',
                                                                 'MARKET_CHANGED', 'OTHER')),
            released_by    text NOT NULL REFERENCES users (id),
            released_at    timestamptz NOT NULL DEFAULT now(),
            request_id     text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            request_key    text CHECK (length(request_key) BETWEEN 8 AND 120),
            request_sha256 text CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
            PRIMARY KEY (plan_id, lot_id),
            FOREIGN KEY (plan_id, fpo_id) REFERENCES plans (id, fpo_id),
            FOREIGN KEY (plan_id, lot_id) REFERENCES plan_reservations (plan_id, lot_id),
            CHECK ((request_key IS NULL) = (request_sha256 IS NULL))
        )
        """
    )
    _append_only("plan_releases")
    _tenant("plan_releases")


def downgrade() -> None:
    for t in ("plan_releases", "plan_reservations", "plan_decisions", "plan_revisions"):
        op.execute(f"DROP TABLE {t}")
        op.execute(f"DROP FUNCTION {t}_append_only()")
