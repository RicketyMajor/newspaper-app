# Newspaper

A personal daily newspaper. You list your RSS sources by section; every morning Newspaper gathers the new articles, groups the coverage of each event, and uses a local LLM (Ollama) to write one integrated note per event: what happened, where the outlets differ, and links to every original. Read it in the terminal or in a local web page; both share the same editions and reading state.

> **Status:** first usable version (MVP). Windows is the supported platform for now.

## How it works

1. **Fetch** every enabled RSS/Atom/RDF feed in `newspaper.toml`.
2. **Extract** the public article text with trafilatura; fall back to the RSS excerpt.
3. **Group** articles about the same event: multilingual embeddings (`bge-m3`) + a cosine-similarity threshold.
4. **Select** a front page (events covered by the most outlets, then the most recent) and up to N notes per section.
5. **Write** each note with `gemma3:12b`: headline, dek, body, and "where sources differ" when there are two or more outlets.
6. **Publish** the edition atomically in SQLite. If Ollama is down the edition fails and the previous one stays readable.

## Requirements

- Windows, Python 3.12+
- [Ollama](https://ollama.com) running locally:

```powershell
ollama pull gemma3:12b
ollama pull bge-m3
```

## Install

```powershell
git clone https://github.com/RicketyMajor/newspaper-app.git
cd newspaper-app
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
newspaper config          # creates ~/.newspaper/newspaper.toml from the example and opens it
```

The configuration is looked up in `./newspaper.toml`, then `~/.newspaper/newspaper.toml` (or pass `--config PATH`). The example ships five Spanish-language sources (CIPER, Cooperativa, BBC Mundo, DW Español, SINC) in three sections; replace them with your own.

## Use

| Command | What it does |
| --- | --- |
| `newspaper generate [--force]` | Prepare today's edition (takes a while: one LLM call per note). |
| `newspaper read` | Terminal reader. |
| `newspaper web [--port 8765]` | Local web reader at `http://127.0.0.1:8765`. |
| `newspaper status` | Today's edition status and the latest ready edition. |
| `newspaper schedule install` | Windows scheduled task at `edition_hour`; runs at the next boot if the PC was off. |
| `newspaper add-feed` / `list-feeds` | Manage sources. |

Opening `read` or `web` after `edition_hour` starts today's edition in the background if it does not exist yet; meanwhile you read the previous one.

### Terminal keys

| Key | Action |
| --- | --- |
| `j` / `k` | move |
| `l` / `Enter` | open |
| `h` / `Esc` | back |
| `m` / `f` / `s` | read / favorite / saved (note, or the focused source) |
| `o` | open the original article in the browser |
| `/` | search every edition |
| `a` | archive |
| `F` / `S` | favorites / saved |
| `g` | latest front page |
| `q` | quit |

## Configuration knobs

`[edition]` in `newspaper.toml`:

- `cluster_threshold` (default `0.75`): raise it if unrelated stories get merged, lower it if the same event appears as several notes.
- `front_page_size`, `max_notes_per_section`, `window_hours`.

## Development

```powershell
pytest          # no network or Ollama needed
ruff check src tests
```

## License

MIT
