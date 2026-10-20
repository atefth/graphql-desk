"""Module entrypoint; importing it does not dispatch a command."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
