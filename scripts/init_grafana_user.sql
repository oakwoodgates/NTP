-- Run once after `alembic upgrade head` to create a read-only Grafana user.
-- Then update grafana/provisioning/datasources/postgres.yml to use grafana_ro.
--
-- Usage:
--   psql -U "${POSTGRES_USER:-nautilus}" -d "${POSTGRES_DB:-nautilus_platform}" \
--     -v grafana_pw="${GRAFANA_RO_PASSWORD:-changeme}" \
--     -v postgres_db="${POSTGRES_DB:-nautilus_platform}" \
--     -f scripts/init_grafana_user.sql

CREATE USER grafana_ro WITH PASSWORD :'grafana_pw';
GRANT CONNECT ON DATABASE :"postgres_db" TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
