"""Run the bounded warning demonstration on one verified owner runtime."""

from .runtime_tools import command_main


def main(argv: list[str] | None = None) -> int:
    return command_main("traffic", argv)


if __name__ == "__main__":
    raise SystemExit(main())
