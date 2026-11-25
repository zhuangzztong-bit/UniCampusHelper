"""Offline regression tests for model requests and the workbench API indicator."""

from __future__ import annotations

import io
import json
import sys
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openai import BadRequestError, RateLimitError
from openai import _base_client as sdk_base

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import UnipusAI_Helper as app
from unipus_helper.workbench_controller import WorkbenchController

httpx = getattr(sdk_base, "httpx2", None) or sdk_base.httpx


def model_config():
    return SimpleNamespace(
        base_url="https://model-api.invalid/v1",
        api_key="fake-model-api-key",
        model="Custom-Model",
        temperature=0.2,
        max_tokens=1024,
    )


def completion(text):
    choice = SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def request_error(error_type, status, code, message):
    error = {"code": code, "message": message}
    request = httpx.Request("POST", "https://model-api.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": error})
    return error_type(message, response=response, body=error)


class ModelRequestTests(unittest.TestCase):
    def setUp(self):
        self.create = Mock()
        self.client = app.OpenAICompatibleClient.__new__(app.OpenAICompatibleClient)
        self.client.config = model_config()
        self.client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        self.client.conversation_history = []
        self.client.material_messages = []
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(app.logger, "error"))
        self.enterContext(patch.object(app.time, "sleep"))

    def test_invalid_model_is_requested_once(self):
        self.create.side_effect = request_error(BadRequestError, 400, "model_not_found", "Model does not exist")

        self.assertIsNone(self.client.ask("测试问题"))

        self.assertEqual(self.create.call_count, 1)

    def test_request_honors_configured_output_budget(self):
        self.create.return_value = completion("正确正文")

        self.assertEqual(self.client.ask("测试问题"), "正确正文")

        self.assertEqual(self.create.call_args.kwargs["max_tokens"], 1024)

    def test_rate_limit_can_retry_with_the_original_budget(self):
        self.create.side_effect = [
            request_error(RateLimitError, 429, "rate_limit", "Too many requests"),
            completion("重试成功"),
        ]

        self.assertEqual(self.client.ask("测试问题"), "重试成功")

        self.assertEqual([call.kwargs["max_tokens"] for call in self.create.call_args_list], [1024, 1024])

    def test_response_arriving_after_stop_is_discarded(self):
        stopped = threading.Event()

        def stop_before_response(**kwargs):
            stopped.set()
            return completion("迟到正文")

        self.create.side_effect = stop_before_response

        self.assertIsNone(self.client.ask("测试问题", stop_requested=stopped))

        self.assertEqual(self.client.conversation_history, [])


class WorkbenchProbeTests(unittest.TestCase):
    def run_probe(self, content, content_type, config=None):
        with patch.object(threading.Thread, "start"):
            controller = WorkbenchController(bot=SimpleNamespace(config=config or model_config()))
        requests = []

        def sdk_response(request, **kwargs):
            requests.append(json.loads(request.content))
            return httpx.Response(200, request=request, content=content, headers={"Content-Type": content_type})

        def legacy_response(request, **kwargs):
            requests.append(json.loads(request.data))
            return io.BytesIO(content)

        with patch.object(httpx.Client, "send", side_effect=sdk_response), \
             patch.object(urllib.request, "urlopen", side_effect=legacy_response):
            controller._probe_api()
        return controller, requests

    def test_http_200_html_is_displayed_as_a_failed_probe(self):
        controller, requests = self.run_probe(b"<!doctype html><title>Gateway login</title>", "text/html")

        self.assertEqual(len(requests), 1)
        self.assertFalse(controller._api_ok, controller._api_status)
        self.assertTrue(controller._api_fail, controller._api_status)

    def test_http_200_without_answer_text_is_displayed_as_a_failed_probe(self):
        payload = {"choices": [{"message": {"content": "  "}, "finish_reason": "stop"}]}
        controller, requests = self.run_probe(json.dumps(payload).encode(), "application/json")

        self.assertEqual(len(requests), 1)
        self.assertFalse(controller._api_ok, controller._api_status)
        self.assertTrue(controller._api_fail, controller._api_status)

    def test_valid_answer_preserves_custom_model_name(self):
        payload = {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
        controller, requests = self.run_probe(json.dumps(payload).encode(), "application/json")

        self.assertTrue(controller._api_ok, controller._api_status)
        self.assertEqual([request["model"] for request in requests], ["Custom-Model"])

    def test_official_deepseek_model_case_error_shows_the_exact_model_name(self):
        config = model_config()
        config.base_url = "https://api.deepseek.com"
        config.model = "deepseek-Flash"
        payload = {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}

        controller, requests = self.run_probe(json.dumps(payload).encode(), "application/json", config=config)

        self.assertFalse(controller._api_ok, controller._api_status)
        self.assertIn("deepseek-flash", controller._api_status)
        self.assertEqual(requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
