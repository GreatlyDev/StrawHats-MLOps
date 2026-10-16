"""Preview one owner Iris sizing decision; --apply permits one guarded change."""

from .runtime_tools import command_main


def main(argv: list[str] | None = None) -> int:
    return command_main("adjust", argv)


if __name__ == "__main__":
    raise SystemExit(main())
