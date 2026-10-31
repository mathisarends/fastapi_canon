# Docker

Keep Docker files close to the application they describe. For a single-package
service, use a small root-level layout:

```text
.
├── .dockerignore
├── compose.yml
└── Dockerfile
```

For a workspace with independently deployed processes, keep Compose at the
repository root and place each Dockerfile beside its package:

```text
.
├── .dockerignore
├── compose.yml
├── api/
│   └── Dockerfile
├── worker/
│   └── Dockerfile
├── pyproject.toml
└── uv.lock
```

`compose.yml` describes the services used together. Each Dockerfile describes
one deployable process. Add further Docker files only when the project needs
them.
