"""Local web reader: FastAPI + Jinja2, server-rendered, no accounts."""

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import schedule
from ..config.models import NewspaperConfig
from ..dates import long_date
from ..edition.today import local_today, running, today_banner
from ..storage import queries
from ..storage.database import open_db
from ..storage.models import Article, Edition, EditionStatus, utcnow

HERE = Path(__file__).parent
STATIC = HERE / "static"
log = logging.getLogger(__name__)


def same_origin(request: Request) -> None:
    """Another site's form or fetch (any page open in the phone or PC browser) must not change
    anything here. same-site is not enough: it ignores the port. ponytail: no header passes (old
    browsers and the tests); every current browser sends it."""
    if request.headers.get("sec-fetch-site", "none") not in ("same-origin", "none"):
        raise HTTPException(403, "Ese cambio llegó desde otro sitio.")


def _push(config: NewspaperConfig):
    """The notices' key, created on the first start: (notify, key, public key). Any failure (a
    missing dependency, a corrupt PEM, a filesystem without hard links) turns the notices off,
    never the reader."""
    try:
        from .. import notify  # heavy (cryptography): imported after web.log is set up

        vapid = notify.ensure_key(notify.key_path(config))
        return notify, vapid, notify.public_key(vapid)
    except Exception:
        log.warning("Avisos desactivados: no se pudo preparar la clave", exc_info=True)
        return None


def create_app(config: NewspaperConfig) -> FastAPI:
    engine = open_db(config)
    app = FastAPI(title=config.general.edition_name, docs_url=None, redoc_url=None)

    # `serve` keeps the PC awake while this says so, asked every minute and on each visit: after
    # the 06:00 wake an unattended PC sleeps again once nobody needs it (2026-10-22).
    app.state.visited, app.state.recheck = datetime.min, lambda: None

    @app.middleware("http")  # inside TrustedHostMiddleware: a refused Host is no visit
    async def visit(request: Request, call_next):
        if request.url.path != "/estado":  # a tab left open polls it while today isn't ready
            app.state.visited = utcnow()
            app.state.recheck()  # now: the PC may be one breath from sleeping
        return await call_next(request)

    def awake() -> bool:
        now = utcnow()
        if now - app.state.visited < schedule.AWAKE_AFTER_VISIT:
            return True
        with Session(engine) as s:
            # While it's written too, so the hold is already here when `generate` lets go.
            # ponytail: a run shorter than one renewal goes unseen; after the 06:00 wake it ends
            # inside Windows' 2-min unattended timeout, so the next check still catches it. A
            # handoff from `generate` if unattended runs ever start mid-morning.
            making = s.scalars(select(Edition).where(Edition.status == EditionStatus.generating))
            if any(running(e) for e in making):
                return True
            # Any date: a run past midnight is still this morning's.
            finished = s.scalar(select(func.max(Edition.finished_at)))
        return finished is not None and now - finished < schedule.AWAKE_AFTER_EDITION

    app.state.awake = awake
    # DNS rebinding: a foreign domain resolving to 127.0.0.1 must not read the reading history.
    # *.ts.net: the phone through `tailscale serve`; Tailscale owns that DNS, nobody can rebind it.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "*.ts.net"])
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    # LLM/RSS text is untrusted: no raw HTML, no external images (tracking pixels).
    markdown = MarkdownIt("commonmark", {"html": False}).disable("image")
    templates.env.filters["markdown"] = markdown.render
    # Headlines and deks are plain text but for *work titles* in italics; HTML stays escaped.
    templates.env.filters["inline"] = MarkdownIt("zero").enable("emphasis").renderInline
    templates.env.filters["long_date"] = long_date
    # Cache-busting for /static after an upgrade: the newest file's mtime.
    templates.env.globals["static_v"] = int(max(p.stat().st_mtime for p in STATIC.iterdir()))
    templates.env.filters["es_list"] = lambda items: " y ".join(
        [", ".join(items[:-1]), items[-1]] if len(items) > 1 else items
    )

    def db():
        with Session(engine) as s:
            yield s

    DB = Annotated[Session, Depends(db)]  # noqa: N806 - type alias
    push = _push(config)

    def render(request: Request, s: Session, name: str, **context) -> HTMLResponse:
        today = queries.today_edition(s, local_today(config))
        banner = today_banner(today.status, today.error) if today else None
        latest = queries.latest_ready_edition(s)
        edition = context.get("edition")
        # The masthead's date follows the page's edition, else the latest.
        paper_edition = edition or latest
        # The latest edition's pages carry its date: back in the app, app.js reloads Today when
        # a newer one is out (the installed app resumes yesterday's page without asking).
        live = edition is not None and latest is not None and edition.id == latest.id
        return templates.TemplateResponse(
            request,
            name,
            {
                "paper": config.general.edition_name,
                "banner": banner,
                "today_status": today.status.value if today else None,
                "paper_edition": paper_edition,
                "live_day": edition.edition_date if live else None,
                **context,
            },
        )

    def opened(response: HTMLResponse, request: Request, *objs) -> HTMLResponse:
        """Record the first opening of editions and notes. Only real navigations count (not
        curl, a check or the tests), and only the first: a revisit writes nothing. It runs after
        the page is sent, so the writer lock (generate, fetch) never delays the page."""
        firsts = [(type(obj), obj.id) for obj in objs if obj.opened_at is None]
        if firsts and request.headers.get("sec-fetch-dest") == "document":
            response.background = BackgroundTask(record_openings, firsts)
        return response

    def record_openings(firsts: list[tuple[type, int]]) -> None:
        # Its own transaction: a locked database is only logged, never an error page.
        try:
            with engine.begin() as conn:
                for model, obj_id in firsts:
                    stmt = update(model).where(model.id == obj_id, model.opened_at.is_(None))
                    conn.execute(stmt.values(opened_at=utcnow()))
        except OperationalError:
            log.warning("No se pudo guardar una apertura", exc_info=True)

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

    def edition_page(request: Request, s: Session, edition, today: bool = False) -> HTMLResponse:
        # The notice line lives on Today only, with or without an edition.
        key = push[2] if today and push else None
        if edition is None:
            return render(request, s, "empty.html", edition=None, notice_key=key)
        core = queries.reading_order(edition.notes)[0]
        # Each band: its first note (the core's) big, the rest folded under "y N más".
        sections = []
        for name, _ in queries.section_counts(s, edition.id):
            first, *rest = queries.section_notes(s, edition.id, name)
            sections.append((name, first, rest))
        response = render(
            request, s, "front.html", edition=edition, front=queries.front_page(s, edition.id),
            sections=sections, next_core=next((n for n in core if not n.is_read), None),
            core_read=sum(n.is_read for n in core), core_total=len(core),
            core_minutes=sum(n.read_minutes for n in core), notice_key=key,
        )  # fmt: skip
        return opened(response, request, edition)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, s: DB):
        return edition_page(request, s, queries.latest_ready_edition(s), today=True)

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
        core, rest = queries.reading_order(found.edition.notes)
        path = core + rest
        i = path.index(found)

        def label(j: int) -> str:
            if j < len(core):
                return f"{j + 1} de {len(core)}"
            return f"Más · {j - len(core) + 1} de {len(rest)}"

        at_noon = i == len(core) - 1  # the core ends at noon, then the rest of the edition
        next_note = path[i + 1] if i + 1 < len(path) and not at_noon else None
        if at_noon:
            next_url = f"/fin/{found.edition.edition_date}"
        else:
            next_url = f"/n/{next_note.id}" if next_note else None
        # The header takes its band's color: the front page is the first band of the sky, and
        # the bands spread over the six stops of the ramp (--r0…--r5 in style.css).
        sections = [name for name, _ in queries.section_counts(s, found.edition.id)]
        band = 0 if found.is_front else sections.index(found.section) + 1
        response = render(
            request, s, "note.html", edition=found.edition, note=found, position_label=label(i),
            prev_url=f"/n/{path[i - 1].id}" if i else None, next_url=next_url,
            next_note=next_note, next_label=label(i + 1),
            in_core=i < len(core), core_read=sum(n.is_read for n in core), core_total=len(core),
            tint=int(band * 5 / max(1, len(sections)) + 0.5),  # round half up, not to even
        )  # fmt: skip
        # Its edition too: a note reached from search or a bookmark opened it, never Today.
        return opened(response, request, found, found.edition)

    @app.get("/fin/{day}", response_class=HTMLResponse)
    def end(request: Request, s: DB, day: date):
        found = queries.ready_edition(s, day)
        if found is None:
            raise HTTPException(404, "No hay edición para esa fecha")
        core, rest = queries.reading_order(found.notes)
        front = sum(n.is_front for n in core)
        bands = ([("Portada", front)] if front else []) + [
            (n.section, 1) for n in core if not n.is_front
        ]
        return render(
            request, s, "end.html", edition=found, core_total=len(core),
            core_minutes=sum(n.read_minutes for n in core), bands=bands, rest_total=len(rest),
            core_sections=len({n.section for n in core}),
            first_rest=rest[0] if rest else None, core_left=sum(not n.is_read for n in core),
            next_hour=f"{config.general.edition_hour:02d}:15",
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
        value: bool | None = None,  # set it (app.js marks a finished note); without it, toggle
    ):
        same_origin(request)
        try:
            value = queries.set_flag(s, "note" if kind == "n" else "article", obj_id, flag, value)
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
        editions = queries.list_editions(s)
        cores = {e.id: queries.reading_order(e.notes)[0] for e in editions}
        suns = {e: (sum(n.is_read for n in core), len(core)) for e, core in cores.items()}
        return render(request, s, "archive.html", editions=editions, suns=suns)

    @app.get("/buscar", response_class=HTMLResponse)
    def search(request: Request, s: DB, q: str = ""):
        hits = queries.search(s, q)
        return render(request, s, "search.html", q=q, hits=hits)

    @app.get("/guardado", response_class=HTMLResponse)
    def saved(request: Request, s: DB, f: Literal["todo", "favoritos", "guardados"] = "todo"):
        flags = {"todo": ("favorite", "saved"), "favoritos": ("favorite",), "guardados": ("saved",)}
        notes, articles = queries.flagged(s, *flags[f])
        return render(request, s, "saved.html", f=f, notes=notes, articles=articles)

    @app.get("/favoritos")
    def favorites_moved():
        return RedirectResponse("/guardado?f=favoritos", 301)

    @app.get("/guardados")
    def saved_moved():
        return RedirectResponse("/guardado?f=guardados", 301)

    @app.get("/estado")
    def status(s: DB):
        today = queries.today_edition(s, local_today(config))
        latest = queries.latest_ready_edition(s)
        return {
            "today": today.status.value if today else None,
            "error": today.error if today else None,
            "latest": latest.edition_date.isoformat() if latest else None,
        }

    @app.get("/manifest.webmanifest")
    def manifest():
        # Chrome on Android installs the paper as an app from this (HTTPS by tailscale serve).
        def icon(size: int, purpose: str) -> dict:
            return {"src": f"/static/icon-{size}.png", "sizes": f"{size}x{size}",
                    "type": "image/png", "purpose": purpose}  # fmt: skip

        name = config.general.edition_name
        return JSONResponse(
            {
                "name": name,
                "short_name": name,
                "start_url": "/",
                "display": "standalone",
                "background_color": "#F2F4F5",  # --mist
                "theme_color": "#F2F4F5",
                # The glyph sits inside the maskable safe zone: one image serves both purposes.
                "icons": [icon(192, "any"), icon(512, "any"), icon(512, "maskable")],
            },
            media_type="application/manifest+json",
        )

    if push:
        notify, vapid, _ = push
        worker = notify.service_worker(HERE / "sw.js", STATIC, config.general.edition_name)

        @app.get("/sw.js")
        def service_worker():
            # Explicit type: Windows' registry can map .js to text/plain, and a service worker
            # must be JavaScript.
            headers = {"Cache-Control": "no-cache"}
            return Response(worker, media_type="text/javascript", headers=headers)

        @app.post("/aviso/alta")
        def subscribe(request: Request, alta: notify.Alta):
            same_origin(request)
            new, gone, sid = notify.upsert(engine, alta)
            if alta.confirm and not gone:
                name = config.general.edition_name
                test = notify.Message("confirmacion", "Listo", f"Te aviso cuando salga {name}.")
                try:  # the subscription is saved already: a failed test is only logged
                    notify.send(engine, vapid, test, log.warning, only=sid)
                except Exception:
                    log.warning("No se pudo enviar la confirmación", exc_info=True)
            return {"new": new, "gone": gone}

        @app.post("/aviso/baja")
        def unsubscribe(request: Request, baja: notify.Baja):
            same_origin(request)
            notify.remove(engine, baja.endpoint)
            return {"ok": True}

        @app.post("/aviso/{day}/{event}", status_code=204)
        def ping(request: Request, day: date, event: Literal["entregado", "tocado"], s: int):
            same_origin(request)
            if not notify.record_ping(engine, day, event, s):
                raise HTTPException(404, "No hay edición para esa fecha")
            return Response(status_code=204)

    return app


def serve(config: NewspaperConfig, port: int, open_browser: bool = True) -> None:
    """Serve on 127.0.0.1 until stopped; `newspaper web` sets up web.log around it."""
    import threading
    import webbrowser

    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    print(f"Newspaper en {url} (Ctrl+C para salir)")
    app = create_app(config)
    # After the 06:00 wake Windows sleeps again unless asked: the phone needs it awake for the
    # notice and while reading, not all day.
    with schedule.keep_awake(app.state.awake) as app.state.recheck:
        # log_config=None: uvicorn's default calls sys.stdout.isatty(); pythonw has None.
        uvicorn.run(app, host="127.0.0.1", port=port, log_config=None)
