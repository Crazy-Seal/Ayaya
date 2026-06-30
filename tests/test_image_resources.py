import asyncio
from types import SimpleNamespace

from app.agent.context import HookContext, PluginHook
from app.agent.models import vlm
from app.agent.plugins import image as image_plugin_module
from app.agent.plugins.image import ImagePlugin
from app.agent.state import AgentState
from app.agent.utils.domain.images import get_image_task


def test_vlm_client_is_closed(monkeypatch) -> None:
    clients = []

    class FakeCompletions:
        async def create(self, **kwargs):
            message = SimpleNamespace(content="description")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())
            self.closed = False
            clients.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(vlm, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(vlm, "save_multiple_images", lambda images: ["test.png"])
    monkeypatch.setenv("VLM_API_KEY", "test-key")

    result = asyncio.run(vlm.generate_multiple_image_descriptions(["image-data"]))

    assert result.description == "description"
    assert len(clients) == 1
    assert clients[0].closed is True


def test_image_plugin_cancels_unconsumed_task(monkeypatch) -> None:
    started = asyncio.Event()

    async def never_finishes(*args, **kwargs):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(
        image_plugin_module, "generate_multiple_image_descriptions", never_finishes
    )

    async def scenario() -> None:
        plugin = ImagePlugin()
        state = AgentState.create_new("test-session")
        state.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                ],
            }
        )
        context = HookContext.create(PluginHook.ON_INVOKE, state)

        await plugin.execute(context)
        await started.wait()
        key = state.extra["image_task_key"]
        task = get_image_task(key)
        assert task is not None

        await plugin.on_unregister()
        await asyncio.sleep(0)
        assert get_image_task(key) is None
        assert task.cancelled()

    asyncio.run(scenario())
