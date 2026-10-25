"""Access slice access/v1: users, sessions, throttling, audit, and the tenant walls.

Revision ID: 0003_access
Revises: 0002_sensor_readings

Contract: kisaniti/docs/contracts/L0_ACCESS_CONTRACT_v1.md.

The API connects as the migration owner for the narrow sign-in and session path. Every tenant-scoped request runs
`SET LOCAL ROLE kisaniti_app` and sets `app.fpo_id`; that role is not an owner, so row-level security applies to it.
It cannot read `users.password_hash`, cannot touch `sessions` or `login_attempts`, and can only append to `audit_log`.
"""

from alembic import op

revision = "0003_access"
down_revision = "0002_sensor_readings"
branch_labels = None
depends_on = None

TENANT = "fpo_id = nullif(current_setting('app.fpo_id', true), '')"


def upgrade() -> None:
    op.execute(
        r"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kisaniti_app') THEN
                CREATE ROLE kisaniti_app NOLOGIN NOINHERIT;
            END IF;
        END $$;
        """
    )
    op.execute("GRANT kisaniti_app TO CURRENT_USER")
    op.execute("GRANT USAGE ON SCHEMA public TO kisaniti_app")
    op.execute("GRANT SELECT ON fpo TO kisaniti_app")

    op.execute(
        r"""
        CREATE TABLE users (
            id                   text PRIMARY KEY CHECK (id ~ '^usr_[0-9A-HJKMNP-TV-Z]{26}$'),
            fpo_id               text REFERENCES fpo (id),
            username             text NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9][a-z0-9._-]{2,63}$'),
            display_name         text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 120),
            role                 text NOT NULL CHECK (role IN ('FPO_ADMIN', 'STAFF', 'VIEWER', 'PLATFORM_OPERATOR')),
            extra_capabilities   text[] NOT NULL DEFAULT '{}'
                                 CHECK (extra_capabilities <@ ARRAY['contacts.view_phone', 'messages.send']),
            language             text NOT NULL DEFAULT 'en' CHECK (language IN ('pa', 'en')),
            active               boolean NOT NULL DEFAULT true,
            must_change_password boolean NOT NULL,
            password_hash        text NOT NULL,
            origin               text NOT NULL CHECK (origin IN ('REAL', 'POLICY', 'SYNTHETIC')),
            created_at           timestamptz NOT NULL DEFAULT now(),
            password_changed_at  timestamptz,
            CHECK ((role = 'PLATFORM_OPERATOR') = (fpo_id IS NULL)),
            CHECK (role = 'STAFF' OR cardinality(extra_capabilities) = 0)
        )
        """
    )
    op.execute("CREATE INDEX users_fpo ON users (fpo_id)")
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON users USING ({TENANT}) WITH CHECK ({TENANT})")
    op.execute(
        "GRANT SELECT (id, fpo_id, username, display_name, role, extra_capabilities, language, active, "
        "must_change_password, origin, created_at, password_changed_at) ON users TO kisaniti_app"
    )
    op.execute(
        "GRANT INSERT (id, fpo_id, username, display_name, role, extra_capabilities, language, active, "
        "must_change_password, password_hash, origin) ON users TO kisaniti_app"
    )
    op.execute(
        "GRANT UPDATE (active, must_change_password, password_hash, password_changed_at) ON users TO kisaniti_app"
    )

    op.execute(
        r"""
        CREATE TABLE sessions (
            token_sha256 text PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
            user_id      text NOT NULL REFERENCES users (id),
            csrf_token   text NOT NULL CHECK (length(csrf_token) >= 32),
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            expires_at   timestamptz NOT NULL,
            revoked_at   timestamptz
        )
        """
    )
    op.execute("CREATE INDEX sessions_user ON sessions (user_id)")

    op.execute(
        r"""
        CREATE TABLE login_attempts (
            key_sha256    text PRIMARY KEY CHECK (key_sha256 ~ '^[0-9a-f]{64}$'),
            window_start  timestamptz NOT NULL,
            failures      integer NOT NULL CHECK (failures >= 0),
            blocked_until timestamptz
        )
        """
    )

    events = (
        "'LOGIN_SUCCESS','LOGIN_FAILURE','LOGIN_THROTTLED','LOGOUT','SESSION_EXPIRED','PASSWORD_CHANGED',"
        "'PASSWORD_RESET','USER_CREATED','USER_DEACTIVATED','LANGUAGE_CHANGED','ACCESS_DENIED',"
        "'CROSS_TENANT_REFERENCE'"
    )
    op.execute(
        f"""
        CREATE TABLE audit_log (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            occurred_at     timestamptz NOT NULL DEFAULT now(),
            request_id      text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 64),
            event           text NOT NULL CHECK (event IN ({events})),
            actor_user_id   text REFERENCES users (id),
            fpo_id          text REFERENCES fpo (id),
            subject_user_id text REFERENCES users (id)
        )
        """
    )
    op.execute("CREATE INDEX audit_log_fpo_time ON audit_log (fpo_id, occurred_at)")
    op.execute(
        r"""
        CREATE FUNCTION audit_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()"
    )
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON audit_log USING ({TENANT}) WITH CHECK ({TENANT})")
    op.execute("GRANT SELECT, INSERT ON audit_log TO kisaniti_app")
    op.execute("GRANT USAGE ON SEQUENCE audit_log_id_seq TO kisaniti_app")


def downgrade() -> None:
    op.execute("DROP TABLE audit_log")
    op.execute("DROP FUNCTION audit_log_append_only()")
    op.execute("DROP TABLE login_attempts")
    op.execute("DROP TABLE sessions")
    op.execute("DROP TABLE users")
