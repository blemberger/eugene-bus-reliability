# Migrations

`sql/schema/` builds a new database from scratch and is always the authoritative
definition of the tables. When a change has to reach a database that already exists,
it goes into `sql/schema/` **and** into a migration here: a SQL file named
`NNN_description.sql` that brings an existing database to the same state.

Every migration must be safe to run more than once (`ADD COLUMN IF NOT EXISTS`,
`CREATE INDEX IF NOT EXISTS`, ...), because `make migrate` simply applies all of them,
in order, to the collection database (and to the test database if it is running),
then sets the read-only user's password from `READER_PASSWORD`. On a database built
from the current schema they change nothing.