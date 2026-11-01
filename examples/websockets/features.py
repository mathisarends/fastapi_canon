from fastapi_canon.integrations.websockets import RpcChannel, RpcModel, RpcService


class StatusResult(RpcModel):
    status: str


class ProfileResult(RpcModel):
    name: str


system = RpcChannel("system")
account = RpcChannel("account")


@system.method()
async def status() -> StatusResult:
    return StatusResult(status="ready")


@account.method(summary="Return the current profile.")
async def profile() -> ProfileResult:
    return ProfileResult(name="Mathis")


app = RpcService(version=2)
app.socket("/rpc", channels=(system, account))


def main() -> None:
    protocol = app.freeze()
    print(f"Protocol version: {protocol.version}")
    for method in protocol.methods:
        print(method.name)


if __name__ == "__main__":
    main()
