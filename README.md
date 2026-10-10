# Newspaper

A personal daily newspaper. You list your RSS sources by section; Newspaper gathers the day's articles, groups the coverage of each event, and uses a local LLM (Ollama) to write one integrated note per event — what happened, where sources differ, and links to every original. Read it in the terminal (Textual) or in a local web page.

> **Status:** under construction. The MVP is being built in this order: configuration → storage → ingestion → grouping → generation → web reader → TUI → scheduling. Windows is the first supported platform.

## Requirements

- Python 3.12+
- [Ollama](https://ollama.com) running locally with `gemma3:12b` and `bge-m3` pulled

## Install (development)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

## License

MIT
