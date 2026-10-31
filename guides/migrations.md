# Database migrations

Use Alembic as the schema history for a persistent relational database. Keep
one migration environment per independently deployed schema, owned by the
service that owns that schema.

```text
api/
├── alembic.ini
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
├── pyproject.toml
└── src/api/
```

Features that share a database schema also share this migration history; do
not create a migration directory per feature. The Alembic environment imports
the schema's table metadata through one explicit registration point.

Commit revisions with the application change they support. Application startup
does not create or infer a persistent schema with
`SQLModel.metadata.create_all()`. Disposable examples may use a clearly named
bootstrap helper.

For table placement and metadata registration, see [Database](database.md#table-placement-and-registration).
