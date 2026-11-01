from fastapi import FastAPI

from examples.showcase.features.catalog import feature as catalog_feature
from examples.showcase.features.status import feature as status_feature
from fastapi_canon import Composition, ErrorOptions

composition = Composition(
    status_feature,
    catalog_feature,
    errors=ErrorOptions(type_base="https://api.example.test/problems"),
)

app = composition.apply(
    FastAPI(
        title="fastapi-canon showcase",
        version="0.1.0",
        description="Feature composition with RFC 9457 Problem Details.",
    )
)
