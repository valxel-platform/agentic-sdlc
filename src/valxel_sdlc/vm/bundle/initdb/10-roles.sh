#!/bin/sh
# First start only. The tool's login owns its database but is no superuser and can't bypass
# row-level security (VALXEL_TENANCY=rls); the audit sink gets a database and login of its own.
set -eu
tool_pw=$(cat /run/secrets/VALXEL_DB_PASSWORD)
audit_pw=$(cat /run/secrets/AUDIT_DB_PASSWORD)
psql -v ON_ERROR_STOP=1 --username postgres <<SQL
CREATE ROLE valxel LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB PASSWORD '${tool_pw}';
CREATE DATABASE valxel OWNER valxel;
CREATE ROLE audit LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB PASSWORD '${audit_pw}';
CREATE DATABASE audit OWNER audit;
SQL
