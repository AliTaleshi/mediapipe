"""Bootstrap helper: ``python -m app.cli create-project "My App"`` prints a new API key."""

import argparse
import asyncio

from app.db.session import get_sessionmaker
from app.services.auth import create_api_key, create_project


async def _create_project(name: str) -> None:
    async with get_sessionmaker()() as session:
        project = await create_project(session, name)
        _, key = await create_api_key(session, project.id, name="default")
        await session.commit()
    print(f"project_id: {project.id}\napi_key:    {key}")
    print("Store the key now; it cannot be shown again.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="mediapipe")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-project", help="create a project and its first API key")
    create.add_argument("name")
    args = parser.parse_args()
    if args.command == "create-project":
        asyncio.run(_create_project(args.name))


if __name__ == "__main__":
    main()
