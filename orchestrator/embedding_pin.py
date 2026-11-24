"""Pinned revisions for the embedding model and its remote code.

nomic-embed-text-v2-moe is loaded with trust_remote_code=True and its config
points (auto_map) at code in a SECOND repo, nomic-ai/nomic-bert-2048. Unpinned,
every start re-resolved that repo's `main` and executed whatever was there —
a supply-chain hole (the orchestrator holds HA/Google/API tokens) and a drift
risk. `revision` pins the model repo; `code_revision` pins the cross-repo code
— `revision` alone does not. It has to reach three loads (config, model,
processor), see load_kwargs(). Dependency-free at import so scripts can
import it too.

Until 2026-11-24 only the first of the three got it: sentence-transformers 5.7
pops model_kwargs["code_revision"] from the caller's dict while importing
module classes, and AutoProcessor re-loads the config without it — so the
remote code that ran (modeling_hf_nomic_bert.py, configuration_hf_nomic_bert.py)
came from `main` of the code repo, which happened to equal the pin.
remote_code_revisions() shows which commit really loaded; shared.py and
`check` refuse anything but the pin.

Offline at runtime: the orchestrator image sets HF_HUB_OFFLINE=1, so the
model loads from the HF cache ($HF_HOME = the data/hf_cache bind mount) and
never calls the Hub (local_files_only alone still lets one HEAD out). Online,
every start sent HEAD requests per file; with DNS down they retried with
backoff for ~2.5 min before uvicorn listened (a deploy health check failed on
it, 2026-11-23). Offline, a model missing from the cache fails the load in
seconds: shared.py starts with memory off, the startup self-test reports
FAILED and EmbeddingModelBroken pages.

The CLI below is the only thing that goes online (run it inside the
orchestrator container, or a throwaway container from its image):

    python -m orchestrator.embedding_pin fetch   # download, then prove an offline load works
    python -m orchestrator.embedding_pin check   # offline load + one encode + commit test; exit 1 if it fails

e.g. `docker exec brain-orchestrator python -m orchestrator.embedding_pin
fetch`, then `docker restart brain-orchestrator`. install.sh does this on a
fresh install. Both use $EMBEDDING_MODEL (else the default) unless --model is
given. For the default model `fetch` only downloads the pinned commits, the
same code every start already imports; a custom model's remote code is
unpinned, so fetch that one in a throwaway container with only the cache
mounted (no .env, no credentials).

To upgrade (both SHAs together; see also the transformers<5.12 pin in
orchestrator/requirements.txt):
1. Test the new SHAs in a throwaway container against an EMPTY scratch cache:
   docker run --rm -v "$PWD/orchestrator:/app/orchestrator:ro"
   -v <empty dir>:/home/jess/.cache/huggingface <orchestrator image>
   python -m orchestrator.embedding_pin fetch
   — it must end in "check: OK" and must not print "A new version of the
   following files was downloaded" (that means some load was not pinned).
2. Before merging, run the same `fetch` with the live data/hf_cache mounted
   instead (it adds the new snapshots beside the old ones). Skip this and the
   deploy's pre-flight `check` (ci.yml) fails before the orchestrator is
   recreated. A transformers or sentence-transformers bump: the pre-flight
   runs `check` from the new image against the live cache too (a newer
   version may look for files the cache lacks, or drop code_revision again).
"""

import argparse
import os
import re
import subprocess
import sys
from typing import Any

DEFAULT_EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v2-moe"
EMBEDDING_MODEL_REVISION = "1066b6599d099fbb93dfcb64f9c37a7c9e503e85"  # nomic-embed-text-v2-moe
EMBEDDING_CODE_REVISION = "7710840340a098cfb869c4f65e87cf2b1b70caca"  # nomic-bert-2048 (remote code)

# huggingface_hub reads this once, when it is first imported.
OFFLINE_ENV = "HF_HUB_OFFLINE"
# transformers imports remote code as transformers_modules.<org>.<repo>.<commit>.<file>;
# the optional "_" allows for a future version sanitizing a leading digit.
_COMMIT_PART = re.compile(r"_?([0-9a-f]{40})")


class _KeepCodeRevision(dict):
    """model_kwargs that keep "code_revision" through sentence-transformers' pop.

    ST 5.7 (SentenceTransformer._load_module_class_from_ref) does
    ``model_kwargs.pop("code_revision")`` on the dict we pass, before the
    Transformer module hands model_kwargs to AutoModel.from_pretrained — so a
    plain dict reaches the model load without it and the modeling code
    resolves `main`. Harmless if a later version copies the dict first.
    """

    def pop(self, key: Any, *default: Any) -> Any:
        if key == "code_revision" and key in self:
            return self[key]
        return super().pop(key, *default)


def load_kwargs(model_name: str) -> dict[str, Any]:
    """SentenceTransformer(...) kwargs: pinned for the default model, plain otherwise."""
    kwargs: dict[str, Any] = {"trust_remote_code": True}
    if model_name == DEFAULT_EMBEDDING_MODEL:
        kwargs["revision"] = EMBEDDING_MODEL_REVISION
        kwargs["config_kwargs"] = {"code_revision": EMBEDDING_CODE_REVISION}
        kwargs["model_kwargs"] = _KeepCodeRevision(code_revision=EMBEDDING_CODE_REVISION)
        # AutoProcessor loads the config (and its remote class) again.
        kwargs["processor_kwargs"] = {"code_revision": EMBEDDING_CODE_REVISION}
    return kwargs


def remote_code_revisions() -> set[str]:
    """Commits of every trust_remote_code module imported in this process so far.

    Covers the config class too (not an nn.Module). Call it right after the
    model load; the orchestrator loads no other remote-code model.
    """
    found: set[str] = set()
    for name in list(sys.modules):
        if name.startswith("transformers_modules."):
            for part in name.split("."):
                m = _COMMIT_PART.fullmatch(part)
                if m:
                    found.add(m.group(1))
    return found


def _load_and_encode(model_name: str) -> tuple[int, set[str]]:
    """Load the model the way shared.py does and encode one sentence.

    Returns the vector dimension and the remote-code commits that loaded.
    """
    from sentence_transformers import SentenceTransformer  # heavy: CLI only

    model = SentenceTransformer(model_name, **load_kwargs(model_name))
    return len(model.encode("self-test", normalize_embeddings=True)), remote_code_revisions()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.embedding_pin",
        description="Populate (fetch) or verify (check) the embedding model in the local HF cache.",
    )
    parser.add_argument("action", choices=("fetch", "check"))
    parser.add_argument("--model", help="model id (default: $EMBEDDING_MODEL, else the pinned default)")
    args = parser.parse_args(argv)
    model = args.model or os.environ.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
    offline = args.action == "check"

    hub = sys.modules.get("huggingface_hub.constants")
    if hub is not None and bool(hub.HF_HUB_OFFLINE) != offline:
        print(f"{args.action}: huggingface_hub is already imported in this process; run it on its own", file=sys.stderr)
        return 2
    os.environ[OFFLINE_ENV] = "1" if offline else "0"

    try:
        dim, code_revisions = _load_and_encode(model)
    except Exception as e:
        print(f"{args.action}: FAILED to load {model}: {type(e).__name__}: {e}", file=sys.stderr)
        if offline:
            print("Not in the HF cache? Run: python -m orchestrator.embedding_pin fetch", file=sys.stderr)
        return 1

    if offline:
        if model == DEFAULT_EMBEDDING_MODEL and code_revisions != {EMBEDDING_CODE_REVISION}:
            print(
                f"check: FAILED — the remote code loaded from {sorted(code_revisions) or 'no commit'}, "
                f"not the pinned {EMBEDDING_CODE_REVISION}",
                file=sys.stderr,
            )
            return 1
        print(f"check: OK — {model} loads offline from the cache ({dim}-dim)")
        return 0
    print(f"fetch: {model} is in the cache; checking that it loads offline...", flush=True)
    # A fresh process: the offline flag only takes effect at import.
    cmd = [sys.executable, "-m", "orchestrator.embedding_pin", "check", "--model", model]
    return subprocess.run(cmd, env={**os.environ, OFFLINE_ENV: "1"}, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
