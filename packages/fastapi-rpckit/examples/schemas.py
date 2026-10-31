import json

from fastapi_canon.integrations.websockets import RpcChannel, RpcModel, RpcService
from fastapi_canon.integrations.websockets.schema import render_openrpc


class SearchParams(RpcModel):
    project_id: str
    query: str
    max_results: int = 10


class SearchResult(RpcModel):
    items: list[str]
    next_page_token: str | None = None


router = RpcChannel("search")


@router.method()
async def run(params: SearchParams) -> SearchResult:
    return SearchResult(items=[])


app = RpcService(version=3)
app.socket("/rpc", channels=(router,))


def main() -> None:
    contract = app.contract(title="Search API", base_url="ws://localhost:8000")
    openrpc = render_openrpc(
        contract.protocol,
        title=contract.title,
        servers=contract.servers,
    )
    print("OpenRPC method:")
    print(json.dumps(openrpc["methods"][0], indent=2))


if __name__ == "__main__":
    main()
