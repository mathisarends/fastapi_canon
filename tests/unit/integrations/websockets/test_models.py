import pytest
from fastapi_canon.integrations.websockets import (
    Inject,
    ProtocolDefinitionError,
    RpcChannel,
    RpcModel,
)
from fastapi_canon.integrations.websockets.schema import render_openrpc
from pydantic import ValidationError


class NavigateParams(RpcModel):
    project_id: str
    ignore_cache: bool


class NavigateResult(RpcModel):
    active_project_id: str


navigation_channel = RpcChannel("navigation", namespace="browser.nav")


class Navigation:
    def __init__(self) -> None:
        self.params: NavigateParams | None = None


@navigation_channel.method("navigate")
async def navigate(
    params: NavigateParams,
    navigation: Inject[Navigation],
) -> NavigateResult:
    navigation.params = params
    return NavigateResult(active_project_id=params.project_id)


navigation_app = navigation_channel


def test_rpc_models_use_snake_case_in_python_and_camel_case_on_the_wire() -> None:
    by_name = NavigateParams(project_id="p1", ignore_cache=True)
    by_alias = NavigateParams.model_validate({"projectId": "p2", "ignoreCache": False})

    assert by_name.project_id == "p1"
    assert by_alias.project_id == "p2"
    assert by_name.model_dump() == {"projectId": "p1", "ignoreCache": True}
    with pytest.raises(ValidationError):
        NavigateParams(project_id="p1", ignore_cache=True, unexpected=True)


def test_openrpc_uses_the_canonical_wire_field_names() -> None:
    document = render_openrpc(navigation_app.protocol, title="Navigation")
    method = document["methods"][0]

    assert method["params"] == [
        {
            "name": "projectId",
            "required": True,
            "schema": {"title": "Projectid", "type": "string"},
        },
        {
            "name": "ignoreCache",
            "required": True,
            "schema": {"title": "Ignorecache", "type": "boolean"},
        },
    ]
    assert set(document["components"]["schemas"]["NavigateResult"]["properties"]) == {
        "activeProjectId"
    }


async def test_server_accepts_camel_case_and_serializes_results_with_aliases() -> None:
    handler = Navigation()

    class Resolver:
        async def resolve(self, dependency: type[Navigation]) -> Navigation:
            return handler

    response = await navigation_app.create_server(resolver=Resolver()).handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "browser.nav.navigate",
            "params": {"projectId": "p1", "ignoreCache": True},
        }
    )

    assert handler.params == NavigateParams(project_id="p1", ignore_cache=True)
    assert response is not None
    assert response.model_dump(mode="json")["result"] == {"activeProjectId": "p1"}


def test_contract_rejects_colliding_wire_field_names() -> None:
    class CollidingParams(RpcModel):
        foo_bar: str
        fooBar: str

    router = RpcChannel("collision")

    @router.method("test")
    async def test(params: CollidingParams) -> None: ...

    with pytest.raises(ProtocolDefinitionError, match="wire field 'fooBar'"):
        render_openrpc(router.protocol, title="Collision")
