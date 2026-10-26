"""Sales slice sales/v1: actual sales, reservation releases made for a sale, voids and farmers' replies.

Revision ID: 0011_sales
Revises: 0010_messaging

All tables are tenant-owned (row-level security, composite keys) and append-only, also for the owner. A sale links to
the stock ledger by the event IDs of the events written in the same transaction. Contract:
kisaniti/docs/contracts/L5_SALES_CONTRACT_v1.md.
"""

from alembic import op

revision = "0011_sales"
down_revision = "0010_messaging"
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
        CREATE TABLE actual_sales (
            id                 text PRIMARY KEY CHECK (id ~ '^sal_[0-9A-HJKMNP-TV-Z]{26}$'),
            fpo_id             text NOT NULL,
            lot_id             text NOT NULL,
            member_id          text NOT NULL,
            plan_id            text,
            plan_mandi_id      text REFERENCES mandis (id),
            plan_date          date,
            mandi_id           text NOT NULL REFERENCES mandis (id),
            sale_date          date NOT NULL,
            quantity_g         bigint NOT NULL CHECK (quantity_g > 0),
            price_paise        bigint NOT NULL CHECK (price_paise > 0),
            amount_paise       bigint NOT NULL CHECK (amount_paise >= 0),
            j_form_no          text NOT NULL CHECK (j_form_no ~ '^[A-Za-z0-9][A-Za-z0-9/_-]{0,39}$'),
            consumed_g         bigint NOT NULL CHECK (consumed_g >= 0),
            origin             text NOT NULL CHECK (origin IN ('REAL', 'POLICY', 'SYNTHETIC')),
            sold_event_id      bigint NOT NULL REFERENCES stock_events (id),
            consumed_event_id  bigint REFERENCES stock_events (id),
            recorded_by        text NOT NULL REFERENCES users (id),
            recorded_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
            request_id         text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            request_key        text CHECK (length(request_key) BETWEEN 8 AND 120),
            request_sha256     text CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
            FOREIGN KEY (lot_id, fpo_id) REFERENCES lots (id, fpo_id),
            FOREIGN KEY (member_id, fpo_id) REFERENCES members (id, fpo_id),
            FOREIGN KEY (plan_id, fpo_id) REFERENCES plans (id, fpo_id),
            UNIQUE (id, fpo_id),
            UNIQUE (fpo_id, j_form_no, lot_id),
            CHECK ((plan_id IS NULL) = (plan_mandi_id IS NULL) AND (plan_id IS NULL) = (plan_date IS NULL)),
            CHECK ((plan_id IS NULL AND consumed_g = 0 AND consumed_event_id IS NULL)
                   OR (plan_id IS NOT NULL AND consumed_g = quantity_g AND consumed_event_id IS NOT NULL)),
            CHECK ((request_key IS NULL) = (request_sha256 IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX actual_sales_lot ON actual_sales (lot_id, recorded_at DESC)")
    op.execute("CREATE INDEX actual_sales_plan ON actual_sales (plan_id) WHERE plan_id IS NOT NULL")
    op.execute(
        "CREATE UNIQUE INDEX actual_sales_idempotency ON actual_sales (fpo_id, request_key) "
        "WHERE request_key IS NOT NULL"
    )
    _append_only("actual_sales")
    _tenant("actual_sales")

    op.execute(
        r"""
        CREATE TABLE sale_releases (
            sale_id        text NOT NULL,
            plan_id        text NOT NULL,
            lot_id         text NOT NULL,
            fpo_id         text NOT NULL,
            quantity_g     bigint NOT NULL CHECK (quantity_g > 0),
            stock_event_id bigint NOT NULL REFERENCES stock_events (id),
            PRIMARY KEY (sale_id, plan_id),
            FOREIGN KEY (sale_id, fpo_id) REFERENCES actual_sales (id, fpo_id),
            FOREIGN KEY (plan_id, lot_id) REFERENCES plan_reservations (plan_id, lot_id)
        )
        """
    )
    _append_only("sale_releases")
    _tenant("sale_releases")

    op.execute(
        r"""
        CREATE TABLE sale_voids (
            sale_id          text PRIMARY KEY,
            fpo_id           text NOT NULL,
            reason_code      text NOT NULL CHECK (reason_code IN ('ENTRY_ERROR', 'BUYER_CANCELLED', 'OTHER')),
            restore_event_id bigint NOT NULL REFERENCES stock_events (id),
            voided_by        text NOT NULL REFERENCES users (id),
            voided_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
            request_id       text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            FOREIGN KEY (sale_id, fpo_id) REFERENCES actual_sales (id, fpo_id)
        )
        """
    )
    _append_only("sale_voids")
    _tenant("sale_voids")

    op.execute(
        r"""
        CREATE TABLE replies (
            id             text PRIMARY KEY CHECK (id ~ '^rpl_[0-9A-HJKMNP-TV-Z]{26}$'),
            fpo_id         text NOT NULL,
            instruction_id text NOT NULL,
            member_id      text NOT NULL,
            body           text NOT NULL CHECK (length(body) BETWEEN 1 AND 500),
            source         text NOT NULL CHECK (source IN ('STAFF_ENTERED', 'WEBHOOK')),
            recorded_by    text REFERENCES users (id),
            recorded_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
            request_id     text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            FOREIGN KEY (instruction_id, fpo_id) REFERENCES instructions (id, fpo_id),
            FOREIGN KEY (member_id, fpo_id) REFERENCES members (id, fpo_id)
        )
        """
    )
    op.execute("CREATE INDEX replies_instruction ON replies (instruction_id, recorded_at DESC)")
    _append_only("replies")
    _tenant("replies")


def downgrade() -> None:
    for t in ("replies", "sale_voids", "sale_releases", "actual_sales"):
        op.execute(f"DROP TABLE {t}")
        op.execute(f"DROP FUNCTION {t}_append_only()")
