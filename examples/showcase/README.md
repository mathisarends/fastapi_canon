# fastapi-canon showcase

This small application demonstrates the recommended application boundary:
each feature owns its router and its domain-error contracts, while `app.py`
is the only composition root. Its layout is deliberately suitable as a starting
point for a real service:

```text
showcase/
  app.py                         # composition root
  features/
    catalog/
      __init__.py                # public `feature` export
      router.py                  # HTTP layer
      models.py                  # request and response models
      exceptions.py              # domain exceptions, without HTTP concerns
      errors.py                  # Error / ErrorRegistry contracts
      service.py                 # framework-independent use cases
      providers.py               # Dishka bindings contributed by the feature
    status/
      __init__.py
      router.py
```

Run it from the repository root:

```console
uv run --with uvicorn uvicorn examples.showcase.app:app --reload
```

Then open [the API documentation](http://127.0.0.1:8000/docs), or try the
following requests:

```console
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/products/coffee
curl http://127.0.0.1:8000/products/missing
curl -X POST http://127.0.0.1:8000/products/tea/reservations -H "content-type: application/json" -d '{"quantity": 1}'
curl -X POST http://127.0.0.1:8000/products/coffee/reservations -H "content-type: application/json" -d '{"quantity": 0}'
```

The last three calls show the three error classes handled by the shared error
engine:

| Situation | Result |
| --- | --- |
| Unknown product | Feature-defined `404 product_not_found` problem |
| Insufficient stock | Feature-defined `409 product_unavailable` problem, including typed extension members |
| Invalid request body | Built-in normalized `422 request_validation_error` problem |

`catalog/errors.py` deliberately has no application-wide problem-type URL. Its
`ErrorRegistry` is local to the feature. `Composition` merges that registry
and `ErrorOptions(type_base=...)` resolves every feature error type to the
same public namespace. The `responses=errors.responses(...)` declarations use
the very same contracts as runtime handling, so `/openapi.json` documents the
`404` and `409` responses without duplicating schemas.

The router only validates HTTP input and calls `CatalogService`; it receives
that service through Dishka. `catalog/__init__.py` contributes the provider,
router, and error registry as one `Feature`, so removing the feature removes
all of those pieces together.

To turn a feature off, remove `catalog_feature` from `Composition` in `app.py`.
Its routes and error definitions disappear together.
