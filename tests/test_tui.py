from sqlalchemy.orm import Session
from textual.widgets import Input, ListView

from newspaper.storage.models import Article, Note
from newspaper.tui.app import EditionScreen, ListScreen, NewspaperApp, NoteScreen, SearchScreen


def entries(screen) -> list[tuple[str, object]]:
    return [(item.kind, item.ref) for item in screen.query_one(ListView).children]


async def test_front_page_lists_front_notes_then_sections(config, seeded):
    app = NewspaperApp(config)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, EditionScreen)
        assert entries(app.screen) == [("note", seeded["front"]), ("section", "Mundo")]
        assert "sábado 10 de octubre de 2026" in app.screen.sub_title


async def test_open_note_mark_flags_and_go_back(config, engine, seeded):
    app = NewspaperApp(config)
    async with app.run_test() as pilot:
        await pilot.press("l")
        assert isinstance(app.screen, NoteScreen)
        await pilot.press("f", "s")
        await pilot.press("h")
        assert isinstance(app.screen, EditionScreen)
    with Session(engine) as s:
        note = s.get(Note, seeded["front"])
        assert (note.is_read, note.is_favorite, note.is_saved) == (True, True, True)


async def test_flags_apply_to_the_focused_source(config, engine, seeded):
    app = NewspaperApp(config)
    async with app.run_test() as pilot:
        await pilot.press("l")
        app.screen.query_one("#sources", ListView).focus()
        await pilot.press("j", "f")
    with Session(engine) as s:
        assert s.get(Article, seeded["articles"][1]).is_favorite
        assert not s.get(Note, seeded["front"]).is_favorite


async def test_section_and_archive_navigation(config, seeded):
    app = NewspaperApp(config)
    async with app.run_test() as pilot:
        await pilot.press("j", "l")
        assert entries(app.screen) == [("note", seeded["world"])]
        await pilot.press("a")
        assert isinstance(app.screen, ListScreen)
        assert entries(app.screen) == [("edition", seeded["edition"])]


async def test_search_finds_notes(config, seeded):
    app = NewspaperApp(config)
    async with app.run_test() as pilot:
        await pilot.press("slash")
        assert isinstance(app.screen, SearchScreen)
        app.screen.query_one(Input).value = "pension"
        await pilot.pause()
        assert entries(app.screen) == [("note", seeded["front"])]
        await pilot.press("enter", "enter")
        assert isinstance(app.screen, NoteScreen)


async def test_empty_state_without_editions(config):
    app = NewspaperApp(config)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "Todavía no hay ediciones" in str(app.screen.query_one("#empty").render())
