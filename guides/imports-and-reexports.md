# Imports and Re-exports

Treat every package's `__init__.py` as an intentional public facade. Re-exports reduce coupling only when they create a small, stable vocabulary; indiscriminate re-exports hide ownership and create import cycles.

For practical facades, see the reference task [`domain/__init__.py`](../examples/reference-service/src/app/features/tasks/domain/__init__.py) and [`application/__init__.py`](../examples/reference-service/src/app/features/tasks/application/__init__.py). Its feature root [`__init__.py`](../examples/reference-service/src/app/features/tasks/__init__.py) demonstrates when leaving the facade empty is safer.

## What to re-export

Re-export a symbol when all of these are true:

- consumers in other packages or layers are expected to use it;
- it is part of the package's supported vocabulary rather than an implementation detail;
- its ownership remains understandable from the facade name; and
- keeping its import path stable is useful when internal modules move.

Typical public symbols are domain entities and owned ports, application services/results/exceptions, and deliberately shared presentation or infrastructure mechanisms.

Do not re-export ORM table models, concrete adapters, provider helper functions, private mappers, router handler functions, vendor SDK types, or every name found in a module. An empty `__init__.py` is correct when a package has no public facade.

## How to define a facade

Use explicit relative imports and an explicit `__all__` inside `__init__.py`:

```python
from .entities import Task
from .repository import TaskRepository

__all__ = ["Task", "TaskRepository"]
```

Relative imports are reserved for package facades. Use absolute imports in
regular modules so dependencies and ownership remain visible. Never use
`from .module import *` or build `__all__` dynamically. Keep a facade shallow:
do not pass a symbol through several unrelated `__init__.py` files. The package
that owns the concept exports it.

Importing the public facade must be cheap and side-effect free. It must not connect to a database, read required secrets, create the FastAPI app, or eagerly import optional integrations. If adding a re-export creates a cycle, fix the dependency direction or import from the defining module internally; do not hide the cycle with a function-local import unless deferred loading is the actual requirement.

## How consumers import

Code outside the owning subpackage imports its supported facade:

```python
from api.features.tasks.domain import Task, TaskRepository
from api.features.tasks.application import TaskNotFound, TaskService
```

Code inside the same subpackage imports sibling definition modules directly when that makes ownership clearer. Do not make internal modules call back through their own package facade.

A concrete implementation of an application-owned ABC is the deliberate exception to facade-first consumption: import the ABC from its definition module and inherit it explicitly.

```python
from api.features.tasks.domain.repository import TaskRepository


class SqlTaskRepository(SqlRepository[TaskModel, Task], TaskRepository): ...
```

This preserves nominal conformance and gives language servers the most direct definition/implementation navigation. Other consumers may continue to import `TaskRepository` from the domain facade. See [ABCs for owned ports](architecture.md#abcs-for-owned-ports).

## Feature assembly imports

Python executes a parent package's `__init__.py` before importing any child. Re-exporting a feature descriptor from the feature root would therefore load routers, providers, FastAPI, and infrastructure even when a consumer asks only for `features.tasks.domain`. Keep the feature-root `__init__.py` empty and import the dedicated assembly module directly:

```python
from api.features.tasks.feature import feature as tasks_feature
```

`feature.py` is the public composition surface; routers, concrete providers, and exception registration remain private behind its descriptor. Re-export from a feature root only when the feature has no inward subpackages whose lightweight import would be compromised.

## Imports between workspace packages

Each workspace member is a real distribution boundary. Declare it as a dependency and import from its documented package facade:

```toml
[project]
dependencies = ["contracts"]

[tool.uv.sources]
contracts = { workspace = true }
```

```python
from contracts import TaskCreated
```

Never import through another member's filesystem layout such as `services.worker.src...`, reach into a private `_module`, or depend on a transitive workspace member without declaring it. If consumers repeatedly need an internal symbol, either promote it deliberately to the owner's facade or reconsider the package boundary.

For workspace layout and dependency rules, see [uv workspace](uv-workspace.md).

## Compatibility and typing

Changing a re-exported name or its semantics is a public API change for its consumers. Keep aliases only for an intentional deprecation window; do not accumulate compatibility aliases indefinitely.

Use `if TYPE_CHECKING` only for imports needed exclusively by static analysis. It must not conceal a runtime dependency required for object construction or validation. Prefer forward annotations and corrected dependency direction over pervasive conditional imports.
