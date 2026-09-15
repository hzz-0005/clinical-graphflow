#!/bin/sh
set -eu
: "${POSTGRES_READER_PASSWORD:?POSTGRES_READER_PASSWORD must be set}"
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set=ON_ERROR_STOP=1 --set=reader_password="$POSTGRES_READER_PASSWORD" <<'SQL'
ALTER ROLE insightflow_reader PASSWORD :'reader_password';
SQL

