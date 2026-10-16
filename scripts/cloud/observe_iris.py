"""Observe only the explicitly selected, verified owner Iris runtime."""

from .runtime_tools import command_main


def main(argv: list[str] | None = None) -> int:
    return command_main("observe", argv)


if __name__ == "__main__":
    raise SystemExit(main())
