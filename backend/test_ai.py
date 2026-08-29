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

    def test_prompt_uses_soft_length_guidance_for_every_provider(self) -> None:
        prompt = ai._build_prompt([])

        self.assertIn("title 尽量控制在 30 字内", prompt)
        self.assertIn("不会仅因文字较长判定失败", prompt)

    def test_long_model_text_is_preserved_until_the_safety_limit(self) -> None:
        summary = "摘要" * 300
        raw = {
            "title": "标题" * 40,
            "summary": summary,
            "logic": "逻辑" * 1100,
            "picks": [
                {
                    "code": f"60000{index}",
                    "name": "股票名称" * 30,
                    "score": 90 - index,
                    "reason": "理由" * 1100,
                    "risk": "风险" * 600,
                }
                for index in range(1, 4)
            ],
        }

        result = ai.AiResult.model_validate(ai._normalize_result_text(raw))

        self.assertEqual(len(result.title), ai.TEXT_SAFETY_LIMITS["title"])
        self.assertEqual(result.summary, summary)
        self.assertEqual(len(result.logic), ai.TEXT_SAFETY_LIMITS["logic"])
        self.assertTrue(all(len(pick.name) <= ai.TEXT_SAFETY_LIMITS["name"] for pick in result.picks))
        self.assertTrue(all(len(pick.reason) <= ai.TEXT_SAFETY_LIMITS["reason"] for pick in result.picks))
        self.assertTrue(all(len(pick.risk) <= ai.TEXT_SAFETY_LIMITS["risk"] for pick in result.picks))

    async def test_failed_current_run_can_retry_without_force(self) -> None:
        candidates = [
            {"symbol": f"60000{index}", "name": f"测试股票{index}", "quote": {}}
            for index in range(1, 4)
        ]
        raw = {
            "title": "统一研究排序",
            "summary": "基于候选快照完成横向比较并给出谨慎排序。",
            "logic": "比较涨跌幅与成交额，同时明确数据边界。",
            "picks": [
                {
                    "code": f"60000{index}",
                    "name": f"测试股票{index}",
                    "score": 90 - index,
                    "reason": "成交较活跃，候选池内相对靠前。",
                    "risk": "快照信息有限，需核对最新公告。",
                }
                for index in range(1, 4)
            ],
        }
        previous = {"provider": "deepseek", "status": "failed", "error": "旧错误"}
        saved = {"provider": "deepseek", "status": "succeeded", "result": raw}
        call = AsyncMock(return_value=raw)

        with (
            patch.object(ai.database, "china_date", return_value="2026-08-29"),
            patch.object(ai.database, "read_ai_run", side_effect=[previous, saved]),
            patch.object(ai.database, "get_meta", return_value=ai.PROMPT_VERSION),
            patch.object(ai.database, "set_meta"),
            patch.object(ai.database, "start_ai_run"),
            patch.object(ai.database, "finish_ai_run") as finish,
            patch.object(ai, "provider_key", return_value="secret"),
            patch.object(ai, "model_for", return_value="deepseek-chat"),
            patch.object(ai, "_call_compatible", call),
        ):
            result = await ai._execute_provider("deepseek", candidates)

        self.assertEqual(result["status"], "succeeded")
        call.assert_awaited_once()
        finish.assert_called_once()


if __name__ == "__main__":
    unittest.main()
