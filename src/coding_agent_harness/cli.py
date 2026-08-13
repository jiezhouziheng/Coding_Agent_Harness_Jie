import typer

app = typer.Typer(no_args_is_help=True)

sessions_app = typer.Typer(no_args_is_help=True)
approvals_app = typer.Typer(no_args_is_help=True)
changes_app = typer.Typer(no_args_is_help=True)
credentials_app = typer.Typer(no_args_is_help=True)
memory_app = typer.Typer(no_args_is_help=True)
report_app = typer.Typer(no_args_is_help=True)
demo_app = typer.Typer(no_args_is_help=True)


@app.command()
def run() -> None:
    """Run the coding agent harness."""


@sessions_app.callback()
def sessions() -> None:
    """Manage sessions."""


@approvals_app.callback()
def approvals() -> None:
    """Manage approvals."""


@changes_app.callback()
def changes() -> None:
    """Inspect changes."""


@credentials_app.callback()
def credentials() -> None:
    """Manage credentials."""


@memory_app.callback()
def memory() -> None:
    """Manage memory."""


@report_app.callback()
def report() -> None:
    """Generate reports."""


@demo_app.callback()
def demo() -> None:
    """Run demonstrations."""


app.add_typer(sessions_app, name="sessions")
app.add_typer(approvals_app, name="approvals")
app.add_typer(changes_app, name="changes")
app.add_typer(credentials_app, name="credentials")
app.add_typer(memory_app, name="memory")
app.add_typer(report_app, name="report")
app.add_typer(demo_app, name="demo")
