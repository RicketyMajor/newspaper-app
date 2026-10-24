# Newspaper

A personal daily newspaper. You list your RSS sources by section; every morning Newspaper gathers the new articles, groups the coverage of each event, and uses a local LLM (Ollama) to write one integrated note per event: what happened, where the outlets differ, and links to every original. Read it in the terminal or in a local web page; both share the same editions and reading state.

> **Status:** first usable version (MVP). Windows is the supported platform for now.

## How it works

1. **Fetch** every enabled RSS/Atom/RDF feed in `newspaper.toml`.
2. **Extract** the public article text with trafilatura; fall back to the RSS excerpt.
3. **Group** articles about the same event: multilingual embeddings (`bge-m3`) + a cosine-similarity threshold.
4. **Select** a front page (events covered by the most outlets, then the most recent) and up to N notes per section. With `[interests]`, the local model tags each event with one of your topics: "dislike" ones go last and never to the front page, "like" ones go first among events covered by as many outlets.
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

The configuration is looked up in `./newspaper.toml`, then `~/.newspaper/newspaper.toml` (or pass `--config PATH`). The example ships sixteen sources in five sections: Chile (CIPER, Cooperativa), Mundo (BBC Mundo, DW Español), Ciencia (El País, The Conversation, Xataka Ciencia), Tecnología (El País, Xataka, Genbeta, MIT Technology Review, The Verge) and Cultura (El País, Culto, elDiario.es, The Guardian Books). English sources are written up in Spanish like the rest. Replace them with your own.

## Use

| Command | What it does |
| --- | --- |
| `newspaper generate [--force]` | Prepare today's edition (takes a while: one LLM call per note). |
| `newspaper fetch` | Fetch the feeds and store new articles (no LLM). The scheduled task runs it every 30 minutes so short feeds don't lose articles between editions. |
| `newspaper read` | Terminal reader. |
| `newspaper web [--port 8765]` | Local web reader at `http://127.0.0.1:8765`. |
| `newspaper status` | Today's edition status and the latest ready edition, plus the daily notice's funnel (sent · arrived · saved · tapped · opened · read; arrived, saved and tapped come from the phone) and how many devices are subscribed. |
| `newspaper notify` | Send today's notice again, now, to every subscribed device (to test it, or after a notice that didn't arrive). |
| `newspaper schedule install` | Windows scheduled tasks: the edition at `edition_hour` (wakes the PC from sleep, and keeps it awake until the edition is ready; runs at the next boot if the PC was off), `fetch` every 30 minutes, the web reader (`Newspaper web`: at logon and checked every 30 minutes; it keeps the PC awake while it's needed, see below) and, with `notice_time` set, the notice (`Newspaper aviso`). Run it again after updating the code or `newspaper.toml`: it restarts the web server. |
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

### Read on your phone (Tailscale)

The web reader only listens on `127.0.0.1`. [Tailscale](https://tailscale.com) publishes it inside your own tailnet, never on the internet:

1. Install Tailscale on the PC (`winget install Tailscale.Tailscale`) and sign in with the account your phone uses.
2. In the Tailscale menu, turn on **Run unattended**. In the admin console, **disable key expiry** for this PC (keys expire after 180 days by default).
3. `tailscale serve --bg --https=443 http://127.0.0.1:8765`, then open the `https://<pc>.<tailnet>.ts.net` address it prints on your phone. Chrome on Android offers **Install app**.

`newspaper schedule install` keeps the server running (the `Newspaper web` task). The server keeps the PC awake while an edition is written, 20 minutes after it, 30 minutes after the notice and 15 minutes after each visit; then it puts back to sleep a PC nobody is using (one you are using stays on, per your Windows power settings). A PC that was shut down (not asleep) stays off until you turn it on. Errors go to `web.log`, next to the database. Note that the HTTPS certificate publishes the `<pc>.<tailnet>.ts.net` name in public Certificate Transparency logs.

**Daily notice.** At the foot of Today, **Avisarme cuando salga …** subscribes that browser (the installed app counts as its own) to a Web Push notice. The notice is end-to-end encrypted and goes through the browser's push service (Google's, for Chrome): the PC's first outbound request that isn't a feed. Its signing key, `vapid.pem`, sits next to the database. A new key (after deleting it and running `newspaper schedule install`) voids every subscription; each device subscribes again on its next visit to Today.

- **When it goes out:** without `notice_time` (in `[general]`), when each edition is published. With `notice_time = "07:30"` (local `HH:MM`, up to 22:59), the `Newspaper aviso` task wakes the PC at that time and sends it, retrying for 9 minutes while the network comes back; `generate` and `fetch` leave it to the task until 5 minutes past that time. Run `newspaper schedule install` after changing it.
- **Reading without the PC:** when the notice arrives, the installed app saves a read-only copy of the latest edition and of Guardado (all or nothing; `status` shows it as «guardada»). While the PC sleeps or is out of reach, Today, its notes, Pleno día and Guardado open from that copy, with the line «Sin el PC: …» at the foot of Today. Archive, search and anything else show a short page saying they need the PC. The original articles still open, since they come from the internet. Marks can't change without the PC, so their buttons wait for it. Openings, the notice's marks and the sources you open are queued with their time and sent when the PC answers again.

## Configuration knobs

`[edition]` in `newspaper.toml`:

- `cluster_threshold` (default `0.75`): raise it if unrelated stories get merged, lower it if the same event appears as several notes.
- `front_page_size`, `max_notes_per_section`, `window_hours`.
- `section_limits = { "Cultura" = 5 }`: a section with its own cap instead of `max_notes_per_section`.

Per feed (`[[feeds]]`), optional rules: regexes searched (case-insensitive) in each article's URL and title, applied to newly fetched articles.

- `exclude = ["/deportes/"]`: drop matching articles; they are never stored, extracted or written.
- `route = { "/mundo/" = "Mundo" }`: move matching articles to another section (first match wins; `exclude` goes first).

Optional `[interests]` (see the commented example): `like` and `dislike` lists of short phrases. `gemma3:12b` picks each event's main topic among them plus a few fixed neutral topics (one quick call per event, about 1.5 s each); a story that fits both lists counts as "dislike". Each note shows its label ("Te interesa: …" / "Menos interés: …").

## Development

```powershell
pytest          # no network or Ollama needed
ruff check src tests
```

## License

MIT
