# SQLite runtime migrations

The service applies these numbered application migrations in order and records their checksums in `schema_migrations`. Released migrations are immutable. Connections enable `PRAGMA foreign_keys=ON`.

Back up an existing database before upgrading. Keep the database and its SQLite sidecars together when making an administrator-managed backup or relocation.
