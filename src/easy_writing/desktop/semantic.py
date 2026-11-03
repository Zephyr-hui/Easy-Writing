"""在用户明确选定的本机 Python 进程中执行 BGE 对话。"""

import json
import sys

from easy_writing.desktop.backend import DesktopBackend, expected_error
from easy_writing.retrieval.embeddings import BGEEmbeddingProvider
from easy_writing.services.conversation import ChatRequest


def main():
    try:
        payload = json.load(sys.stdin)
        backend = DesktopBackend(payload["data_dir"], payload["env_file"])
        settings = backend.settings
        repo, chat = backend.open(payload["knowledge"], payload["chat"])
        provider = BGEEmbeddingProvider(settings.embedding_model, cache_folder=settings.embedding_cache,
                                        revision=settings.embedding_revision)
        result = backend.ask(chat, payload["project"], payload["session"], ChatRequest.model_validate(payload["request"]),
                             embedding=provider)
        print(json.dumps({"result": result}, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"error": expected_error(error)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
