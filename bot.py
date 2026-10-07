"""Safe entry point for the single-contact tutoring demo.

Importing this module never connects to WeChat or starts a worker.
The upstream full-feature bot is retained in legacy_bot.py.
"""


def main():
    from tutoring_demo.cli import main as demo_main
    return demo_main()


if __name__ == "__main__":
    raise SystemExit(main())
