from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from backend import ai


class AiProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_status_uses_new_provider_keys(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "deepseek-key",
                "GLM_API_KEY": "glm-key",
                "QWEN_API_KEY": "qwen-key",
            },
            clear=True,
        ):
            self.assertEqual(
                ai.provider_status(),
                {"deepseek": True, "glm": True, "qwen": True},
            )

    async def test_qwen_uses_shared_chat_payload_and_disables_thinking_for_json(self) -> None:
        response = {
            "choices": [{"message": {"content": '{"title":"测试"}'}}],
        }
        post = AsyncMock(return_value=response)
        with (
            patch.object(ai, "_post_json", post),
            patch.object(ai, "base_url_for", return_value="https://example.test/v1"),
        ):
            result = await ai._call_compatible("qwen", "统一提示", "qwen3.8-max", "secret")

        self.assertEqual(result, {"title": "测试"})
        request = post.await_args.args[2]
        self.assertEqual(request["messages"][1]["content"], "统一提示")
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertFalse(request["enable_thinking"])


if __name__ == "__main__":
    unittest.main()
