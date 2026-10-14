"""Terminal reader (Textual): front page -> section -> note, with vim-style keys."""

from __future__ import annotations

import webbrowser
from typing import Any

from rich.text import Text
from sqlalchemy.orm import Session
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Markdown, Static

from ..config.models import NewspaperConfig
from ..dates import long_date
from ..edition.today import local_today, today_banner
from ..storage import queries
from ..storage.database import open_db
from ..storage.models import Article, Edition, EditionStatus, Note

FLAG_LABELS = {"read": "Leído", "favorite": "Favorito", "saved": "Guardado"}


class Entry(ListItem):
    """A list row that knows what it points to: note, article, section or edition."""

    def __init__(self, kind: str, ref: Any, text: Text) -> None:
        super().__init__(Label(text))
        self.kind, self.ref = kind, ref


def marks(item: Note | Article) -> str:
    return ("★ " if item.is_favorite else "") + ("◆ " if item.is_saved else "")


def interest_label(note: Note) -> str | None:
    if note.interest is None:
        return None
    return f"{'te interesa' if note.interest_liked else 'menos interés'}: {note.interest}"


def note_text(note: Note) -> Text:
    outlets = len({a.source_name for a in note.articles})
    meta = [note.section, f"{outlets} {'fuente' if outlets == 1 else 'fuentes'}"]
    meta.append(f"{note.read_minutes} min")
    meta += [tag for tag, on in (
        ("fuente única", note.single_source),
        ("basada en extracto", note.excerpt_only),
        ("sin redactar", not note.drafted),
        (interest_label(note), note.interest is not None),
    ) if on]  # fmt: skip
    style = "dim" if note.is_read else "bold"
    return Text.assemble((marks(note) + note.headline, style), "\n", (" · ".join(meta), "dim"))


def article_text(article: Article) -> Text:
    style = "dim" if article.is_read else ""
    return Text.assemble(
        (marks(article) + article.title, style), "\n", (article.source_name, "dim")
    )


class VimList(ListView):
    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("l", "select_cursor", show=False),
    ]


class VimScroll(VerticalScroll):
    BINDINGS = [
        Binding("j", "scroll_down", show=False),
        Binding("k", "scroll_up", show=False),
    ]


class ReaderScreen(Screen):
    BINDINGS = [
        Binding("h,escape", "back", "Volver"),
        Binding("m", "flag('read')", "Leído"),
        Binding("f", "flag('favorite')", "Favorito"),
        Binding("s", "flag('saved')", "Guardar"),
        Binding("o", "open_original", "Original"),
    ]

    @property
    def reader(self) -> NewspaperApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Header()
        yield from self.compose_body()
        yield Footer()

    def compose_body(self) -> ComposeResult:
        yield from ()

    def highlighted(self) -> Entry | None:
        focused = self.focused
        if isinstance(focused, ListView) and isinstance(focused.highlighted_child, Entry):
            return focused.highlighted_child
        return None

    def target(self) -> tuple[str, int] | None:
        """The note or article that m/f/s act on."""
        entry = self.highlighted()
        return (entry.kind, entry.ref) if entry and entry.kind in ("note", "article") else None

    def action_back(self) -> None:
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()

    def action_flag(self, flag: str) -> None:
        target = self.target()
        if target is None:
            return
        with self.reader.session() as s, s.begin():
            value = queries.set_flag(s, target[0], target[1], flag)
            self.show_flags(s, *target)
        self.notify(f"{FLAG_LABELS[flag]}: {'sí' if value else 'no'}")

    def show_flags(self, s: Session, kind: str, ref: int) -> None:
        """Redraw the highlighted row so a toggled flag shows at once."""
        entry = self.highlighted()
        if entry and (entry.kind, entry.ref) == (kind, ref):
            item = s.get(Note, ref) if kind == "note" else s.get(Article, ref)
            entry.query_one(Label).update(note_text(item) if kind == "note" else article_text(item))

    def action_open_original(self) -> None:
        target = self.target()
        if target is None:
            return
        kind, ref = target
        if kind == "note":  # a listed note opens its main source
            with self.reader.session() as s:
                ref = queries.get_note(s, ref).articles[0].id
        self.reader.open_article(ref)

    def open(self, kind: str, ref: Any) -> None:
        if kind == "note":
            self.app.push_screen(NoteScreen(ref))
        elif kind == "edition":
            self.app.push_screen(EditionScreen(ref))
        elif kind == "article":
            self.reader.open_article(ref)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, Entry):
            self.open(event.item.kind, event.item.ref)


class EditionScreen(ReaderScreen):
    """Front page of one edition (the latest ready one when edition_id is None)."""

    def __init__(self, edition_id: int | None) -> None:
        super().__init__()
        self.edition_id = edition_id

    def compose_body(self) -> ComposeResult:
        banner = self.reader.banner_text()
        if banner:
            yield Static(banner, id="banner")
        with self.reader.session() as s:
            edition = (
                s.get(Edition, self.edition_id)
                if self.edition_id
                else queries.latest_ready_edition(s)
            )
            if edition is None:
                items = None
            else:
                self.edition_id = edition.id
                self.sub_title = long_date(edition.edition_date)
                items = [
                    Entry("note", n.id, note_text(n)) for n in queries.front_page(s, edition.id)
                ]
                items += [
                    Entry(
                        "section", name, Text.assemble((name.upper(), "bold"), f"  {count} notas")
                    )
                    for name, count in queries.section_counts(s, edition.id)
                ]
        if items is None:
            yield Static(
                "Todavía no hay ediciones.\n\n"
                "La primera se prepara a la hora configurada, o ejecuta `newspaper generate`.",
                id="empty",
            )
        else:
            yield VimList(*items)

    def open(self, kind: str, ref: Any) -> None:
        if kind == "section":
            self.app.push_screen(SectionScreen(self.edition_id, ref))
        else:
            super().open(kind, ref)


class SectionScreen(ReaderScreen):
    def __init__(self, edition_id: int, section: str) -> None:
        super().__init__()
        self.edition_id, self.section = edition_id, section

    def compose_body(self) -> ComposeResult:
        self.sub_title = self.section
        with self.reader.session() as s:
            notes = queries.section_notes(s, self.edition_id, self.section)
            items = [Entry("note", n.id, note_text(n)) for n in notes]
        yield VimList(*items)


class NoteScreen(ReaderScreen):
    AUTO_FOCUS = "#body"

    def __init__(self, note_id: int) -> None:
        super().__init__()
        self.note_id = note_id

    def compose_body(self) -> ComposeResult:
        with self.reader.session() as s, s.begin():
            note = queries.get_note(s, self.note_id)
            note.is_read = True
            self.show_flags(s, "note", self.note_id)
            parts = [f"# {note.headline}"]
            if note.dek:
                parts.append(f"*{note.dek}*")
            parts.append(note.body)
            if note.differences:
                parts.append(f"## Dónde difieren\n\n{note.differences}")
            sources = [Entry("article", a.id, article_text(a)) for a in note.articles]
        with VimScroll(id="body"):
            yield Markdown("\n\n".join(parts))
        yield Label("Fuentes · Tab para elegir · o abre el original", classes="caption")
        yield VimList(*sources, id="sources")

    def target(self) -> tuple[str, int] | None:
        if self.focused is self.query_one("#sources"):
            return super().target()
        return ("note", self.note_id)

    def show_flags(self, s: Session, kind: str, ref: int) -> None:
        if kind == "article":
            return super().show_flags(s, kind, ref)
        note = s.get(Note, ref)
        day = long_date(note.edition.edition_date)
        unread = "" if note.is_read else " · no leída"
        label = interest_label(note)
        interest = f" · {label}" if label else ""
        self.sub_title = f"{marks(note)}{note.section} · {day}{unread}{interest}"

    def action_open_original(self) -> None:
        sources = self.query_one("#sources", ListView)
        entry = self.highlighted() if self.focused is sources else sources.children[0]
        if isinstance(entry, Entry):
            self.reader.open_article(entry.ref)


class ListScreen(ReaderScreen):
    """Archive, favorites and saved items."""

    def __init__(self, title: str, items: list[Entry]) -> None:
        super().__init__()
        self.list_title, self.items = title, items

    def compose_body(self) -> ComposeResult:
        self.sub_title = self.list_title
        if not self.items:
            yield Static("Nada por aquí todavía.", id="empty")
        yield VimList(*self.items)


class SearchScreen(ReaderScreen):
    def compose_body(self) -> ComposeResult:
        self.sub_title = "Buscar"
        yield Input(placeholder="Buscar en todas las ediciones… (Enter para ir a los resultados)")
        yield VimList(id="results")

    async def on_input_changed(self, event: Input.Changed) -> None:
        with self.reader.session() as s:
            hits = queries.search(s, event.value)
        results = self.query_one("#results", ListView)
        await results.clear()
        await results.extend(
            Entry(h.kind, h.id, Text.assemble((h.title, "bold"), "\n", (h.snippet, "dim")))
            for h in hits
        )
        results.index = 0 if hits else None

    def on_input_submitted(self) -> None:
        self.query_one("#results").focus()


class NewspaperApp(App):
    CSS = """
    #banner { background: $warning 30%; padding: 0 1; }
    #empty { padding: 2 4; }
    ListView { height: 1fr; }
    ListItem { padding: 0 1 1 1; }
    #body { height: 2fr; padding: 1 2; }
    #sources { height: 1fr; border-top: solid $primary; }
    .caption { color: $text-muted; padding: 0 1; }
    """
    BINDINGS = [
        Binding("q", "quit", "Salir"),
        Binding("slash", "search", "Buscar"),
        Binding("a", "archive", "Archivo"),
        Binding("F", "flagged('favorite')", "Favoritos"),
        Binding("S", "flagged('saved')", "Guardados"),
        Binding("g", "front", "Portada"),
    ]

    def __init__(self, config: NewspaperConfig) -> None:
        super().__init__()
        self.config = config
        self.engine = open_db(config)
        self.title = config.general.edition_name
        self.today_status: str | None = None
        self.today_error: str | None = None

    def session(self) -> Session:
        return Session(self.engine)

    def on_mount(self) -> None:
        self.check_today()
        self.push_screen(EditionScreen(None))
        self.set_interval(30, self.check_today)

    def check_today(self) -> None:
        with self.session() as s:
            today = queries.today_edition(s, local_today(self.config))
            status = today.status.value if today else None
            self.today_error = today.error if today else None
        if self.today_status == EditionStatus.generating and status == EditionStatus.ready:
            self.notify("La edición de hoy está lista. Pulsa g para abrirla.", timeout=15)
        self.today_status = status

    def banner_text(self) -> str | None:
        return today_banner(self.today_status, self.today_error)

    def open_article(self, article_id: int) -> None:
        with self.session() as s, s.begin():
            article = s.get(Article, article_id)
            article.is_read = True
            url = article.url
        webbrowser.open(url)

    def action_search(self) -> None:
        self.push_screen(SearchScreen())

    def action_archive(self) -> None:
        with self.session() as s:
            items = [
                Entry("edition", e.id, Text(long_date(e.edition_date)))
                for e in queries.list_editions(s)
            ]
        self.push_screen(ListScreen("Archivo", items))

    def action_flagged(self, flag: str) -> None:
        with self.session() as s:
            notes, articles = queries.flagged(s, flag)
            items = [Entry("note", n.id, note_text(n)) for n in notes]
            items += [Entry("article", a.id, article_text(a)) for a in articles]
        self.push_screen(ListScreen("Favoritos" if flag == "favorite" else "Guardados", items))

    def action_front(self) -> None:
        self.check_today()
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(EditionScreen(None))
