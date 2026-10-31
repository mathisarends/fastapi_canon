import asyncio
from dataclasses import dataclass

from fastapi_canon.integrations.websockets import Inject, RpcChannel, RpcModel


class GreetParams(RpcModel):
    name: str


class Greeting(RpcModel):
    text: str


@dataclass(frozen=True)
class Greeter:
    salutation: str


router = RpcChannel("greeting")


@router.method()
async def say(params: GreetParams, greeter: Inject[Greeter]) -> Greeting:
    return Greeting(text=f"{greeter.salutation}, {params.name}!")


async def main() -> None:
    server = router.create_server(context=Greeter("Hello"))
    response = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "greeting.say",
            "params": {"name": "World"},
        }
    )
    print(response)


if __name__ == "__main__":
    asyncio.run(main())
