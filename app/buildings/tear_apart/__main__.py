"""Allow `python -m app.buildings.tear_apart ...` to invoke the CLI."""

from app.buildings.tear_apart.cli import main


if __name__ == "__main__":
    main()
