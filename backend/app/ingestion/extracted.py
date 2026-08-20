from dataclasses import dataclass, field


@dataclass
class ExtractedTable:
    page_number: int | None
    rows: list[list[str]]

    def as_markdown(self) -> str:
        if not self.rows:
            return ""
        lines = []
        header = self.rows[0]
        lines.append("| " + " | ".join(c or "" for c in header) + " |")
        lines.append("| " + " | ".join("---" for _ in header) + " |")
        for row in self.rows[1:]:
            lines.append("| " + " | ".join(c or "" for c in row) + " |")
        return "\n".join(lines)


@dataclass
class ExtractedPage:
    page_number: int
    text: str
    headings: list[tuple[str, int]] = field(default_factory=list)  # (text, font_rank) largest font = rank 0
    tables: list[ExtractedTable] = field(default_factory=list)
    char_count: int = 0

    def __post_init__(self):
        self.char_count = len(self.text)


@dataclass
class ExtractedDocument:
    status: str            # ok | partial | unsupported | failed
    reason: str | None
    pages: list[ExtractedPage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def total_chars(self) -> int:
        return sum(p.char_count for p in self.pages)
