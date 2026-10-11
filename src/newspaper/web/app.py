"""Local web reader: FastAPI + Jinja2, server-rendered, no accounts."""

from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..config.models import NewspaperConfig
from ..dates import long_date
from ..edition.today import local_today, today_banner
from ..storage import queries
from ..storage.database import open_db
from ..storage.models import Article

HERE = Path(__file__).parent
STATIC = HERE / "static"


def create_app(config: NewspaperConfig) -> FastAPI:
    engine = open_db(config)
    app = FastAPI(title=config.general.edition_name, docs_url=None, redoc_url=None)
    # DNS rebinding: a foreign domain resolving to 127.0.0.1 must not read the reading history.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    # LLM/RSS text is untrusted: no raw HTML, no external images (tracking pixels).
    markdown = MarkdownIt("commonmark", {"html": False}).disable("image")
    templates.env.filters["markdown"] = markdown.render
    templates.env.filters["long_date"] = long_date
    # Cache-busting for /static after an upgrade: the newest file's mtime.
    templates.env.globals["static_v"] = int(max(p.stat().st_mtime for p in STATIC.iterdir()))
    templates.env.filters["lede"] = lambda body: body.strip().split("\n\n", 1)[0]
    templates.env.filters["es_list"] = lambda items: " y ".join(
        [", ".join(items[:-1]), items[-1]] if len(items) > 1 else items
    )

    def db():
        with Session(engine) as s:
            yield s

    DB = Annotated[Session, Depends(db)]  # noqa: N806 - type alias

    def render(request: Request, s: Session, name: str, **context) -> HTMLResponse:
        today = queries.today_edition(s, local_today(config))
        banner = today_banner(today.status, today.error) if today else None
        # Masthead (date, progress, section nav) follows the page's edition, else the latest.
        paper_edition = context.get("edition") or queries.latest_ready_edition(s)
        return templates.TemplateResponse(
            request,
            name,
            {
                "paper": config.general.edition_name,
                "banner": banner,
                "today_status": today.status.value if today else None,
                "paper_edition": paper_edition,
                "nav_sections": queries.section_counts(s, paper_edition.id)
                if paper_edition
                else [],
                **context,
            },
        )

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        message = "Esta página no existe." if exc.detail == "Not Found" else exc.detail
        title = "No encontrado" if exc.status_code == 404 else "No se pudo completar"
        with Session(engine) as s:
            response = render(request, s, "error.html", title=title, message=message)
        response.status_code = exc.status_code
        response.headers.update(exc.headers or {})  # e.g. Allow on a 405
        return response

    @app.exception_handler(RequestValidationError)
    def invalid_request(request: Request, _exc: RequestValidationError) -> HTMLResponse:
        with Session(engine) as s:
            response = render(
                request, s, "error.html", title="Dirección no válida",
                message="Revisa la fecha o el enlace.",
            )  # fmt: skip
        response.status_code = 422
        return response

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
        # Opening marks it read; un-marking it (fetch, no reload) sticks until the next visit,
        # like `m` in the TUI.
        queries.set_flag(s, "note", note_id, "read", True)
        s.commit()
        notes = found.edition.notes  # edition order (rank)
        i = [n.id for n in notes].index(note_id)
        next_note = notes[i + 1] if i + 1 < len(notes) else None
        return render(
            request, s, "note.html", edition=found.edition, note=found,
            position=i + 1, total=len(notes), next_note=next_note,
        )  # fmt: skip

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
            value = queries.set_flag(s, "note" if kind == "n" else "article", obj_id, flag)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        s.commit()
        if "application/json" in request.headers.get("accept", ""):  # app.js, no reload
            return {"value": value}
        back = urlsplit(request.headers.get("referer", "/"))
        anchor = f"#a-{obj_id}" if kind == "a" else ""  # without JS, land back on the source
        return RedirectResponse(back.path + (f"?{back.query}" if back.query else "") + anchor, 303)

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
        return render(
            request, s, "flagged.html", title="Favoritos", toggle="Favorito",
            notes=notes, articles=articles,
        )  # fmt: skip

    @app.get("/guardados", response_class=HTMLResponse)
    def saved(request: Request, s: DB):
        notes, articles = queries.flagged(s, "saved")
        return render(
            request, s, "flagged.html", title="Guardados", toggle="Guardado",
            notes=notes, articles=articles,
        )  # fmt: skip

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
