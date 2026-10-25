"""Market reference data (market/v1): commodities, mandis, MSP, dataset versions, price observations.

Revision ID: 0006_market_reference
Revises: 0005_storage_assessments

Global data: no fpo_id. Every FPO may read it (the restricted role has SELECT); only the import job (owner) writes it.
Price observations and dataset status changes are append-only. A REJECTED dataset's rows are invisible to the
restricted role (row-level security through the dataset), whatever the application does.
"""

from alembic import op

revision = "0006_market_reference"
down_revision = "0005_storage_assessments"
branch_labels = None
depends_on = None


def _append_only(table: str) -> None:
    fn = f"{table}_append_only"
    op.execute(
        f"""
        CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '{table} is append-only';
        END $$;
        """
    )
    op.execute(
        f"CREATE TRIGGER {table}_no_change BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION {fn}()"
    )


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE commodities (
            id          text PRIMARY KEY CHECK (id ~ '^cmd_[0-9A-HJKMNP-TV-Z]{26}$'),
            source_name text NOT NULL UNIQUE CHECK (length(source_name) BETWEEN 1 AND 80),
            crop        text NOT NULL CHECK (crop IN ('PADDY', 'WHEAT')),
            msp_applies boolean NOT NULL
        )
        """
    )
    op.execute(
        r"""
        CREATE TABLE mandis (
            id                text PRIMARY KEY CHECK (id ~ '^mnd_[0-9A-HJKMNP-TV-Z]{26}$'),
            name              text NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 120),
            town              text,
            state             text NOT NULL DEFAULT 'Punjab',
            latitude          double precision CHECK (latitude BETWEEN -90 AND 90),
            longitude         double precision CHECK (longitude BETWEEN -180 AND 180),
            coordinate_source text,
            CHECK ((latitude IS NULL) = (longitude IS NULL))
        )
        """
    )
    op.execute(
        r"""
        CREATE TABLE msp_entries (
            crop             text NOT NULL CHECK (crop IN ('PADDY', 'WHEAT')),
            grade            text NOT NULL CHECK (grade IN ('common', 'grade_a', 'all')),
            marketing_season text NOT NULL CHECK (marketing_season ~ '^(KMS|RMS) [0-9]{4}-[0-9]{2}$'),
            msp_inr_per_quintal numeric(12, 2) NOT NULL CHECK (msp_inr_per_quintal > 0),
            announced_on     date NOT NULL,
            source_url       text NOT NULL,
            verified         boolean NOT NULL,
            PRIMARY KEY (crop, grade, marketing_season)
        )
        """
    )
    op.execute(
        r"""
        CREATE TABLE dataset_versions (
            id                text PRIMARY KEY CHECK (id ~ '^dsv_[0-9A-HJKMNP-TV-Z]{26}$'),
            name              text NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 120),
            rows              integer NOT NULL CHECK (rows >= 0),
            dated_rows        integer NOT NULL CHECK (dated_rows >= 0),
            blank_date_rows   integer NOT NULL CHECK (blank_date_rows >= 0),
            flagged_rows      integer NOT NULL CHECK (flagged_rows >= 0),
            validation_status text NOT NULL DEFAULT 'PENDING_HAND_CHECK'
                              CHECK (validation_status IN ('PENDING_HAND_CHECK', 'VALIDATED', 'REJECTED')),
            manifest_sha256   text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
            manifest          jsonb NOT NULL,
            imported_at       timestamptz NOT NULL DEFAULT now(),
            note              text,
            CHECK (rows = dated_rows + blank_date_rows)
        )
        """
    )
    op.execute(
        r"""
        CREATE TABLE dataset_status_log (
            id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            dataset_version_id text NOT NULL REFERENCES dataset_versions (id),
            from_status text NOT NULL,
            to_status  text NOT NULL,
            changed_at timestamptz NOT NULL DEFAULT now(),
            note       text
        )
        """
    )
    _append_only("dataset_status_log")
    op.execute(
        r"""
        CREATE TABLE price_observations (
            id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            dataset_version_id text NOT NULL REFERENCES dataset_versions (id),
            mandi_id           text NOT NULL REFERENCES mandis (id),
            commodity_id       text NOT NULL REFERENCES commodities (id),
            variety_label      text NOT NULL,
            observed_on        date NOT NULL,
            min_price          numeric(12, 2) CHECK (min_price > 0),
            max_price          numeric(12, 2) CHECK (max_price > 0),
            modal_price        numeric(12, 2) CHECK (modal_price > 0),
            arrivals_tonnes    numeric(14, 3) CHECK (arrivals_tonnes >= 0),
            date_inferred      boolean NOT NULL,
            arrivals_shared    boolean NOT NULL,
            flags              text[] NOT NULL DEFAULT '{}',
            source_file        text NOT NULL,
            source_line        integer NOT NULL,
            UNIQUE (dataset_version_id, source_file, source_line)
        )
        """
    )
    op.execute("CREATE INDEX price_obs_series ON price_observations (mandi_id, commodity_id, observed_on)")
    op.execute("CREATE INDEX price_obs_date ON price_observations (observed_on)")
    _append_only("price_observations")
    op.execute("ALTER TABLE price_observations ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY not_rejected ON price_observations FOR SELECT USING (EXISTS (SELECT 1 FROM dataset_versions d "
        "WHERE d.id = price_observations.dataset_version_id AND d.validation_status <> 'REJECTED'))"
    )
    for t in ("commodities", "mandis", "msp_entries", "dataset_versions", "price_observations"):
        op.execute(f"GRANT SELECT ON {t} TO kisaniti_app")


def downgrade() -> None:
    for t in ("price_observations", "dataset_status_log", "dataset_versions", "msp_entries", "mandis", "commodities"):
        op.execute(f"DROP TABLE {t}")
    op.execute("DROP FUNCTION price_observations_append_only()")
    op.execute("DROP FUNCTION dataset_status_log_append_only()")
