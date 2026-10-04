#!/bin/sh
set -eu
# psql quoting protects even passwords containing SQL syntax. Never enable shell tracing.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 --set=app_password="$APP_DB_PASSWORD" <<'SQL'
CREATE ROLE opspilot_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'app_password';
GRANT CONNECT ON DATABASE opspilot TO opspilot_app;
GRANT USAGE ON SCHEMA public TO opspilot_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
