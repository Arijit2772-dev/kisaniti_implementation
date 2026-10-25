"""Organisation data (org/v1): members, locations, lots, placements and the exact stock ledger.

Revision ID: 0007_org
Revises: 0006_market_reference

Tenant-owned: every table carries fpo_id, row-level security, and a composite key so a child row can never name a
parent of another FPO. Stock quantities are integer grams. The ledger is append-only and a trigger enforces the
invariants (on hand, reserved and available never negative; REGISTERED exactly once and first) after locking the lot, so
two concurrent writers cannot both pass. Contract: L1_L2_ORG_AND_MARKET_CONTRACT_v1.md.
"""

from alembic import op

revision = "0007_org"
down_revision = "0006_market_reference"
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
        CREATE TABLE members (
            id                 text PRIMARY KEY CHECK (id ~ '^mbr_[0-9A-HJKMNP-TV-Z]{26}$'),
            fpo_id             text NOT NULL REFERENCES fpo (id),
            display_name       text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 120),
            village            text CHECK (length(village) <= 100),
            preferred_language text NOT NULL CHECK (preferred_language IN ('pa', 'hi', 'en')),
            active             boolean NOT NULL DEFAULT true,
            origin             text NOT NULL CHECK (origin IN ('REAL', 'POLICY', 'SYNTHETIC')),
            created_at         timestamptz NOT NULL DEFAULT now(),
            UNIQUE (id, fpo_id)
        )
        """
    )
    op.execute("CREATE INDEX members_fpo ON members (fpo_id, display_name)")
    _tenant("members", "SELECT, INSERT")
    op.execute("GRANT UPDATE (display_name, village, preferred_language, active) ON members TO kisaniti_app")

    op.execute(
        r"""
        CREATE TABLE locations (
            id         text PRIMARY KEY CHECK (id ~ '^stl_[0-9A-HJKMNP-TV-Z]{26}$'),
            fpo_id     text NOT NULL REFERENCES fpo (id),
            name       text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
            kind       text NOT NULL CHECK (kind IN ('COLLECTION_CENTRE', 'FARM', 'WAREHOUSE', 'THIRD_PARTY')),
            village    text CHECK (length(village) <= 100),
            latitude   double precision CHECK (latitude BETWEEN -90 AND 90),
            longitude  double precision CHECK (longitude BETWEEN -180 AND 180),
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK ((latitude IS NULL) = (longitude IS NULL)),
            UNIQUE (id, fpo_id)
        )
        """
    )
    _tenant("locations", "SELECT, INSERT")

    op.execute(
        r"""
        CREATE TABLE lots (
            id              text PRIMARY KEY CHECK (id ~ '^lot_[0-9A-HJKMNP-TV-Z]{26}$'),
            fpo_id          text NOT NULL REFERENCES fpo (id),
            member_id       text NOT NULL,
            commodity_id    text NOT NULL REFERENCES commodities (id),
            source_variety  text CHECK (length(source_variety) <= 60),
            source_grade    text CHECK (length(source_grade) <= 60),
            registered_grams bigint NOT NULL CHECK (registered_grams > 0 AND registered_grams <= 1000000000000),
            harvested_on    date,
            verified_by     text REFERENCES users (id),
            verified_at     timestamptz,
            origin          text NOT NULL CHECK (origin IN ('REAL', 'POLICY', 'SYNTHETIC')),
            created_at      timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (member_id, fpo_id) REFERENCES members (id, fpo_id),
            CHECK ((verified_by IS NULL) = (verified_at IS NULL)),
            UNIQUE (id, fpo_id)
        )
        """
    )
    op.execute("CREATE INDEX lots_fpo ON lots (fpo_id, created_at DESC)")
    _tenant("lots", "SELECT, INSERT")

    op.execute(
        r"""
        CREATE TABLE lot_placements (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            lot_id      text NOT NULL,
            fpo_id      text NOT NULL,
            location_id text NOT NULL,
            placed_at   timestamptz NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            created_by  text REFERENCES users (id),
            FOREIGN KEY (lot_id, fpo_id) REFERENCES lots (id, fpo_id),
            FOREIGN KEY (location_id, fpo_id) REFERENCES locations (id, fpo_id),
            UNIQUE (lot_id, placed_at)
        )
        """
    )
    op.execute("CREATE INDEX lot_placements_lot ON lot_placements (lot_id, placed_at DESC)")
    _append_only("lot_placements")
    _tenant("lot_placements", "SELECT, INSERT")
    op.execute("GRANT USAGE ON SEQUENCE lot_placements_id_seq TO kisaniti_app")

    op.execute(
        r"""
        CREATE TABLE stock_events (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            lot_id      text NOT NULL,
            fpo_id      text NOT NULL,
            kind        text NOT NULL CHECK (kind IN ('REGISTERED', 'ADJUSTED', 'RESERVED', 'RESERVATION_RELEASED',
                                                      'RESERVATION_CONSUMED', 'SOLD')),
            quantity_g  bigint NOT NULL CHECK (abs(quantity_g) <= 1000000000000),
            reason_code text CHECK (reason_code IN ('WEIGHING_CORRECTION', 'SPILLAGE', 'MOISTURE_LOSS', 'OTHER')),
            request_key text CHECK (length(request_key) BETWEEN 8 AND 120),
            request_sha256 text CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
            created_by  text REFERENCES users (id),
            created_at  timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (lot_id, fpo_id) REFERENCES lots (id, fpo_id),
            CHECK ((kind = 'ADJUSTED') = (reason_code IS NOT NULL)),
            CHECK (kind = 'ADJUSTED' OR quantity_g > 0),
            CHECK (kind <> 'ADJUSTED' OR quantity_g <> 0)
        )
        """
    )
    op.execute("CREATE INDEX stock_events_lot ON stock_events (lot_id, id)")
    op.execute(
        "CREATE UNIQUE INDEX stock_events_idempotency ON stock_events (fpo_id, request_key) "
        "WHERE request_key IS NOT NULL"
    )
    _append_only("stock_events")
    # The invariants live in the database. SECURITY DEFINER: the restricted role may insert events but has no right to
    # lock or read the lot row itself; the function runs with the owner's rights and only ever touches NEW.lot_id.
    op.execute(
        r"""
        CREATE FUNCTION stock_events_check() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = public AS $$
        DECLARE
            on_hand bigint; reserved bigint; has_registered boolean; delta_hand bigint := 0; delta_res bigint := 0;
        BEGIN
            PERFORM 1 FROM lots WHERE id = NEW.lot_id AND fpo_id = NEW.fpo_id FOR UPDATE;
            SELECT coalesce(sum(CASE kind WHEN 'REGISTERED' THEN quantity_g WHEN 'ADJUSTED' THEN quantity_g
                                          WHEN 'SOLD' THEN -quantity_g ELSE 0 END), 0),
                   coalesce(sum(CASE kind WHEN 'RESERVED' THEN quantity_g WHEN 'RESERVATION_RELEASED' THEN -quantity_g
                                          WHEN 'RESERVATION_CONSUMED' THEN -quantity_g ELSE 0 END), 0),
                   coalesce(bool_or(kind = 'REGISTERED'), false)
              INTO on_hand, reserved, has_registered FROM stock_events WHERE lot_id = NEW.lot_id;
            IF NEW.kind = 'REGISTERED' AND has_registered THEN
                RAISE EXCEPTION 'stock invariant: a lot is registered exactly once' USING ERRCODE = '23514';
            END IF;
            IF NEW.kind <> 'REGISTERED' AND NOT has_registered THEN
                RAISE EXCEPTION 'stock invariant: the first event of a lot must be REGISTERED' USING ERRCODE = '23514';
            END IF;
            IF NEW.kind IN ('REGISTERED', 'ADJUSTED') THEN delta_hand := NEW.quantity_g;
            ELSIF NEW.kind = 'SOLD' THEN delta_hand := -NEW.quantity_g;
            ELSIF NEW.kind = 'RESERVED' THEN delta_res := NEW.quantity_g;
            ELSE delta_res := -NEW.quantity_g;
            END IF;
            on_hand := on_hand + delta_hand;
            reserved := reserved + delta_res;
            IF on_hand < 0 THEN
                RAISE EXCEPTION 'stock invariant: on-hand quantity would be negative' USING ERRCODE = '23514';
            END IF;
            IF reserved < 0 THEN
                RAISE EXCEPTION 'stock invariant: reserved quantity would be negative' USING ERRCODE = '23514';
            END IF;
            IF on_hand - reserved < 0 THEN
                RAISE EXCEPTION 'stock invariant: available quantity would be negative' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER stock_events_invariants BEFORE INSERT ON stock_events "
        "FOR EACH ROW EXECUTE FUNCTION stock_events_check()"
    )
    _tenant("stock_events", "SELECT, INSERT")
    op.execute("GRANT USAGE ON SEQUENCE stock_events_id_seq TO kisaniti_app")
    op.execute(
        r"""
        CREATE VIEW lot_balances WITH (security_invoker = true) AS
        SELECT lot_id, fpo_id,
               sum(CASE kind WHEN 'REGISTERED' THEN quantity_g WHEN 'ADJUSTED' THEN quantity_g
                             WHEN 'SOLD' THEN -quantity_g ELSE 0 END)::bigint AS on_hand_g,
               sum(CASE kind WHEN 'RESERVED' THEN quantity_g WHEN 'RESERVATION_RELEASED' THEN -quantity_g
                             WHEN 'RESERVATION_CONSUMED' THEN -quantity_g ELSE 0 END)::bigint AS reserved_g,
               sum(CASE kind WHEN 'SOLD' THEN quantity_g ELSE 0 END)::bigint AS sold_g
        FROM stock_events GROUP BY lot_id, fpo_id
        """
    )
    op.execute("GRANT SELECT ON lot_balances TO kisaniti_app")


def downgrade() -> None:
    op.execute("DROP VIEW lot_balances")
    for t in ("stock_events", "lot_placements"):
        op.execute(f"DROP TABLE {t}")
        op.execute(f"DROP FUNCTION {t}_append_only()")
    op.execute("DROP FUNCTION stock_events_check()")
    for t in ("lots", "locations", "members"):
        op.execute(f"DROP TABLE {t}")
