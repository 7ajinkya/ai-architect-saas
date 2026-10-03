import time
from enum import Enum

from rich.console import ConsoleRenderable, RenderResult
from rich.live import Live
from rich.table import Table
from rich.text import Text


class ProgressState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETE = "complete"


class RequirementProgress(ConsoleRenderable):
    """Live terminal progress indicator for the requirements workflow."""

    def __init__(self) -> None:
        self.steps = [
            ("Extracting", "requirements"),
            ("Validating", "requirements"),
            ("Requirements", "ready"),
        ]

        self.states = [
            ProgressState.PENDING,
            ProgressState.PENDING,
            ProgressState.PENDING,
        ]

        self.live = Live(
            self,
            refresh_per_second=4,
        )

    def start(self) -> None:
        self.live.start()

    def stop(self) -> None:
        self.live.stop()

    def activate(self, index: int) -> None:
        self.states[index] = ProgressState.ACTIVE
        self.live.update(self)

    def complete(self, index: int) -> None:
        self.states[index] = ProgressState.COMPLETE
        self.live.update(self)

    def __rich_console__(self, console, options):
        table = Table.grid(
            padding=(0, 1),
            expand=False,
        )

        table.add_column(justify="center", width=16)
        table.add_column(justify="center", width=8)
        table.add_column(justify="center", width=16)
        table.add_column(justify="center", width=8)
        table.add_column(justify="center", width=16)

        symbols = []
        labels = []

        for (line1, line2), state in zip(self.steps, self.states):
            if state == ProgressState.PENDING:
                symbol = Text("○", style="dim")
                label_style = "dim"

            elif state == ProgressState.ACTIVE:
                if int(time.time() * 2) % 2 == 0:
                    symbol = Text("●", style="bold yellow")
                else:
                    symbol = Text("○", style="bold yellow")

                label_style = "bold yellow"

            else:
                symbol = Text("✓", style="bold green")
                label_style = "bold green"

            label = Text()
            label.append(line1, style=label_style)
            label.append("\n")
            label.append(line2, style=label_style)

            symbols.append(symbol)
            labels.append(label)

        connector = Text("────────", style="dim")

        table.add_row(
            symbols[0],
            connector,
            symbols[1],
            connector,
            symbols[2],
        )

        table.add_row(
            labels[0],
            "",
            labels[1],
            "",
            labels[2],
        )

        yield table
