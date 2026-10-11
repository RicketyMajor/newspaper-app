"""Local web reader: FastAPI + Jinja2, server-rendered, no accounts."""

from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from sqlalchemy.orm import Session

from ..config.models import NewspaperConfig
from ..dates import long_date
from ..edition.today import local_today, today_banner
from ..storage import queries
from ..storage.database import open_db
from ..storage.models import Article

HERE = Path(__file__).parent


def create_app(config: NewspaperConfig) -> FastAPI:
    engine = open_db(config)
    app = FastAPI(title=config.general.edition_name, docs_url=None, redoc_url=None)
    # DNS rebinding: a foreign domain resolving to 127.0.0.1 must not read the reading history.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    # LLM/RSS text is untrusted: no raw HTML, no external images (tracking pixels).
    markdown = MarkdownIt("commonmark", {"html": False}).disable("image")
    templates.env.filters["markdown"] = markdown.render
    templates.env.filters["long_date"] = long_date

    def db():
        with Session(engine) as s:
            yield s

    DB = Annotated[Session, Depends(db)]  # noqa: N806 - type alias

    def render(request: Request, s: Session, name: str, **context) -> HTMLResponse:
        today = queries.today_edition(s, local_today(config))
        banner = today_banner(today.status, today.error) if today else None
        if context.get("edition") is not None:  # section nav on every edition page
            context.setdefault("sections", queries.section_counts(s, context["edition"].id))
        return templates.TemplateResponse(
            request,
            name,
            {
                "paper": config.general.edition_name,
                "banner": banner,
                "today_status": today.status.value if today else None,
                **context,
            },
        )

    def edition_page(request: Request, s: Session, edition) -> HTMLResponse:
        if edition is None:
            return render(request, s, "empty.html", edition=None)
        counts = queries.section_counts(s, edition.id)
        sections = [(name, queries.section_notes(s, edition.id, name)) for name, _ in counts]
        front = queries.front_page(s, edition.id)
        return render(request, s, "front.html", edition=edition, front=front, sections=sections)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, s: DB):
        return edition_page(request, s, queries.latest_ready_edition(s))

    @app.get("/e/{day}", response_class=HTMLResponse)
    def edition(request: Request, s: DB, day: date):
        found = queries.ready_edition(s, day)
        if found is None:
            raise HTTPException(404, "No hay edición para esa fecha")
        return edition_page(request, s, found)

    @app.get("/e/{day}/{section}", response_class=HTMLResponse)
    def section(request: Request, s: DB, day: date, section: str):
        found = queries.ready_edition(s, day)
        if found is None:
            raise HTTPException(404, "No hay edición para esa fecha")
        notes = queries.section_notes(s, found.id, section)
        return render(request, s, "section.html", edition=found, section=section, notes=notes)

    @app.get("/n/{note_id}", response_class=HTMLResponse)
    def note(request: Request, s: DB, note_id: int):
        found = queries.get_note(s, note_id)
        if found is None:
            raise HTTPException(404, "Nota no encontrada")
        queries.set_flag(s, "note", note_id, "read", True)
        s.commit()
        return render(request, s, "note.html", edition=found.edition, note=found)

    @app.get("/a/{article_id}")
    def original(s: DB, article_id: int):
        article = s.get(Article, article_id)
        if article is None:
            raise HTTPException(404, "Artículo no encontrado")
        article.is_read = True
        url = article.url
        s.commit()
        return RedirectResponse(url, status_code=307)

    @app.post("/{kind}/{obj_id}/estado")
    def toggle(
        request: Request,
        s: DB,
        kind: Literal["n", "a"],
        obj_id: int,
        flag: Literal["read", "favorite", "saved"],  # in the form's action URL, not its body
    ):
        try:
            queries.set_flag(s, "note" if kind == "n" else "article", obj_id, flag)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        s.commit()
        back = urlsplit(request.headers.get("referer", "/"))
        return RedirectResponse(back.path + (f"?{back.query}" if back.query else ""), 303)

    @app.get("/archivo", response_class=HTMLResponse)
    def archive(request: Request, s: DB):
        return render(request, s, "archive.html", editions=queries.list_editions(s))

    @app.get("/buscar", response_class=HTMLResponse)
    def search(request: Request, s: DB, q: str = ""):
        hits = queries.search(s, q)
        return render(request, s, "search.html", q=q, hits=hits)

    @app.get("/favoritos", response_class=HTMLResponse)
    def favorites(request: Request, s: DB):
        notes, articles = queries.flagged(s, "favorite")
        return render(request, s, "flagged.html", title="Favoritos", notes=notes, articles=articles)

    @app.get("/guardados", response_class=HTMLResponse)
    def saved(request: Request, s: DB):
        notes, articles = queries.flagged(s, "saved")
        return render(request, s, "flagged.html", title="Guardados", notes=notes, articles=articles)

    @app.get("/estado")
    def status(s: DB):
        today = queries.today_edition(s, local_today(config))
        latest = queries.latest_ready_edition(s)
        return {
            "today": today.status.value if today else None,
            "error": today.error if today else None,
            "latest": latest.edition_date.isoformat() if latest else None,
        }

    return app


def serve(config: NewspaperConfig, port: int = 8765, open_browser: bool = True) -> None:
    import threading
    import webbrowser

    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    print(f"Newspaper en {url} (Ctrl+C para salir)")
    uvicorn.run(create_app(config), host="127.0.0.1", port=port, log_level="warning")
