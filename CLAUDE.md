# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`bilingual_book_maker` (package `bbook-maker`) is a CLI that translates ebooks (epub/txt/srt/md/pdf) into bilingual versions using a swappable set of translation backends (OpenAI, Claude, Gemini, DeepL, Google, Caiyun, Groq, xAI, Qwen, Tencent, custom APIs). Output is written next to the source as `{name}_bilingual.epub` (or translation-only with `--single_translate`).

## Commands

- **Run the tool**: `python3 make_book.py --book_name <file> --model <alias> ...` (or the installed console script `bbook_maker ...`). `make_book.py` is a one-line shim into `book_maker.cli:main`.
- **Smoke test without an API key**: use `--model google` or `--model deeplfree` (free backends, no key) with `--test --test_num N` to translate only the first N paragraphs.
- **Format**: `black .` — CI enforces `black . --check`. (`make fmt` runs the venv copy.)
- **Tests**: `pytest tests/` — or `make tests` which runs only `tests/test_integration.py`. Integration tests shell out to `make_book.py` with the free `google`/`deeplfree` backends, so they need no API keys.
- **Single test**: `pytest tests/test_integration.py::test_google_translate_epub`.
- **Spell check**: CI runs `typos` (config in `typos.toml`).
- **Docs**: `make serve-docs` (mkdocs; sources in `docs/`).

## Dependencies / packaging

Managed by **PDM** (`pyproject.toml` + `pdm.lock`, Python ≥3.10). `requirements.txt` is **auto-generated** from `pyproject.toml` by the `pdm-autoexport` plugin — do not hand-edit it. Add runtime deps to `[project.dependencies]` in `pyproject.toml`. Version is derived from SCM tags (`tool.pdm.version`).

## Architecture

The CLI is a dispatcher over **two registries**, keyed independently:

- **`BOOK_LOADER_DICT`** (`book_maker/loader/__init__.py`) — maps the source **file extension** → a loader class.
- **`MODEL_DICT`** (`book_maker/translator/__init__.py`) — maps a `--model` **alias** → a translator class.

`cli.py:main()` resolves both, instantiates `loader(book, translator_class, key, ...)`, mutates the loader/translator with the remaining CLI options, then calls `loader.make_bilingual_book()`. Adding a new file format = new loader + one dict entry; adding a new backend = new translator + dict entries.

### Loaders (`book_maker/loader/`)
All subclass `BaseBookLoader` (ABC: `make_bilingual_book`, `_make_new_book`, `load_state`, `_save_temp_book`, `_save_progress`). The loader owns parsing, the translate loop, progress/resume, and output assembly; it calls `self.translate_model.translate(...)` per unit.

`EPUBBookLoader` (~1400 lines) is the heavyweight and the reference implementation. Notable behavior to be aware of before editing it:
- Parses chapter HTML with BeautifulSoup; translates configured tags (`--translate-tags`, default `p`), skips `--exclude-translate-tags` (default `sup,code`). Inserts the translation as a sibling after the original node (or replaces it when `--single_translate`).
- Contains **monkey-patches** of `ebooklib`'s `EpubWriter._write_items` / `EpubReader` (see comments referencing issues #71, #173) — don't "clean these up" without checking those issues.
- Multiple translation strategies coexist and are selected by flags: `--accumulated_num` (accumulate tokens before a call), `block_size` delimiter-batching (default 1), `--sentence_mode`, `--parallel-workers` (per-chapter `ThreadPoolExecutor`), `--batch`/`--batch-use` (OpenAI Batch API), and `--retranslate` (re-do a string range in an already-translated epub).

### Translators (`book_maker/translator/`)
All subclass `Base` (`book_maker/translator/base_translator.py`; ABC: `rotate_key`, `translate`). `Base` also provides shared **batch helpers** — `_build_batch_prompt`, `_extract_paragraphs`, `_do_batch_translate` — that implement delimiter-based multi-paragraph translation with a one-by-one fallback when the model returns the wrong segment count. Keys are comma-splittable and round-robined via `itertools.cycle` (`self.keys`).

**Alias → model selection quirk**: one translator class backs many aliases. `ChatGPTAPI` serves `chatgptapi`/`gpt4`/`gpt4o`/`gpt4omini`/`gpt5mini`/`o1*`/`o3mini`/`openai`; after construction, `cli.py` calls a `set_*_models()` method on the instance to pick the concrete model list for that alias. `--model openai` and `--model gemini` additionally **require `--model_list`**. `claude*` and `qwen-*` aliases are matched by prefix (`set_claude_model` / `set_qwen_model`).

### Custom providers (`book_maker/provider_loader.py`)
`--provider <name>` (mutually exclusive with `--model`) loads a provider from `bbm_providers.json` in the cwd or `~/.bbm/providers.json` (local overrides global). Each provider declares an `api_style` (`openai`/`claude`/`gemini`/`qwen`) that maps to one of those translator classes, plus optional `base_url`, `default_models`, `env_key`. Config is schema-validated in `validate_provider`.

### API keys & env vars
Resolved per-model in `cli.py`. Each backend accepts a `--*_key` flag and falls back to a `BBM_`-prefixed env var (e.g. `BBM_OPENAI_API_KEY`, `BBM_CLAUDE_API_KEY`, `BBM_GOOGLE_GEMINI_KEY`, `BBM_GROQ_API_KEY`, `BBM_QWEN_API_KEY`). Plain `OPENAI_API_KEY` is still honored for backward compat. `--api_base` / `--ollama_model` redirect OpenAI-compatible endpoints (Ollama defaults to `http://localhost:11434/v1`).

### Resume & progress
Progress is pickled to `.{source_stem}.temp.bin` beside the source file. On `KeyboardInterrupt` or error (only when `accumulated_num == 1`) the loader saves progress and a partial book; `--resume` reloads the pickle. This is why interrupting a long run and re-running with `--resume` continues rather than restarts.

### Custom prompts
`--prompt` (`parse_prompt_arg` in `cli.py`) accepts: an inline JSON string, a `.json` file, a `.txt` template, or a **PromptDown** `.md` file. The resolved prompt may only contain keys `user` and `system`, and `user` **must** contain the `{text}` placeholder (`{language}` is optional). Env overrides exist for the ChatGPT backend: `BBM_CHATGPTAPI_USER_MSG_TEMPLATE`, `BBM_CHATGPTAPI_SYS_MSG`.

## Conventions

- New runtime config tunables that aren't per-call CLI flags live in `book_maker/config.py`.
- Match the existing loader/translator structure when extending — register the class in the relevant `__init__.py` dict; the CLI needs no other change for a same-`api_style` backend.
