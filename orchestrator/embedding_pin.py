"""Pinned revisions for the embedding model and its remote code.

nomic-embed-text-v2-moe is loaded with trust_remote_code=True and its config
points (auto_map) at code in a SECOND repo, nomic-ai/nomic-bert-2048. Unpinned,
every start re-resolved that repo's `main` and executed whatever was there —
a supply-chain hole (the orchestrator holds HA/Google/API tokens) and a drift
risk. `revision` pins the model repo; `code_revision` (passed through
config_kwargs/model_kwargs) is what pins the cross-repo code — `revision`
alone does not. Dependency-free so scripts can import it too.

To upgrade: test the new SHAs in a throwaway container (encode must work and
"A new version of the following files was downloaded" must not appear),
then change both together. See also the transformers<5.12 pin in
orchestrator/requirements.txt.
"""

from typing import Any

DEFAULT_EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v2-moe"
EMBEDDING_MODEL_REVISION = "1066b6599d099fbb93dfcb64f9c37a7c9e503e85"  # nomic-embed-text-v2-moe
EMBEDDING_CODE_REVISION = "7710840340a098cfb869c4f65e87cf2b1b70caca"  # nomic-bert-2048 (remote code)


def load_kwargs(model_name: str) -> dict[str, Any]:
    """SentenceTransformer(...) kwargs: pinned for the default model, plain otherwise."""
    kwargs: dict[str, Any] = {"trust_remote_code": True}
    if model_name == DEFAULT_EMBEDDING_MODEL:
        kwargs["revision"] = EMBEDDING_MODEL_REVISION
        kwargs["config_kwargs"] = {"code_revision": EMBEDDING_CODE_REVISION}
        kwargs["model_kwargs"] = {"code_revision": EMBEDDING_CODE_REVISION}
    return kwargs
