"""Command line: contact-scraper input.xlsx -o output.xlsx"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import phonenumbers
import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from .excel_io import InputError, read_sheet, write_results
from .models import Settings
from .runner import run

app = typer.Typer(add_completion=False, help="Find phone numbers and emails for the websites in an Excel file.")
console = Console()


@app.command()
def main(
    input_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Excel file (.xlsx) with a website column."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Where to save results (default: <input>_contacts.xlsx)."),
    url_column: Optional[str] = typer.Option(None, "--url-column", "-c", help="Name of the website column (auto-detected if omitted)."),
    region: Optional[str] = typer.Option(None, "--region", "-r", help="Fallback country for phone numbers when a site gives no clue, e.g. US, IN, GB."),
    concurrency: Optional[int] = typer.Option(None, help="Websites checked at the same time."),
    max_pages: Optional[int] = typer.Option(None, help="Max pages opened per website."),
    browser: Optional[bool] = typer.Option(None, "--browser/--no-browser", help="Use headless browser for JS-heavy sites."),
    ignore_robots: bool = typer.Option(False, "--ignore-robots", help="Don't check robots.txt."),
    analytics: bool = typer.Option(True, "--analytics/--simple", help="--simple: quicker check that opens extra pages only until an email or mobile is found, and saves only businesses with one (name, state, country, emails, mobiles)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(message)s")
    settings = Settings.from_env()
    if region:
        settings.region = region.upper()
    if settings.region not in phonenumbers.SUPPORTED_REGIONS:
        raise typer.BadParameter(f"Unknown country code '{settings.region}'. Use a 2-letter code like US, IN, GB.")
    if concurrency:
        settings.concurrency = concurrency
    if max_pages:
        settings.max_pages = max_pages
    if browser is not None:
        settings.use_browser = browser
    if ignore_robots:
        settings.respect_robots = False
    settings.analytics = analytics

    output = output or input_file.with_name(f"{input_file.stem}_contacts.xlsx")

    try:
        sheet = read_sheet(input_file, url_column)
    except InputError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    urls = sheet.urls
    console.print(f"Reading websites from column [bold]{sheet.headers[sheet.url_column]}[/bold] "
                  f"({sum(1 for u in urls if u)} websites, region {settings.region})")

    with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(),
                  MofNCompleteColumn(), TimeElapsedColumn(), console=console) as progress:
        task = progress.add_task("Checking websites", total=len(set(u for u in urls if u)))
        results = asyncio.run(run(urls, settings, lambda done, total, _r: progress.update(task, completed=done)))

    write_results(output, sheet, results, analytics)
    ok = sum(r.status == "ok" for r in results)
    errors = sum(r.status.startswith("error") for r in results)
    console.print(f"[green]Done.[/green] Contacts found for {ok} of {len(results)} rows ({errors} errors).")
    console.print(f"Saved to [bold]{output}[/bold]")


if __name__ == "__main__":
    app()
