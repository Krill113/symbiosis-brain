from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from symbiosis_brain.storage import Storage

from symbiosis_brain import retrieval_log
from symbiosis_brain.retrieval_log import LogContext

logger = logging.getLogger("symbiosis-brain.search")

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
"""The default model, used whenever a vault's DB has no
schema_version.embedding_model row yet (fresh vault, or a legacy DB from
before this row existed). Doubles as the module's *legacy* active-model
selector: _get_embedder()/_embed()/_embed_one() read it fresh on every call,
exactly as before this file learned to switch models. SearchEngine resolves
each instance's own model from the DB (see _resolve_model_name) and threads
that name explicitly through _embed_documents/_embed_query — but those DO
mutate this global, via _set_active_model, as the bridge that keeps the
legacy _get_embedder()/_embed() singleton pointed at whichever model the
current call actually wants. So after a process's first real embed call this
no longer reliably names *the default* — see _DEFAULT_MODEL_NAME below for
that. tools/eval_search.py also mutates it directly (apply_model_override):
a standalone CLI process, never imported by the server, so its process-wide
model swap can never race a real server's per-vault resolution."""
_DEFAULT_MODEL_NAME = _MODEL_NAME
"""Frozen copy of the startup default, for messages that must name it even
after _set_active_model has repointed the mutable _MODEL_NAME above at
something else (see its docstring)."""
_EMBED_BATCH_SIZE = 16
"""fastembed's silent default is batch_size=256. With 512-token padded
documents that peaks the onnxruntime CPU arena at ~11 GB on a ~1300-note
corpus; the arena is never returned to the OS. batch_size=16 measured
identical throughput (518 vs 544 CPU-s) at ~1.1 GB peak (2026-08-10)."""
_embedder = None

_DEFAULT_EMBEDDING_DIM = 384
"""BAAI/bge-small-en-v1.5's vector width — the fallback _embedding_dim()
returns only when fastembed has no metadata for a model name at all."""

_E5_SMALL_INT8_MODEL_NAME = "intfloat/multilingual-e5-small-int8"
"""Alias this model lives under in schema_version.embedding_model. Not the
bare HF repo id (intfloat/multilingual-e5-small) on purpose: fastembed's
registry has no entry for that repo at all (see _CUSTOM_MODELS below), and an
alias that spells out both the origin and the quantization keeps a DB row
self-explanatory without a code lookup."""

_CUSTOM_MODELS: dict[str, dict] = {
    _E5_SMALL_INT8_MODEL_NAME: {
        "hf": "intfloat/multilingual-e5-small",
        "model_file": "onnx/model_qint8_avx512_vnni.onnx",
        "dim": 384,
        "pooling": "MEAN",
        "normalization": True,
        "description": (
            "intfloat/multilingual-e5-small, int8 (avx512-vnni) quantized "
            "ONNX weights served straight from the model's own HF repo. "
            "Measured MRR 0.460 on Russian queries vs 0.265 for "
            "paraphrase-multilingual-MiniLM-L12-v2 and 0.109 for "
            "BAAI/bge-small-en-v1.5 (147 queries / 76 notes, 2026-09-02), at "
            "483 MB resident vs 631 MB for the MiniLM model. Same 384-dim "
            "output as both."
        ),
        "license": "mit",
    },
}
"""Models fastembed 0.8.0's own registry (TextEmbedding.list_supported_models)
has no entry for at all — each needs one TextEmbedding.add_custom_model call,
made idempotently by _ensure_custom_model_registered, before its dimension or
its weights can be looked up. Keyed by the alias this codebase calls the
model (see each entry's `hf` for the actual HF repo, which may differ from
the key — e5-small-int8 is exactly that case)."""


_custom_model_registration_lock = threading.Lock()
"""Guards _ensure_custom_model_registered's check-then-act against fastembed's
add_custom_model, which raises ValueError('... is already registered') on a
second call for the same name (verified against the installed fastembed
0.8.0 source). _get_embedder() and _embedding_dim() both call into
_ensure_custom_model_registered from independent, differently-locked (one:
the cross-process sb-fastembed-init.lock; the other: no lock at all) paths,
so without a lock scoped to this check-then-act itself, two threads racing
between the list_supported_models() check and the add_custom_model() call
could both see "not registered yet" and both call add_custom_model — the
loser raising ValueError. Cross-process races are not possible here:
fastembed's model registry is an in-memory, per-interpreter list, never
shared or persisted, so a plain threading.Lock (not the file-based
cross-process lock _get_embedder uses for the separate concern of
concurrent cold starts) fully closes the window."""


def _ensure_custom_model_registered(model_name: str) -> None:
    """Register `model_name` with fastembed via add_custom_model if — and
    only if — it is one of ours (_CUSTOM_MODELS) AND fastembed does not
    already know it. A no-op for every built-in fastembed model name (the
    overwhelming majority of calls) and for typos/unknown names alike: those
    are none of this function's business and _embedding_dim's own fallback
    handles them.

    MUST be called before both get_embedding_size and TextEmbedding(...) for
    a custom name — an unregistered name silently returns _embedding_dim's
    384-fallback (right by coincidence, since every model in this table
    happens to be 384-dim too) or, for a bigger custom model, an outright
    wrong dimension.

    Idempotent by construction: checked against
    TextEmbedding.list_supported_models(), never by catching the
    already-registered exception add_custom_model raises for a repeat name —
    and the whole check-then-act runs under _custom_model_registration_lock
    (see its docstring) so that guarantee holds even when _get_embedder and
    _embedding_dim call in concurrently from more than one thread. fastembed's
    registry is process-global and this codebase calls into it from more than
    one place across more than one SearchEngine instance per process, so a
    call here after the first is the normal case, not an edge case.
    """
    spec = _CUSTOM_MODELS.get(model_name)
    if spec is None:
        return
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    with _custom_model_registration_lock:
        already = any(
            m.get("model") == model_name for m in TextEmbedding.list_supported_models()
        )
        if already:
            return
        TextEmbedding.add_custom_model(
            model=model_name,
            pooling=PoolingType[spec["pooling"]],
            normalization=spec["normalization"],
            sources=ModelSource(hf=spec["hf"]),
            dim=spec["dim"],
            model_file=spec["model_file"],
            description=spec.get("description", ""),
            license=spec.get("license", ""),
        )


_MODEL_PREFIXES: dict[str, tuple[str, str]] = {
    "intfloat/multilingual-e5-large": ("query: ", "passage: "),
    _E5_SMALL_INT8_MODEL_NAME: ("query: ", "passage: "),
}
"""model name -> (query_prefix, doc_prefix). fastembed 0.8.0 carries prefix
advice only as free text inside each model's metadata `description`, and
TextEmbedding.query_embed/passage_embed (which WOULD parse it) are passthrough
wrappers this codebase never calls (_embed_query/_embed_documents call
.embed() directly) — so this table is filled in by hand, per model, from its
card. A model missing here gets ("", "") — no prefix, text unchanged."""


def _model_prefixes(model_name: str) -> tuple[str, str]:
    return _MODEL_PREFIXES.get(model_name, ("", ""))


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    """v / ‖v‖₂, in place of a divide-by-zero for the zero vector (returned
    unchanged rather than raising — reachable from a degenerate fake embedder
    in a test, never from a real model, but cheaper to special-case than to
    forbid). This is THE single point both the write path
    (_embed_documents -> index_note/index_all) and the read path
    (_embed_query -> search_vector) pass every vector through: fastembed 0.8.0
    normalises the OUTPUT of only some models (bge-small: already unit,
    measured 1.0000; the multilingual candidates: 2.0-28, not unit), and
    notes_vec is declared without `distance_metric` (vec0 default: raw L2), so
    an unnormalised candidate would be scored with a handicap unless every
    vector that reaches notes_vec — and every query vector compared against
    it — is rescaled here, unconditionally."""
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return vec / norm


def _resolve_model_name(storage: "Storage") -> str:
    """The embedding model bound to this vault's index — schema_version.
    embedding_model if the DB has it, else the default (З3).

    Deliberately NOT the SYMBIOSIS_BRAIN_EMBED_MODEL env var: hook and CLI
    subprocesses receive their environment from CLAUDE_ENV_FILE (a file the
    SessionStart hook drops), not from the env the MCP registration launched
    the server with, so a hook process reading the var directly could pick a
    model the server has not (yet, or ever) migrated the index to — embedding
    the query with a model the stored vectors were never built from, silently
    degrading exactly the retrieval-quality signal this whole feature exists
    to measure honestly. The var is a REQUEST, applied only by server.py at
    startup (under _reindex_lock, alongside the migration it triggers); once
    applied, it is written here, to the DB, where every process — server,
    hooks, CLI — reads it from, in agreement.
    """
    try:
        stored = storage.get_schema_version("embedding_model")
    except Exception:
        stored = None
    return stored if isinstance(stored, str) and stored else _MODEL_NAME


def _embedding_dim(model_name: str) -> int:
    """Vector width for `model_name`, from fastembed's own model metadata —
    TextEmbedding.get_embedding_size is a pure lookup: no network call, no
    weights loaded. If fastembed has no metadata for the name (typo, a model
    retired from its registry, a name a newer version of this package would
    know), we log an error and fall back to _DEFAULT_EMBEDDING_DIM rather than
    raising — notes_vec must always end up with SOME valid table, even
    mid-upgrade, and a bad model string in the DB must not fail the whole
    server's startup.

    A ValueError from _ensure_custom_model_registered is handled separately
    from a genuinely unknown model: _ensure_custom_model_registered's own
    check-then-act is locked (_custom_model_registration_lock), but that lock
    cannot cover code outside this module that might call
    TextEmbedding.add_custom_model directly for the same name between our
    check and our call — 'already registered' then means the model IS known,
    just not by way of our own call, so we retry the lookup once instead of
    treating it as an unknown model and silently returning the wrong
    dimension for it."""
    def _unknown_model_fallback() -> int:
        logger.error(
            "Unknown embedding model %r — fastembed has no size metadata for "
            "it. Falling back to the default dimension (%d, %s's).",
            model_name, _DEFAULT_EMBEDDING_DIM, _DEFAULT_MODEL_NAME, exc_info=True)
        return _DEFAULT_EMBEDDING_DIM

    try:
        _ensure_custom_model_registered(model_name)
        from fastembed import TextEmbedding
        return TextEmbedding.get_embedding_size(model_name)
    except ValueError:
        try:
            from fastembed import TextEmbedding
            return TextEmbedding.get_embedding_size(model_name)
        except Exception:
            return _unknown_model_fallback()
    except Exception:
        return _unknown_model_fallback()

LOCK_DIR = Path(tempfile.gettempdir())
_FASTEMBED_LOCK_TIMEOUT_S = 120

_REINDEX_LOCK_WAIT_S = 180
"""How long we queue for the reindex lock before proceeding unguarded.
Giving up costs duplicated work — exactly the pre-fix behaviour — never a
hang."""

_MODEL_DRIFT_RECHECK_S = 30.0
"""How often a live SearchEngine re-checks that the index it queries still
belongs to the model it embeds with (SearchEngine._check_model_drift). One
indexed read of schema_version, so the interval exists to keep the query path
tidy, not because the read is expensive."""

_REINDEX_LOCK_STALE_S = 1800
"""When an unattended lock file is broken as abandoned. Orphaned locks are
the NORMAL case, not the exception: _run_server force-exits via os._exit(0)
(server.py:870) whenever the parent Claude window dies, so a holder that is
mid-index_all never runs its finally."""

_SCOPE_BOOST = 1.5
"""Multiplier applied to RRF scores of notes whose scope matches the query scope.

Promotes scope-specific matches above otherwise-equal global matches when a
non-global scope filter is set. RRF scores for adjacent top ranks sit around
1/(60+1) ≈ 0.016; a 1.5× boost is large enough to flip ties and small gaps
without overwhelming genuinely stronger matches from the global pool.
Tunable — see `docs/superpowers/plans/2026-04-21-w4-lint-data-hygiene.md`.
"""

FTS_MODE_ANY = "any"
"""Лексическая половина: OR по токенам — «хотя бы одно слово»."""

FTS_MODE_ALL = "all"
"""Лексическая половина: AND по токенам. Историческое поведение и дефолт
_sanitize_fts_query/search_fts: неизвестный режим не должен молча РАСШИРЯТЬ
выдачу."""

FTS_MODE_ALL_THEN_ANY = "all_then_any"
"""AND, а при нуле строк лексической половины — повтор в OR.

Только ЗАПРОШЕННЫЙ режим: наружу (в `stats` и в журнал) уходит ИСХОД —
FTS_MODE_ALL или FTS_EFFECTIVE_FALLBACK_ANY (§2.9, §4.2). Замер 26.08 на 1666
живых запросах: на инжект-путях этот режим вырождается в OR в 97-99 % случаев,
поэтому митигацией precision он не считается (§12, риск 6)."""

FTS_EFFECTIVE_FALLBACK_ANY = "fallback_any"
"""ИСХОД, а не режим запроса: AND дал ноль строк, и мы повторили в OR.
Легально только в `stats["fts_mode"]` и в колонке `retrieval_event.fts_mode`;
передавать это значение в `search(fts_mode=…)` нельзя — там оно трактуется как
неизвестный режим, то есть как AND."""

_STRONG_RANK_MAX = 3
"""★ = нота в топ-3 ОБЕИХ половин (§4.5, I-22). Замер на реальном профиле
рендера PreToolUse (пул 6, кап 3, 142 живых запроса): 3,5 % показанных хитов
против 27,5 % у прежнего `_in_both`. Порог зависит от размера пула — меняя
`hit_limit`, перечитай решение о метке силы (2026-08-26) в vault владельца."""

OVERFETCH_FACTORS = (2, 8, 32)
"""Множители `limit` для KNN в scoped-выдаче (I-20, §4.4).

vec0 не умеет предфильтр: `notes_vec` объявлена как
`vec0(path TEXT PRIMARY KEY, embedding FLOAT[N])`, N — размерность текущей
модели (_embedding_dim, 384 для дефолтной BAAI/bge-small-en-v1.5), метаданной
для партиционирования там нет, и джойн с `notes` по scope даёт побайтно тот же
результат, что постфильтр — замерено (EXPLAIN QUERY PLAN: SCAN v VIRTUAL TABLE,
затем SEARCH n). Поэтому лестница: берём глобальный топ-k, фильтруем, и при
недоборе увеличиваем k. Первый шаг равен сегодняшнему `limit * 2`, то есть
незаскоупленная выдача поведения не меняет. Цена третьего шага честная:
limit*32 при limit=12 — KNN на 384 кандидата, 2,3-6,3 мс против 10,2 мс всей
векторной половины ([отчёт 03, F31]); он срабатывает только на узких скоупах.

Оговорка: 2,3-6,3 мс — цена самого KNN. Материализация нот (storage.get_note на
каждый выживший путь ступени, SELECT * вместе с content) в замер не входит и на
узком скоупе доходит до limit*32 полных нот за один вызов search()."""


def _extract_fallback_gist(content: str, max_chars: int = 80) -> str:
    """Extract first non-empty paragraph after frontmatter+heading, ≤max_chars.

    Used as fallback when frontmatter has no `gist:` field.
    """
    lines = content.split("\n")
    in_frontmatter = False
    in_para = False
    para_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if not stripped:
            if in_para:
                break  # paragraph ended
            continue
        if stripped.startswith("#"):
            continue  # skip headings
        para_lines.append(stripped)
        in_para = True
    paragraph = " ".join(para_lines).strip()
    if len(paragraph) > max_chars:
        cut = paragraph[:max_chars]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        paragraph = cut
    return paragraph


def _get_embedder():
    global _embedder
    if _embedder is not None:
        return _embedder

    lockfile = LOCK_DIR / "sb-fastembed-init.lock"
    deadline = time.time() + _FASTEMBED_LOCK_TIMEOUT_S
    acquired = False

    while not acquired:
        try:
            fd = os.open(str(lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()}\n{int(time.time())}\n")
            acquired = True
        except FileExistsError:
            try:
                age = time.time() - lockfile.stat().st_mtime
            except FileNotFoundError:
                continue
            except OSError:
                # Unreadable rather than gone: on Windows a live handle raises
                # PermissionError (WinError 32), which IS an OSError but NOT a
                # FileNotFoundError — pre-fix it escaped and killed the cold
                # start. Treat the lock as fresh and fall through to the wait.
                # NEVER `continue` here: a persistent error would busy-loop.
                logger.warning("fastembed lock: cannot stat lock file", exc_info=True)
                age = 0.0
            if age > _FASTEMBED_LOCK_TIMEOUT_S:
                try:
                    lockfile.unlink()
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    # Undeletable, not absent — same Windows shape as above.
                    # Fall through to the wait instead of retrying immediately.
                    logger.warning("fastembed lock: cannot remove stale lock", exc_info=True)
            if time.time() >= deadline:
                # Lock held too long — give up, attempt unguarded init.
                # Worst case: parallel cold-starts compete; not a hang.
                break
            time.sleep(0.1)

    try:
        if _embedder is None:
            _ensure_custom_model_registered(_MODEL_NAME)
            from fastembed import TextEmbedding
            _embedder = TextEmbedding(model_name=_MODEL_NAME)
    finally:
        if acquired:
            try:
                lockfile.unlink()
            except OSError:
                # Cleanup is best-effort: a stale file is broken later by the
                # staleness check, but an exception here would replace whatever
                # the guarded body raised.
                pass
    return _embedder


@contextmanager
def _reindex_lock(db_path):
    """Cross-process single-flight for reindex work on one vault DB.

    Same O_CREAT|O_EXCL idiom as _get_embedder's model lock. Not reentrant —
    never call while already holding it. On wait-timeout we proceed unguarded
    (worst case: duplicated work, never a hang). Callers MUST re-check their
    trigger condition after acquiring — the previous holder usually just did
    the work we queued up for. No PID-liveness probing: os.kill(pid, 0) on
    Windows calls TerminateProcess.
    """
    tag = hashlib.sha256(str(db_path).encode()).hexdigest()[:12]
    lockfile = LOCK_DIR / f"sb-reindex-{tag}.lock"
    deadline = time.time() + _REINDEX_LOCK_WAIT_S
    acquired = False
    while not acquired:
        try:
            fd = os.open(str(lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()}\n{int(time.time())}\n")
            acquired = True
        except FileExistsError:
            try:
                mtime = lockfile.stat().st_mtime
            except FileNotFoundError:
                continue
            except OSError:
                # Unreadable rather than gone — a live handle on Windows raises
                # PermissionError, an OSError but not a FileNotFoundError, and it
                # used to escape this loop and kill the caller mid-startup. Treat
                # the lock as fresh and fall through to the wait below; never
                # `continue` here, or a persistent error busy-loops the thread.
                logger.warning("reindex lock: cannot stat lock file", exc_info=True)
                mtime = time.time()
            if time.time() - mtime > _REINDEX_LOCK_STALE_S:
                # Re-stat immediately before unlinking: if the mtime moved
                # since we judged staleness, another process already broke
                # and re-acquired this lock — it is now live, not abandoned.
                # Skip the unlink and re-evaluate from scratch.
                try:
                    if lockfile.stat().st_mtime == mtime:
                        lockfile.unlink()
                        logger.warning(
                            "reindex lock: broke stale lock (age > %ds)",
                            _REINDEX_LOCK_STALE_S)
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    # Undeletable (live handle). Fall through to the wait.
                    logger.warning("reindex lock: cannot break stale lock", exc_info=True)
            if time.time() >= deadline:
                break
            time.sleep(0.5)
    if not acquired:
        logger.warning(
            "reindex lock: gave up waiting after %ds — proceeding unguarded (pid=%d)",
            _REINDEX_LOCK_WAIT_S, os.getpid())
    try:
        yield
    finally:
        if acquired:
            try:
                lockfile.unlink()
            except OSError:
                # Best-effort release: an exception raised inside `finally`
                # would replace the exception the guarded body raised (B6c).
                pass


def _embed(texts: list[str]) -> list[list[float]]:
    """Legacy no-model-argument batch embed: loads/uses whichever model
    _MODEL_NAME currently names (see its docstring) and L2-normalises every
    vector it returns (§ _l2_normalize) before handing it back. This is the
    function tools/eval_search.py monkeypatches wholesale to fake the model
    out in tests; _embed_documents (below) is what production code actually
    calls, and it delegates here after applying its own model-aware prefix,
    which is what keeps that monkeypatch effective for both."""
    embedder = _get_embedder()
    raw = embedder.embed(texts, batch_size=_EMBED_BATCH_SIZE)
    return [_l2_normalize(np.asarray(e, dtype=np.float32)).tolist() for e in raw]


def _embed_one(text: str) -> list[float]:
    return _embed([text])[0]


def _set_active_model(model_name: str) -> None:
    """Point the legacy _MODEL_NAME/_embedder singleton at `model_name`,
    dropping the warm embedder if it names a different model than the one
    already loaded. A no-op in the overwhelmingly common case (a process runs
    exactly one model for its whole lifetime, so this only ever fires once,
    if at all) — never called from __init__, only lazily from
    _embed_documents/_embed_query, right before the first real embed call."""
    global _MODEL_NAME, _embedder
    if _MODEL_NAME != model_name:
        _MODEL_NAME = model_name
        _embedder = None


def _embed_documents(texts: list[str], model_name: str) -> list[list[float]]:
    """Document/write-path embedding for `model_name` (З4): its doc_prefix
    (empty for bge-small and mpnet, "passage: " for e5-large) is prepended to
    every text before it reaches the model; text is passed through unchanged
    when there is no prefix, so a model with no prefix is byte-identical to
    the pre-switchable-embedder behaviour. Delegates to _embed (normalisation
    included) so a test/tool that monkeypatches _embed still sees every call
    this makes."""
    _set_active_model(model_name)
    _, doc_prefix = _model_prefixes(model_name)
    prefixed = [f"{doc_prefix}{t}" for t in texts] if doc_prefix else texts
    return _embed(prefixed)


def _embed_query(text: str, model_name: str) -> list[float]:
    """Query/read-path embedding for `model_name` (З4) — mirrors
    _embed_documents but with the model's query_prefix, so a prefixed model
    never embeds a query and a document with the same text (they need
    different prefixes; that is why this is a separate function rather than
    _embed_documents([text], ...)[0])."""
    _set_active_model(model_name)
    query_prefix, _ = _model_prefixes(model_name)
    prefixed = f"{query_prefix}{text}" if query_prefix else text
    return _embed_one(prefixed)


def _model_loadable(model_name: str) -> bool:
    """Best-effort smoke test: can `model_name` actually be loaded and used
    to embed something, right now? Used ONLY by server.py's model-change
    migration, and ONLY before it touches anything durable — a bad model
    name, a first-download network failure, or a full disk must be caught
    here, while the OLD, working notes_vec is still intact, rather than
    discovered by index_all() after _recreate_vec_table() has already
    dropped it. False on any exception; the caller is expected to log and
    abandon the migration, not propagate."""
    try:
        _embed_documents(["symbiosis-brain embedding model smoke test"], model_name)
        return True
    except Exception:
        logger.error(
            "Embedding model %r failed to load — aborting the model-change "
            "migration and keeping the existing index untouched.",
            model_name, exc_info=True)
        return False


DEDUP_TOP_K = 5
"""Сколько кандидатов просим у поиска. Ручкой НЕ становится (I-35): влияет на
цену запроса, а не на шум."""

DEDUP_CONTAINMENT_MIN = 0.5
"""Порог лексического покрытия. Замер 26.08 на 250 случайных нотах живого
vault (1465 нот): `containment >= 0.5` вместе с `_in_both` срабатывает на 4,4 %
записей; 0,4 даёт 17,6 % (без `_in_both`), 0,6 — 1,6 %. Голый `_in_both` при
режиме `any` — 52,0 %, то есть беспороговый признак негоден. Порог по
РАССТОЯНИЮ негоден тоже: p1=0,396, p50=0,588, и на нижнем хвосте сидят не
дубли, а архивные handoff'ы разных проектов (§5.3)."""

DEDUP_MAX_SHOWN = 2
"""Сколько путей печатаем. Больше двух — хвост ответа, который перестают
читать (§12, риск 11)."""

_DEDUP_MIN_ENV = "SYMBIOSIS_BRAIN_DEDUP_MIN"
_DEDUP_MAX_SHOWN_ENV = "SYMBIOSIS_BRAIN_DEDUP_MAX_SHOWN"

_DEDUP_ENV_CACHE: dict[str, object] = {}
"""Ручки читаются ОДИН раз на процесс (I-35, образец I-5). Тесты очищают этот
словарь; в рантайме он живёт до конца процесса."""

_DEDUP_WORD = re.compile(r"[0-9A-Za-zЀ-ӿ]+")
"""Цифры, латиница, кириллица U+0400..U+04FF; всё прочее — разделитель."""

_DEDUP_STOP = frozenset({
    "the", "a", "an", "of", "for", "and", "to", "in", "on", "is",
    "не", "и", "в", "на", "по", "с", "для", "что", "как", "это", "из", "за",
})


def _dedup_tokens(text: str) -> frozenset[str]:
    """Нормализованные токены `title + gist` (I-24, дословно из §5.3).

    Порядок операций зафиксирован и является частью контракта: `findall` →
    фильтр `len(t) > 2` по СЫРОМУ токену → `lower()` → отсев по стоп-листу.
    Стемминга нет, нормализации Unicode нет, дедупликация — обычным set.
    Именно этот код дал таблицу частот §5.3; тест детерминированности в
    tests/test_search.py существует для того, чтобы «улучшение» нормализации
    краснело, а не сдвигало частоту срабатывания молча."""
    return frozenset(
        t.lower() for t in _DEDUP_WORD.findall(text or "")
        if len(t) > 2 and t.lower() not in _DEDUP_STOP
    )


def containment(a: frozenset[str], b: frozenset[str]) -> float:
    """|A ∩ B| / min(|A|, |B|); 0.0 при пустой стороне (I-24).

    Не Jaccard: короткий гист-дубль длинной ноты обязан давать высокое
    покрытие, а Jaccard наказал бы его за разницу длин."""
    smaller = min(len(a), len(b))
    if smaller == 0:
        return 0.0
    return len(a & b) / smaller


def _dedup_min() -> float:
    """Порог из окружения; `0` выключает сигнал целиком (I-35)."""
    if "min" not in _DEDUP_ENV_CACHE:
        value = DEDUP_CONTAINMENT_MIN
        try:
            parsed = float(os.environ.get(_DEDUP_MIN_ENV, ""))
            if 0.0 <= parsed <= 1.0:
                value = parsed
        except (TypeError, ValueError):
            pass  # нечитаемое значение = дефолт (fail-open, как у I-5)
        _DEDUP_ENV_CACHE["min"] = value
    return float(_DEDUP_ENV_CACHE["min"])


def _dedup_max_shown() -> int:
    """Сколько путей печатать; `0` тоже выключает (I-35)."""
    if "max_shown" not in _DEDUP_ENV_CACHE:
        value = DEDUP_MAX_SHOWN
        try:
            parsed = int(os.environ.get(_DEDUP_MAX_SHOWN_ENV, ""))
            if parsed >= 0:
                value = parsed
        except (TypeError, ValueError):
            pass
        _DEDUP_ENV_CACHE["max_shown"] = value
    return int(_DEDUP_ENV_CACHE["max_shown"])


def _dedup_canonical(path: str | None) -> str:
    """Путь для сравнения «это же я»: слэши вперёд, без `.md`."""
    return (path or "").strip().replace("\\", "/").removesuffix(".md")


def dedup_candidates(engine, storage, *, title: str, gist: str,
                     self_path: str | None, top_k: int = DEDUP_TOP_K) -> list[dict]:
    """Notes that look like duplicates of the one about to be written (I-23).

    Returns at most DEDUP_MAX_SHOWN dicts {path, gist, containment}, sorted by
    containment DESC. NEVER raises: this runs on the write path, and a hint is
    never worth failing a save for (§5.4).

    Two conditions, both required (§5.3): the candidate surfaced in BOTH search
    halves (`_in_both`), and the lexical containment of the new note's
    `title + gist` in the candidate's is at least the threshold. Bare `_in_both`
    fired on 52 % of writes once the lexical half started ORing; a distance
    threshold is useless on this embedder (p1=0.396 vs p50=0.588, and the low
    tail is archived handoffs, not duplicates).

    `storage` is the fallback source for a candidate's title/gist when the
    engine returned rows without them; the search this function issues is NOT
    logged — the journal knows exactly six retrieval paths (§2.1) and this is
    not one of them.
    """
    try:
        min_containment = _dedup_min()
        max_shown = _dedup_max_shown()
        if min_containment <= 0 or max_shown <= 0:
            return []                       # I-35: 0 = выключено целиком
        query = f"{title or ''} {gist or ''}".strip()
        if not query:
            return []
        mine = _dedup_tokens(query)
        if not mine:
            return []
        self_key = _dedup_canonical(self_path)
        found: list[dict] = []
        for hit in engine.search(query=query, scope=None, limit=top_k,
                                 mode="gist", fts_mode=FTS_MODE_ANY):
            path = hit.get("path") or ""
            if not path or _dedup_canonical(path) == self_key:
                continue
            if not hit.get("_in_both"):
                continue
            their_title = hit.get("title") or ""
            their_gist = hit.get("gist") or ""
            if (not their_title or not their_gist) and storage is not None:
                note = storage.get_note(path) or {}
                fm = note.get("frontmatter") or {}
                their_title = their_title or (note.get("title") or "")
                if not their_gist and isinstance(fm, dict):
                    their_gist = str(fm.get("gist") or "")
            score = containment(mine, _dedup_tokens(f"{their_title} {their_gist}"))
            if score < min_containment:
                continue
            found.append({"path": path, "gist": their_gist, "containment": score})
        found.sort(key=lambda c: (-c["containment"], c["path"]))
        return found[:max_shown]
    except Exception:
        # Fail-open, and quiet: the caller prints nothing when we return [].
        logger.debug("dedup_candidates: skipped", exc_info=True)
        return []


class SearchEngine:
    def __init__(self, storage: Storage, *, model_name: str | None = None):
        """`model_name`, when given, overrides DB resolution outright (used by
        server.py's model-change migration, which must act on the requested
        model before it has written that name to the DB, and by
        tools/eval_search.py, which needs to point a work-copy DB at a
        candidate model regardless of what its (copied-from-the-live-vault)
        schema_version row says). Every other caller omits it and gets
        _resolve_model_name's DB-else-default resolution (З3)."""
        self.storage = storage
        self.model_name = model_name or _resolve_model_name(storage)
        self._model_pinned = model_name is not None
        self._drift_checked_at = time.monotonic()
        self._vec_enabled = self._try_load_vec()

    @property
    def _model_name(self) -> str:
        return self.model_name

    def _check_model_drift(self) -> None:
        """Stop querying an index that no longer belongs to our model.

        We resolve the model once, at construction, and a server process can
        live for hours. Meanwhile another process — a second editor window, or
        a restart applying SYMBIOSIS_BRAIN_EMBED_MODEL — may migrate notes_vec
        to a different model underneath us. Embedding a query with the old
        model against the new vectors does not fail: it returns confident
        nonsense, and the retrieval log records it as an ordinary result. That
        is unrecoverable for a benchmark, so the vector half shuts down for
        this process and the search degrades to FTS until it restarts.

        Cheap enough to sit in the query path: one indexed read of
        schema_version, at most every _MODEL_DRIFT_RECHECK_S. A pinned model
        is exempt — eval_search.py aims a copied DB at a candidate on purpose,
        and server.py's migration builds its engine before writing the name.
        Fail-open on a read error: this is a guard, not a gate.
        """
        if self._model_pinned or not self._vec_enabled:
            return
        now = time.monotonic()
        if now - self._drift_checked_at < _MODEL_DRIFT_RECHECK_S:
            return
        self._drift_checked_at = now
        try:
            stored = self.storage.get_schema_version("embedding_model")
        except Exception:
            logger.debug("model drift check skipped", exc_info=True)
            return
        if isinstance(stored, str) and stored and stored != self.model_name:
            logger.error(
                "Vector index was migrated to %r while this process embeds "
                "with %r; disabling vector search here (FTS only) until "
                "restart, rather than scoring queries against foreign vectors.",
                stored, self.model_name)
            self._vec_enabled = False

    def _try_load_vec(self) -> bool:
        try:
            import sqlite_vec
            self.storage._conn.enable_load_extension(True)
            sqlite_vec.load(self.storage._conn)
            self.storage._conn.enable_load_extension(False)
            self._ensure_vec_table()
            return True
        except Exception:
            return False

    def _ensure_vec_table(self):
        """Create notes_vec, sized for self.model_name, if it doesn't exist
        yet. Never touches an EXISTING table — including one at the wrong
        dimension for self.model_name, which only happens mid-migration and
        is exactly what _recreate_vec_table (below) is for."""
        tables = self.storage.list_tables()
        if "notes_vec" not in tables:
            dim = _embedding_dim(self.model_name)
            self.storage._conn.execute(f"""
                CREATE VIRTUAL TABLE notes_vec USING vec0(
                    path TEXT PRIMARY KEY,
                    embedding FLOAT[{dim}]
                )
            """)
            self.storage._conn.commit()

    def _recreate_vec_table(self) -> None:
        """DROP + CREATE notes_vec for self.model_name's dimension, as ONE
        explicit transaction (BEGIN IMMEDIATE ... COMMIT) — deliberately NOT
        the two-autocommit DROP-then-CREATE idiom tools/eval_search.py uses
        for the same job. That idiom is fine for a one-shot CLI tool; it is
        not fine here: _run_server force-exits via os._exit(0) whenever the
        parent Claude window dies (server.py), and a kill landing between two
        autocommits would leave the vault with no notes_vec table at all.
        Used ONLY when the embedding model is CHANGING — every other case is
        covered by _ensure_vec_table's create-if-missing."""
        dim = _embedding_dim(self.model_name)
        self.storage._conn.execute("BEGIN IMMEDIATE")
        try:
            self.storage._conn.execute("DROP TABLE IF EXISTS notes_vec")
            self.storage._conn.execute(f"""
                CREATE VIRTUAL TABLE notes_vec USING vec0(
                    path TEXT PRIMARY KEY,
                    embedding FLOAT[{dim}]
                )
            """)
            self.storage._conn.execute("COMMIT")
        except Exception:
            try:
                self.storage._conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def index_note(self, path: str, content: str):
        if not self._vec_enabled:
            return
        embedding = _embed_documents([content], self.model_name)[0]
        # BEGIN IMMEDIATE serializes concurrent indexers on the same path.
        # vec0 does not support INSERT OR REPLACE, so DELETE + INSERT is the
        # only upsert pattern — wrapping it in an exclusive transaction prevents
        # the UNIQUE-constraint race that arises when two processes index the
        # same note simultaneously (e.g. parallel cold-starts on a fresh vault).
        self.storage._conn.execute("BEGIN IMMEDIATE")
        try:
            self.storage._conn.execute("DELETE FROM notes_vec WHERE path=?", (path,))
            self.storage._conn.execute(
                "INSERT INTO notes_vec (path, embedding) VALUES (?, ?)",
                (path, np.array(embedding, dtype=np.float32).tobytes()),
            )
            self.storage._conn.execute("COMMIT")
        except Exception:
            try:
                self.storage._conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def index_all(self):
        """Full rebuild of notes_vec from notes. Callers must hold
        _reindex_lock(storage.db_path)."""
        if not self._vec_enabled:
            return
        notes = self.storage.list_notes()
        if not notes:
            return
        texts = [f"{n['title']}\n{n['content']}" for n in notes]
        embeddings = _embed_documents(texts, self.model_name)
        # BEGIN IMMEDIATE serializes concurrent full rebuilds. Without this,
        # two parallel cold-starts both see a dirty index and both run the
        # DELETE + multi-INSERT loop, causing UNIQUE-constraint violations.
        self.storage._conn.execute("BEGIN IMMEDIATE")
        try:
            self.storage._conn.execute("DELETE FROM notes_vec")
            for note, emb in zip(notes, embeddings):
                self.storage._conn.execute(
                    "INSERT INTO notes_vec (path, embedding) VALUES (?, ?)",
                    (note["path"], np.array(emb, dtype=np.float32).tobytes()),
                )
            self.storage._conn.execute("COMMIT")
        except Exception:
            try:
                self.storage._conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def repair_index(self) -> dict:
        """Reconcile notes_vec with notes incrementally.

        Embeds only notes that lack a vector row and deletes orphan vector
        rows whose note is gone. Unlike index_all(), never touches rows that
        are already correct — a drift of K rows costs K _embed_one calls, not
        a full-corpus rebuild (measured 2026-08-10: drift=1 via index_all
        cost 503 CPU-s / 11.2 GB). The gate for doing nothing is "no missing
        and no orphan rows" (computed inside the lock), not is_index_dirty()'s
        bare count comparison — count alone misses balanced drift, e.g. an
        equal number of missing and orphaned rows from a historical rename,
        which would otherwise never get repaired. Residual race: if a note is
        deleted by another process between our `missing` snapshot and the
        embed, we may re-insert its vec row as an orphan; the next repair
        deletes it.
        """
        if not self._vec_enabled:
            return {"embedded": 0, "orphans_deleted": 0}
        with _reindex_lock(self.storage.db_path):
            missing = [r[0] for r in self.storage._conn.execute(
                "SELECT n.path FROM notes n LEFT JOIN notes_vec v ON v.path = n.path"
                " WHERE v.path IS NULL"
            ).fetchall()]
            orphans = [r[0] for r in self.storage._conn.execute(
                "SELECT v.path FROM notes_vec v LEFT JOIN notes n ON n.path = v.path"
                " WHERE n.path IS NULL"
            ).fetchall()]
            if not missing and not orphans:
                # Another process likely repaired while we queued for the
                # lock — re-checking the actual join lists (not just counts)
                # here is what makes this a correct double-checked-lock gate.
                return {"embedded": 0, "orphans_deleted": 0}
            for path in orphans:
                self.delete_vec(path)
            embedded = 0
            for path in missing:
                note = self.storage.get_note(path)
                if note is None:
                    continue
                self.index_note(path, f"{note['title']}\n{note['content']}")
                embedded += 1
            return {"embedded": embedded, "orphans_deleted": len(orphans)}

    def delete_vec(self, path: str) -> None:
        """Remove the vector embedding for a single note path."""
        if not self._vec_enabled:
            return
        self.storage._conn.execute("DELETE FROM notes_vec WHERE path=?", (path,))
        self.storage._conn.commit()

    def is_index_dirty(self) -> bool:
        """True if count(notes) != count(notes_vec). Cheap O(1) drift check."""
        if not self._vec_enabled:
            return False
        n = self.storage._conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        v = self.storage._conn.execute("SELECT COUNT(*) FROM notes_vec").fetchone()[0]
        return n != v

    def has_index_delta(self) -> bool:
        """True if any note lacks a notes_vec row OR any notes_vec row has no note.

        Two cheap `SELECT 1 ... LEFT JOIN ... WHERE ... IS NULL LIMIT 1` probes —
        the same joins repair_index() computes inside its lock. Unlike
        is_index_dirty()'s bare COUNT comparison this catches BALANCED drift
        (an equal number of missing and orphaned rows — the classic residue of a
        historical rename), which repair_index() is built to fix but the old
        count gate never let it see (B6a).

        Never raises: any DB error -> False. This runs on the cold-start path,
        where a failing drift probe must not take the whole server down with it.
        """
        if not self._vec_enabled:
            return False
        try:
            missing = self.storage._conn.execute(
                "SELECT 1 FROM notes n LEFT JOIN notes_vec v ON v.path = n.path"
                " WHERE v.path IS NULL LIMIT 1"
            ).fetchone()
            if missing is not None:
                return True
            orphan = self.storage._conn.execute(
                "SELECT 1 FROM notes_vec v LEFT JOIN notes n ON n.path = v.path"
                " WHERE n.path IS NULL LIMIT 1"
            ).fetchone()
            return orphan is not None
        except Exception:
            logger.warning("has_index_delta: probe failed, assuming clean", exc_info=True)
            return False

    @staticmethod
    def _sanitize_fts_query(query: str, mode: str = FTS_MODE_ALL) -> str:
        """Escape user input for FTS5 MATCH.

        Strips FTS5 operators and wraps each token in double quotes so that
        characters like hyphens, dots, and colons are treated as literals, not
        syntax. `mode` decides how the quoted tokens are joined:

        - FTS_MODE_ANY  -> ' OR ' between them: at least one token must match;
        - anything else -> a bare space, i.e. FTS5's implicit AND.

        AND stays the default on purpose: an unknown or stub mode must never
        silently WIDEN a query. Measured 2026-08-26 on 1666 live queries: the
        implicit AND returned zero rows for 84.5 % of them (94.3 % of the
        Russian ones), which is what FTS_MODE_ANY exists to fix (§4.1).

        Quoting also disarms the OR keyword itself: a user token `OR` arrives
        as `"OR"` and matches literally, never as an operator.
        """
        import re
        # Remove characters that are FTS5 operators or break the parser
        cleaned = re.sub(r'["\(\)\*\:\.\{\}\[\]\^\~\|]', ' ', query)
        tokens = cleaned.split()
        if not tokens:
            return '""'
        quoted = [f'"{t}"' for t in tokens]
        if mode == FTS_MODE_ANY:
            return " OR ".join(quoted)
        return " ".join(quoted)

    def search_fts(self, query: str, scope: str | None = None, limit: int = 10,
                   *, mode: str = FTS_MODE_ALL) -> list[dict]:
        fts_query = self._sanitize_fts_query(query, mode)
        if scope:
            rows = self.storage._conn.execute("""
                SELECT n.*, bm25(notes_fts, 10, 1, 1) as rank
                FROM notes_fts fts
                JOIN notes n ON n.rowid = fts.rowid
                WHERE notes_fts MATCH ? AND n.scope IN (?, 'global')
                ORDER BY rank
                LIMIT ?
            """, (fts_query, scope, limit)).fetchall()
        else:
            rows = self.storage._conn.execute("""
                SELECT n.*, bm25(notes_fts, 10, 1, 1) as rank
                FROM notes_fts fts
                JOIN notes n ON n.rowid = fts.rowid
                WHERE notes_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, limit)).fetchall()
        return [self.storage._row_to_note(r) for r in rows]

    def search_vector(self, query: str, scope: str | None = None, limit: int = 10) -> list[dict]:
        """Vector half. Signature unchanged (I-20) — the escalation is internal.

        A scoped call cannot be pre-filtered by vec0, so we over-fetch: pull the
        global top-k, post-filter by scope, and grow k when the scope came up
        short. Exactly two stop conditions — `limit` collected, or the corpus
        exhausted (`k >= count(notes_vec)`). Measured before this change: 25 %
        of scoped queries returned fewer notes than asked ([report 03, F21]).
        """
        self._check_model_drift()
        if not self._vec_enabled:
            return []
        q_blob = np.array(_embed_query(query, self.model_name), dtype=np.float32).tobytes()
        try:
            corpus = self.storage._conn.execute(
                "SELECT COUNT(*) FROM notes_vec"
            ).fetchone()[0]
        except Exception:
            # Fail-safe, not fail-open-wide: an unknown corpus size collapses the
            # ladder to its first rung, i.e. exactly the pre-Stage-2 behaviour.
            corpus = 0
        # Notes are memoised across rungs: without it the third rung re-reads
        # every path the first two already fetched.
        notes_by_path: dict[str, dict | None] = {}
        results: list[dict] = []
        for factor in OVERFETCH_FACTORS:
            k = max(1, limit * factor)
            rows = self.storage._conn.execute("""
                SELECT v.path, v.distance
                FROM notes_vec v
                WHERE v.embedding MATCH ?
                ORDER BY v.distance
                LIMIT ?
            """, (q_blob, k)).fetchall()

            results = []
            for row in rows:
                path = row[0]
                if path not in notes_by_path:
                    notes_by_path[path] = self.storage.get_note(path)
                note = notes_by_path[path]
                if note and (scope is None or note["scope"] in (scope, "global")):
                    note["_distance"] = row[1]
                    results.append(note)
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
            if k >= corpus:
                break
        return results

    def search(self, query: str, scope: str | None = None, limit: int = 10,
               mode: str = "preview", *, fts_mode: str = FTS_MODE_ANY,
               log_ctx: "LogContext | None" = None,
               stats: dict | None = None) -> list[dict]:
        """Hybrid search: FTS5 + vector with Reciprocal Rank Fusion.

        mode='preview' (default) — returns notes with full content for legacy callers.
        mode='gist' — adds 'gist' key (frontmatter['gist'] or fallback 80-char paragraph).

        fts_mode picks the LEXICAL half's token policy (I-17..I-19). What leaves
        this function — through `stats` and through the journal — is the
        EFFECTIVE mode: 'any' | 'all' | 'fallback_any'. The requested
        'all_then_any' is never written anywhere (§2.9, §4.2): the share of
        'fallback_any' IS the metric, and substituting the request for the
        outcome empties it silently.

        log_ctx (I-7): when given, this call IS the surfacing point and the
        retrieval log is written here, after _score/_in_both are attached and
        before the return. Used by `mcp_search` and `legacy_gist` only — the
        hook paths pass log_ctx=None and log from `run_recall` instead, because
        what the agent sees there is the list AFTER type filtering, SeenStore
        and the cap (§2.9). Default None keeps today's behaviour: nobody logs.

        stats (I-7): a write-only back channel for whoever logs from outside.
        Filled with EXACTLY two keys before every return, including an empty
        result — those are the cases the metric exists for. `fts_mode` is the
        EFFECTIVE mode, `vec_enabled` is the only legal source of the NOT NULL
        column of the same name on the hook paths (§2.9, I-8).
        """
        t0 = time.perf_counter()
        if fts_mode == FTS_MODE_ALL_THEN_ANY:
            fts_results = self.search_fts(query, scope=scope, limit=limit * 2,
                                          mode=FTS_MODE_ALL)
            if fts_results:
                effective_fts_mode = FTS_MODE_ALL
            else:
                fts_results = self.search_fts(query, scope=scope, limit=limit * 2,
                                              mode=FTS_MODE_ANY)
                effective_fts_mode = FTS_EFFECTIVE_FALLBACK_ANY
        else:
            # Unknown values fall back to AND, never to OR — see _sanitize_fts_query.
            effective_fts_mode = FTS_MODE_ANY if fts_mode == FTS_MODE_ANY else FTS_MODE_ALL
            fts_results = self.search_fts(query, scope=scope, limit=limit * 2,
                                          mode=effective_fts_mode)
        vec_results = self.search_vector(query, scope=scope, limit=limit * 2)

        scores: dict[str, float] = {}
        k = 60  # RRF constant

        for rank, note in enumerate(fts_results):
            scores[note["path"]] = scores.get(note["path"], 0) + 1.0 / (k + rank + 1)

        for rank, note in enumerate(vec_results):
            scores[note["path"]] = scores.get(note["path"], 0) + 1.0 / (k + rank + 1)

        all_notes = {n["path"]: n for n in fts_results + vec_results}

        if scope and scope != "global":
            for path in scores:
                note = all_notes.get(path)
                if note and note.get("scope") == scope:
                    scores[path] *= _SCOPE_BOOST

        sorted_paths = sorted(scores, key=lambda p: scores[p], reverse=True)
        results = [all_notes[p] for p in sorted_paths[:limit]]

        # Attach ranking metadata (Stage 0, recall-hardening; Stage 2, I-22).
        # Purely additive, underscore-prefixed (cf. _distance), visible to both
        # preview and gist callers. _score is post-boost RRF. _in_both means
        # the note surfaced in BOTH halves and KEEPS its old meaning — existing
        # tests stand on it and the journal logs it (retrieval_hit.in_both).
        # _strong is the NEW label behind ★: top-_STRONG_RANK_MAX in both
        # halves (§4.5). Neither is ever a filter
        # (see [[decisions/2026-06-03-recall-behavior]]).
        fts_rank_by_path: dict[str, int] = {}
        for rank, note in enumerate(fts_results):
            fts_rank_by_path.setdefault(note["path"], rank)
        vec_rank_by_path: dict[str, int] = {}
        for rank, note in enumerate(vec_results):
            vec_rank_by_path.setdefault(note["path"], rank)
        for note in results:
            note["_score"] = float(scores.get(note["path"], 0.0))
            fts_rank = fts_rank_by_path.get(note["path"])
            vec_rank = vec_rank_by_path.get(note["path"])
            note["_fts_rank"] = fts_rank
            note["_vec_rank"] = vec_rank
            note["_in_both"] = fts_rank is not None and vec_rank is not None
            note["_strong"] = (
                fts_rank is not None and fts_rank < _STRONG_RANK_MAX
                and vec_rank is not None and vec_rank < _STRONG_RANK_MAX
            )

        if mode == "gist":
            for note in results:
                fm = note.get("frontmatter") or {}
                gist = fm.get("gist", "").strip() if isinstance(fm, dict) else ""
                if not gist:
                    gist = _extract_fallback_gist(note["content"], max_chars=80)
                note["gist"] = gist

        latency_ms = int((time.perf_counter() - t0) * 1000)

        # This is the single return of search(). If a future change adds an
        # early one, it MUST fill `stats` and log first — that is the contract,
        # not a courtesy (I-7).
        if stats is not None:
            stats["fts_mode"] = effective_fts_mode
            stats["vec_enabled"] = bool(self._vec_enabled)
        if log_ctx is not None:
            retrieval_log.record(
                log_ctx, query=query, scope=scope, mode=mode,
                fts_mode=effective_fts_mode, hits=results,
                latency_ms=latency_ms, vec_enabled=bool(self._vec_enabled),
            )

        return results
