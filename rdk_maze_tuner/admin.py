"""Security-sensitive administration commands."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Callable, TextIO

from rdk_maze_tuner.platform.auth import (
    AuthService,
    PasswordPolicyError,
    UsernameExistsError,
    UsernamePolicyError,
)
from rdk_maze_tuner.platform.config import PlatformConfig
from rdk_maze_tuner.platform.database import Database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Maze platform administration")
    commands = parser.add_subparsers(dest="command", required=True)
    create_user = commands.add_parser(
        "create-user",
        help="Create a website user using a password read from the TTY",
    )
    create_user.add_argument("--username", help="Website username")
    create_user.add_argument(
        "--data-dir",
        type=Path,
        help="Override MAZE_DATA_DIR for this command",
    )
    return parser


def run_create_user(
    args: argparse.Namespace,
    *,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    input_fn: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
) -> int:
    username = args.username or input_fn("Username: ")
    first = getpass_fn("Password: ")
    second = getpass_fn("Confirm password: ")
    if first != second:
        raise PasswordPolicyError("password confirmation does not match")

    config = (
        PlatformConfig(data_dir=Path(args.data_dir))
        if args.data_dir is not None
        else PlatformConfig.from_env()
    )
    config.ensure_directories()
    database = Database(config.database_path)
    database.initialize()
    user = AuthService(database=database).create_user(username, first)
    print(
        f"Created user {user.username} ({user.user_id})",
        file=output,
    )
    return 0


def run(args: argparse.Namespace) -> int:
    if args.command == "create-user":
        return run_create_user(args)
    raise ValueError(f"unsupported command: {args.command}")


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (
        PasswordPolicyError,
        UsernameExistsError,
        UsernamePolicyError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
