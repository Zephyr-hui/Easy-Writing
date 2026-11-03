"""共享的工作台模型适配器。"""

from easy_writing.usage import JsonlCallRecorder
from easy_writing.storage.repository import digest


def recorder(settings):
    return JsonlCallRecorder(settings.usage_log, secrets=(settings.api_key.get_secret_value(),))


def workbench_import_identity(settings, fixture_text=None):
    if fixture_text is not None:
        return "ui-fixture:" + digest(fixture_text)
    return ":".join(("ui-deepseek", settings.base_url, settings.extraction_model, settings.extraction_thinking))


def client(settings, provider, *, planning=False, output_tokens=3072, call_recorder=None):
    target = call_recorder or recorder(settings)
    if provider == "fake":
        if planning:
            from easy_writing.services.planning import DemoPlanningClient
            return DemoPlanningClient()
        from easy_writing.retrieval.workflow import DemoResearchClient
        return DemoResearchClient(target)
    from easy_writing.llm.deepseek import DeepSeekClient

    values = {"max_retries": 0, "max_output_tokens": output_tokens}
    if planning:
        values.update(extraction_model=settings.analysis_model, extraction_thinking=settings.analysis_thinking)
    return DeepSeekClient(settings.model_copy(update=values), target)
