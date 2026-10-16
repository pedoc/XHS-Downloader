from argparse import ArgumentParser
from asyncio import run
from asyncio.exceptions import CancelledError
from contextlib import suppress
from sys import argv

from source import XHS, Settings, XHSDownloader, cli
from source.GUI import launch


async def tui():
    async with XHSDownloader() as xhs:
        await xhs.run_async()


async def api_server(
    host="127.0.0.1",
    port=5556,
    log_level="info",
):
    async with XHS(**Settings().run()) as xhs:
        await xhs.run_api_server(
            host,
            port,
            log_level,
        )


async def mcp_server(
    transport="streamable-http",
    host="127.0.0.1",
    port=5556,
    log_level="INFO",
):
    async with XHS(**Settings().run()) as xhs:
        await xhs.run_mcp_server(
            transport=transport,
            host=host,
            port=port,
            log_level=log_level,
        )


def server_arguments() -> dict[str, str | int]:
    parser = ArgumentParser(prog=f"{argv[0]} {argv[1]}")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5556)
    arguments = parser.parse_args(argv[2:])
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return vars(arguments)


if __name__ == "__main__":
    with suppress(
        KeyboardInterrupt,
        CancelledError,
    ):
        if len(argv) == 1:
            launch()
        elif argv[1].upper() == "TUI":
            run(tui())
        elif argv[1].upper() == "API":
            run(api_server(**server_arguments()))
        elif argv[1].upper() == "MCP":
            run(mcp_server(**server_arguments()))
            # run(mcp_server("stdio"))
        else:
            cli()
