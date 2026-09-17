import io
import json
import os
import signal
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from agent_box import notify


class NotifyLogicTests(unittest.TestCase):
    def _codex_requests(
        self, detail: str, *, responses: list[object] | None = None,
        project: str = "infrado",
    ) -> list[tuple[str, dict]]:
        """Run the real Codex notifier and capture serialized Telegram requests."""
        sent: list[tuple[str, dict]] = []
        outcomes = iter(responses or [b'{"ok": true}'])

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        def send_response(request, **_kwargs):
            sent.append((request.full_url.rsplit("/", 1)[-1], json.loads(request.data.decode())))
            outcome = next(outcomes, b'{"ok": true}')
            if isinstance(outcome, BaseException):
                raise outcome
            return Response(outcome)

        with (
            mock.patch.object(notify, "load_credentials", return_value=("secret-token", "chat")),
            mock.patch.object(notify.urllib.request, "urlopen", side_effect=send_response),
        ):
            notify.notify_codex(json.dumps({
                "type": "agent-turn-complete",
                "cwd": f"/work/{project}",
                "last-assistant-message": detail,
            }))
        return sent

    def _codex_oversized_notice(self, project: str = "infrado") -> str:
        return (
            f"✅ <b>Codex · {project}</b>\nTurn finished\n"
            "Full report was too large for bounded Telegram delivery."
        )

    def _claude_notices(
        self, payload: dict, *, official_channel: bool = False, state_root: Path | None = None,
    ) -> list[str]:
        """The plain-message text bodies the Claude hook sent."""
        return [body["text"] for _method, body in self._claude_requests(
            payload, official_channel=official_channel, state_root=state_root)]

    def _claude_requests(
        self, payload: dict, *, official_channel: bool = False, state_root: Path | None = None,
    ) -> list[tuple[str, dict]]:
        """Run the real Claude hook sender and capture its Telegram requests."""
        if state_root is None:
            with tempfile.TemporaryDirectory() as tmp:
                return self._claude_requests(
                    payload, official_channel=official_channel, state_root=Path(tmp))

        sent: list[tuple[str, dict]] = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        def send_response(request, **_kwargs):
            sent.append((
                request.full_url.rsplit("/", 1)[-1],
                json.loads(request.data.decode()),
            ))
            return Response(b'{"ok": true}')

        update_state = notify.update_claude_background_state
        background_active = notify.claude_background_active
        env = {notify.SUPPRESS_NOTIFY_ENV: "1"} if official_channel else {}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(notify, "load_credentials", return_value=("token", "chat")),
            mock.patch.object(notify.urllib.request, "urlopen", side_effect=send_response),
            mock.patch.object(
                notify, "update_claude_background_state",
                side_effect=lambda session_id, tasks: update_state(session_id, tasks, state_root),
            ),
            mock.patch.object(
                notify, "claude_background_active",
                side_effect=lambda session_id: background_active(session_id, state_root),
            ),
            mock.patch.object(notify.sys, "stdin", io.StringIO(json.dumps(payload))),
        ):
            notify.notify_claude()
        return sent

    def test_html_escaping(self):
        text = notify.telegram_notice("💬", "Claude", "a<b>&", "Waiting & ready")
        self.assertEqual(text, "💬 <b>Claude · a&lt;b&gt;&amp;</b>\nWaiting &amp; ready")

    def test_project_from_cwd(self):
        self.assertEqual(notify.notify_project("/tmp/my-project"), "my-project")
        self.assertEqual(notify.notify_project("/"), "unknown")
        self.assertEqual(notify.notify_project(""), "unknown")

    def test_parse_payload_bad_json(self):
        self.assertEqual(notify.parse_payload("not json"), {})
        self.assertEqual(notify.parse_payload("[]"), {})
        self.assertEqual(notify.parse_payload('{"a": 1}'), {"a": 1})

    def test_background_tasks_extraction(self):
        self.assertIsNone(notify.claude_background_tasks({}))
        self.assertEqual(notify.claude_background_tasks({"background_tasks": []}), [])
        self.assertEqual(notify.claude_background_tasks({"background_tasks": [1, 2]}), [1, 2])
        self.assertIsNone(notify.claude_background_tasks({"background_tasks": "nope"}))

    def test_background_task_suppression_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(notify.claude_background_active("s1", root))
            notify.update_claude_background_state("s1", [{"id": "x"}], root)
            self.assertTrue(notify.claude_background_active("s1", root))
            notify.update_claude_background_state("s1", [], root)
            self.assertFalse(notify.claude_background_active("s1", root))

    def test_background_state_ignores_none_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notify.update_claude_background_state("s1", None, root)
            self.assertFalse(notify.claude_background_active("s1", root))

    def test_hook_install_and_remove_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            notify.install_claude_notify_hooks(path)
            settings = __import__("json").loads(path.read_text())
            self.assertIn("Stop", settings["hooks"])
            notify.remove_claude_notify_hooks(path)
            settings = __import__("json").loads(path.read_text())
            self.assertNotIn("hooks", settings)

    def test_hook_install_preserves_unrelated_settings(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}))
            notify.install_claude_notify_hooks(path)
            settings = json.loads(path.read_text())
            self.assertEqual(settings["model"], "opus")
            self.assertIn("PreToolUse", settings["hooks"])

    def test_official_channel_still_sends_stop_completion(self):
        notices = self._claude_notices({
            "hook_event_name": "Stop", "cwd": "/work/project", "background_tasks": [],
        }, official_channel=True)
        self.assertEqual(notices, ["✅ <b>Claude · project</b>\nTurn finished"])

    def test_official_channel_still_sends_stop_failure(self):
        notices = self._claude_notices({
            "hook_event_name": "StopFailure", "cwd": "/work/project", "error": "API unavailable",
        }, official_channel=True)
        self.assertEqual(notices, [
            "❌ <b>Claude · project</b>\nStopped with an error\nAPI unavailable",
        ])

    def test_official_channel_owns_interaction_notifications(self):
        for notification_type in ("permission_prompt", "idle_prompt"):
            with self.subTest(notification_type=notification_type):
                notices = self._claude_notices({
                    "hook_event_name": "Notification",
                    "notification_type": notification_type,
                    "cwd": "/work/project",
                }, official_channel=True)
                self.assertEqual(notices, [])

    def test_official_channel_does_not_claim_unknown_notification_types(self):
        with mock.patch.dict(os.environ, {notify.SUPPRESS_NOTIFY_ENV: "1"}, clear=True):
            self.assertFalse(notify.claude_interaction_suppressed("Notification"))
            self.assertFalse(notify.claude_interaction_suppressed("Notification", "future_prompt"))

    def test_normal_claude_session_keeps_all_notification_behavior(self):
        cases = [
            (
                {"hook_event_name": "Notification", "notification_type": "permission_prompt",
                 "cwd": "/work/project", "message": "Approve this command"},
                "🔐 <b>Claude · project</b>\nPermission required\nApprove this command",
            ),
            (
                {"hook_event_name": "Notification", "notification_type": "idle_prompt",
                 "cwd": "/work/project", "message": "What next?"},
                "💬 <b>Claude · project</b>\nWaiting for input\nWhat next?",
            ),
            (
                {"hook_event_name": "Stop", "cwd": "/work/project", "background_tasks": []},
                "✅ <b>Claude · project</b>\nTurn finished",
            ),
            (
                {"hook_event_name": "StopFailure", "cwd": "/work/project", "error": "API unavailable"},
                "❌ <b>Claude · project</b>\nStopped with an error\nAPI unavailable",
            ),
        ]
        for payload, expected in cases:
            with self.subTest(event=payload["hook_event_name"]):
                self.assertEqual(self._claude_notices(payload), [expected])

    def test_claude_stop_sends_the_assistant_message_as_rich_markdown(self):
        sent = self._claude_requests({
            "hook_event_name": "Stop", "cwd": "/work/project", "background_tasks": [],
            "last_assistant_message": "## Done\n\n- Fixed `notify.py`\n- Ran the tests\n",
        })
        self.assertEqual(sent, [("sendRichMessage", {
            "chat_id": "chat",
            "rich_message": {"markdown": (
                "✅ **Claude · project**\nTurn finished\n\n"
                "## Done\n\n- Fixed `notify.py`\n- Ran the tests"
            )},
        })])

    def test_claude_stop_without_a_message_still_reports_completion(self):
        for message in ("", "   \n  ", None):
            with self.subTest(message=message):
                self.assertEqual(self._claude_notices({
                    "hook_event_name": "Stop", "cwd": "/work/project",
                    "background_tasks": [], "last_assistant_message": message,
                }), ["✅ <b>Claude · project</b>\nTurn finished"])

    def test_claude_stop_report_survives_the_official_channel(self):
        sent = self._claude_requests({
            "hook_event_name": "Stop", "cwd": "/work/project", "background_tasks": [],
            "last_assistant_message": "All green.",
        }, official_channel=True)
        self.assertEqual([method for method, _body in sent], ["sendRichMessage"])

    def test_claude_stop_message_is_sanitized_like_any_model_output(self):
        sent = self._claude_requests({
            "hook_event_name": "Stop", "cwd": "/work/project", "background_tasks": [],
            "last_assistant_message": "<b>bold</b> and tg://resolve",
        })
        markdown = sent[0][1]["rich_message"]["markdown"]
        self.assertNotIn("<b>", markdown)
        self.assertNotIn("tg://", markdown)

    def test_background_stop_is_silent(self):
        notices = self._claude_notices({
            "hook_event_name": "Stop",
            "cwd": "/work/project",
            "session_id": "session-1",
            "background_tasks": [{"task_id": "background-1"}],
        }, official_channel=True)
        self.assertEqual(notices, [])

    def test_codex_notification_is_not_suppressed_by_claude_channel_mode(self):
        with (
            mock.patch.dict(os.environ, {notify.SUPPRESS_NOTIFY_ENV: "1"}, clear=True),
        ):
            sent = self._codex_requests("Completed the task.", project="project")
        self.assertEqual(sent, [("sendRichMessage", {
            "chat_id": "chat",
            "rich_message": {
                "markdown": "✅ **Codex · project**\nTurn finished\n\nCompleted the task.",
            },
        })])

    def test_a_reviewer_markdown_report_is_delivered_whole_and_sanitized(self):
        """The reviewer already answers in Markdown: its verdict line and every finding arrive as written."""
        report = ("Verdict: NEEDS_WORK\n\n- [HIGH] agent_box/notify.py:1 — <b>Issue</b> tg://resolve\n"
                  "- [LOW] tests/test_notify.py:12 — Second issue.")
        sent = self._codex_requests(report, responses=[b'{"ok": false}'])
        rich = sent[0][1]["rich_message"]["markdown"]
        self.assertEqual(rich, "✅ **Codex · infrado**\nTurn finished\n\n" + notify.sanitize_rich_markdown(report))
        self.assertIn("Verdict: NEEDS_WORK", rich)
        self.assertNotIn("<b>Issue</b>", rich)
        self.assertNotIn("tg://", rich)
        plain = notify.html.unescape(sent[1][1]["text"])
        self.assertIn("[LOW] tests/test_notify.py:12", plain)

    def test_long_review_report_preserves_all_findings_within_delivery_limits(self):
        """A complete report that needs chunks keeps its tail."""
        evidence = "x" * notify.TELEGRAM_RICH_TEXT_LIMIT
        report = f"Verdict: NEEDS_WORK\n\n- [LOW] a.py:1 — First. {evidence}\n\n- [LOW] a.py:2 — Final proof.\n"
        sent = self._codex_requests(report)
        self.assertEqual([method for method, _body in sent], ["sendRichMessage"] * 2)
        markdown = "".join(body["rich_message"]["markdown"] for _method, body in sent)
        self.assertIn(evidence, markdown)
        self.assertIn("Final proof.", markdown)
        oversized = "Verdict: NEEDS_WORK\n\n" + evidence * notify.MAX_TELEGRAM_REPORT_MESSAGES
        self.assertEqual(self._codex_requests(oversized)[0][1]["text"], self._codex_oversized_notice())

    def test_codex_sends_github_style_markdown_as_a_rich_message(self):
        detail = (
            "## Quality Audit — Standards axis\n\n"
            "### Summary\n\n"
            "- **Ship:** ✅ Ship\n"
            "- `tests/unit/test_images.py` uses a < b & c > d\n\n"
            "1. Keep _underscores_ and *emphasis*.\n"
            "2. Visit [docs](https://example.test/a_(b)?x=1&y=2).\n\n"
            "> quoted [text]\n\n"
            "| File | Result |\n| --- | --- |\n| `a.py` | pass |\n\n"
            "```python\nprint(\"<safe> & intact\")\n```"
        )

        sent = self._codex_requests(detail)

        self.assertEqual(sent, [("sendRichMessage", {
            "chat_id": "chat",
            "rich_message": {
                "markdown": (
                    "✅ **Codex · infrado**\nTurn finished\n\n"
                    "## Quality Audit — Standards axis\n\n"
                    "### Summary\n\n"
                    "- **Ship:** ✅ Ship\n"
                    "- `tests/unit/test_images.py` uses a &lt; b &amp; c > d\n\n"
                    "1. Keep _underscores_ and *emphasis*.\n"
                    "2. Visit [docs](https://example.test/a_(b)?x=1&amp;y=2).\n\n"
                    "> quoted [text]\n\n"
                    "| File | Result |\n| --- | --- |\n| `a.py` | pass |\n\n"
                    "```python\nprint(\"<safe> & intact\")\n```"
                ),
            },
        })])

    def test_codex_neutralizes_model_html_and_telegram_specific_content(self):
        detail = (
            '<tg-button type="callback_data" data="do-it">Run</tg-button> '
            '<b>model bold</b> ![media](https://example.test/image.png) '
            '[mention](tg://user?id=123) '
            '`<b>code stays exact</b>`\n'
            '```html\n<tg-map lat="1" long="2"/>\n```'
        )

        sent = self._codex_requests(detail)
        markdown = sent[0][1]["rich_message"]["markdown"]

        self.assertIn(
            '&lt;tg-button type="callback_data" data="do-it">Run&lt;/tg-button>', markdown,
        )
        self.assertIn('&lt;b>model bold&lt;/b>', markdown)
        self.assertIn('&#33;[media](https://example.test/image.png)', markdown)
        self.assertNotIn("tg://", markdown)
        self.assertIn("t\u200bg://user?id=123", markdown)
        self.assertIn('`<b>code stays exact</b>`', markdown)
        self.assertIn('```html\n<tg-map lat="1" long="2"/>\n```', markdown)

    def test_codex_does_not_trust_mismatched_backtick_runs_as_code(self):
        sent = self._codex_requests('` <tg-button type="callback_data">Run</tg-button> ``')

        markdown = sent[0][1]["rich_message"]["markdown"]
        self.assertIn(
            '` &lt;tg-button type="callback_data">Run&lt;/tg-button> ``', markdown,
        )

    def test_codex_encodes_escaped_image_and_html_introducers_as_text(self):
        detail = (
            "\\![image](https://example.test/image.png)\n"
            "\\\\![even](https://example.test/even.png)\n"
            '\\<tg-button type="copy_text" text="secret">Copy\\</tg-button>\n'
            '\\\\<tg-button type="copy_text" text="even">Copy\\\\</tg-button>'
        )

        sent = self._codex_requests(detail)
        markdown = sent[0][1]["rich_message"]["markdown"]

        self.assertIn(r"\&#33;[image](https://example.test/image.png)", markdown)
        self.assertIn(r"\\&#33;[even](https://example.test/even.png)", markdown)
        self.assertIn(
            r'\&lt;tg-button type="copy_text" text="secret">Copy\&lt;/tg-button>',
            markdown,
        )
        self.assertIn(
            r'\\&lt;tg-button type="copy_text" text="even">Copy\\&lt;/tg-button>',
            markdown,
        )
        self.assertNotIn("![", markdown)
        self.assertNotIn("<tg-button", markdown)

    def test_codex_distinguishes_escaped_and_valid_inline_code_spans(self):
        detail = (
            '\\`<tg-button type="copy_text" text="x">Copy</tg-button>`\n'
            '`<tg-button type="copy_text" text="valid">Copy</tg-button>`\n'
            '``<tg-button type="copy_text" text="multi">Copy</tg-button>``\n'
            '``<tg-button type="copy_text" text="unmatched">Copy</tg-button>`\n'
            '\\\\`<tg-button type="copy_text" text="even">Copy</tg-button>`'
        )

        sent = self._codex_requests(detail)
        markdown = sent[0][1]["rich_message"]["markdown"]

        self.assertIn(
            r'\`&lt;tg-button type="copy_text" text="x">Copy&lt;/tg-button>`',
            markdown,
        )
        self.assertIn(
            '`<tg-button type="copy_text" text="valid">Copy</tg-button>`', markdown,
        )
        self.assertIn(
            '``<tg-button type="copy_text" text="multi">Copy</tg-button>``', markdown,
        )
        self.assertIn(
            '``&lt;tg-button type="copy_text" text="unmatched">Copy&lt;/tg-button>`',
            markdown,
        )
        self.assertIn(
            '\\\\`<tg-button type="copy_text" text="even">Copy</tg-button>`', markdown,
        )

    def test_codex_preserves_backslashes_inside_an_open_code_span(self):
        detail = '`<tg-button type="copy_text">Copy</tg-button>\\` tail'

        sent = self._codex_requests(detail)

        self.assertIn(detail, sent[0][1]["rich_message"]["markdown"])

    def test_codex_neutralizes_escaped_and_entity_encoded_telegram_targets(self):
        detail = (
            "[raw](tg://user?id=1)\n"
            "[escaped](tg\\://user?id=2)\n"
            "[entity](tg&#58;//user?id=3)"
        )

        sent = self._codex_requests(detail)
        markdown = sent[0][1]["rich_message"]["markdown"]

        self.assertIn("[raw](t\u200bg://user?id=1)", markdown)
        self.assertIn("[escaped](t\u200bg\\://user?id=2)", markdown)
        self.assertIn("[entity](tg&amp;#58;//user?id=3)", markdown)
        self.assertNotIn("tg://", markdown.lower())

    def test_codex_falls_back_without_hanging_on_oversized_fence_opener(self):
        def timeout_handler(_signum, _frame):
            raise TimeoutError("splitter made no progress")

        previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
        try:
            for info_length in (
                notify.TELEGRAM_RICH_TEXT_LIMIT - 7,
                notify.TELEGRAM_RICH_TEXT_LIMIT,
            ):
                with self.subTest(info_length=info_length):
                    detail = "intro\n```" + "x" * info_length + "\nbody\n```"
                    signal.setitimer(signal.ITIMER_REAL, 1)
                    try:
                        sent = self._codex_requests(detail)
                    except TimeoutError:
                        self.fail("split_rich_markdown did not terminate")
                    finally:
                        signal.setitimer(signal.ITIMER_REAL, 0)

                    self.assertTrue(sent)
                    self.assertEqual(len(sent), 1)
                    self.assertTrue(all(method == "sendMessage" for method, _body in sent))
                    self.assertEqual(sent[0][1]["text"], self._codex_oversized_notice())
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)

    def test_codex_first_line_near_limit_fence_falls_back_without_emitting_it_partially(self):
        prefix = "✅ **Codex · infrado**\nTurn finished\n\n"
        marker = "```"
        opener = marker + "x" * (notify.TELEGRAM_RICH_TEXT_LIMIT - len(marker) - 5)
        trusted_literal = '<tg-button type="copy_text" text="x">Copy</tg-button>'
        detail = f"{opener}\n{trusted_literal}\n{marker}"

        self.assertEqual(
            len(opener) + 1 + len(marker), notify.TELEGRAM_RICH_TEXT_LIMIT - 1,
        )
        self.assertGreater(
            len(prefix) + len(opener) + 1 + len(marker),
            notify.TELEGRAM_RICH_TEXT_LIMIT,
        )

        sent = self._codex_requests(detail)

        self.assertEqual(len(sent), 1)
        self.assertTrue(all(method == "sendMessage" for method, _body in sent))
        self.assertEqual(sent[0][1]["text"], self._codex_oversized_notice())
        self.assertNotIn(trusted_literal, sent[0][1]["text"])

    def test_codex_first_line_pathological_fence_falls_back_before_splitting(self):
        marker = "```"
        available_fenced_payload = 64
        opener = marker + "x" * (
            notify.TELEGRAM_RICH_TEXT_LIMIT
            - 2 * len(marker)
            - 2
            - available_fenced_payload
        )
        trusted_literal = '<tg-button type="copy_text" text="x">Copy</tg-button>'
        detail = f"{opener}\n{trusted_literal}\n{'x' * notify.TELEGRAM_RICH_TEXT_LIMIT}\n{marker}"

        self.assertEqual(
            notify.TELEGRAM_RICH_TEXT_LIMIT - len(opener) - len(marker) - 2,
            available_fenced_payload,
        )

        sent = self._codex_requests(detail)

        self.assertTrue(sent)
        self.assertEqual(len(sent), 1)
        self.assertTrue(all(method == "sendMessage" for method, _body in sent))
        self.assertEqual(sent[0][1]["text"], self._codex_oversized_notice())
        self.assertNotIn(trusted_literal, sent[0][1]["text"])

    def test_codex_fence_plan_with_256_payload_chunks_falls_back_before_sending(self):
        marker = "```"
        available_fenced_payload = 256
        opener = marker + "x" * (
            notify.TELEGRAM_RICH_TEXT_LIMIT
            - 2 * len(marker)
            - 2
            - available_fenced_payload
        )
        trusted_literal = '<tg-button type="copy_text" text="x">Copy</tg-button>'
        detail = f"{opener}\n{trusted_literal}\n{'x' * notify.TELEGRAM_RICH_TEXT_LIMIT}\n{marker}"

        self.assertEqual(
            notify.TELEGRAM_RICH_TEXT_LIMIT - len(opener) - len(marker) - 2,
            available_fenced_payload,
        )

        sent = self._codex_requests(detail)

        self.assertTrue(sent)
        self.assertEqual(len(sent), 1)
        self.assertTrue(all(method == "sendMessage" for method, _body in sent))
        self.assertEqual(sent[0][1]["text"], self._codex_oversized_notice())
        self.assertNotIn(trusted_literal, sent[0][1]["text"])

    def test_codex_allows_exactly_eight_planned_rich_chunks(self):
        rich_chunk_limit = 8
        prefix = "✅ **Codex · infrado**\nTurn finished\n\n"
        detail = "x" * (
            notify.TELEGRAM_RICH_TEXT_LIMIT - len(prefix)
            + (rich_chunk_limit - 1) * notify.TELEGRAM_RICH_TEXT_LIMIT
        )

        sent = self._codex_requests(detail)

        self.assertEqual([method for method, _body in sent], ["sendRichMessage"] * rich_chunk_limit)
        chunks = [body["rich_message"]["markdown"] for _method, body in sent]
        self.assertEqual("".join(chunks), prefix + detail)
        self.assertTrue(all(
            len(chunk) <= notify.TELEGRAM_RICH_TEXT_LIMIT for chunk in chunks
        ))

    def test_codex_ninth_planned_rich_chunk_falls_back_before_any_rich_send(self):
        rich_chunk_limit = 8
        prefix = "✅ **Codex · infrado**\nTurn finished\n\n"
        detail = "x" * (
            notify.TELEGRAM_RICH_TEXT_LIMIT - len(prefix)
            + rich_chunk_limit * notify.TELEGRAM_RICH_TEXT_LIMIT
        )

        sent = self._codex_requests(detail)

        self.assertTrue(sent)
        self.assertTrue(all(method == "sendMessage" for method, _body in sent))
        self.assertEqual(sent, [("sendMessage", {
            "chat_id": "chat",
            "text": self._codex_oversized_notice(),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })])

    def test_codex_allows_exactly_eight_planned_plain_messages(self):
        plain_message_limit = 8
        visible_header = "✅ Codex · infrado\nTurn finished\n"
        inline_code = "`x`"
        detail = "x" * (
            notify.TELEGRAM_TEXT_LIMIT - len(visible_header)
            + (plain_message_limit - 1) * notify.TELEGRAM_TEXT_LIMIT
            - len(inline_code)
        ) + inline_code

        sent = self._codex_requests(detail)

        self.assertEqual([method for method, _body in sent], ["sendMessage"] * plain_message_limit)
        self.assertEqual(
            "".join(body["text"] for _method, body in sent),
            "✅ <b>Codex · infrado</b>\nTurn finished\n" + notify.html.escape(detail),
        )

    def test_codex_ninth_planned_plain_message_uses_compact_notice(self):
        plain_message_limit = 8
        visible_header = "✅ Codex · infrado\nTurn finished\n"
        inline_code = "`x`"
        detail = "x" * (
            notify.TELEGRAM_TEXT_LIMIT - len(visible_header)
            + plain_message_limit * notify.TELEGRAM_TEXT_LIMIT
            - len(inline_code)
        ) + inline_code

        sent = self._codex_requests(detail)

        self.assertEqual(sent, [("sendMessage", {
            "chat_id": "chat",
            "text": self._codex_oversized_notice(),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })])

    def test_codex_plain_fallback_stops_after_first_transport_failure(self):
        detail = "x" * (2 * notify.TELEGRAM_TEXT_LIMIT) + "`x`"

        sent = self._codex_requests(detail, responses=[
            b'{"ok": false}', b'{"ok": false}', b'{"ok": true}',
        ])

        self.assertEqual([method for method, _body in sent], ["sendRichMessage", "sendMessage"])

    def test_codex_plain_fallback_stops_after_later_transport_failure(self):
        detail = "x" * (2 * notify.TELEGRAM_TEXT_LIMIT) + "`x`"

        sent = self._codex_requests(detail, responses=[
            b'{"ok": false}', b'{"ok": true}', b'{"ok": false}', b'{"ok": true}',
        ])

        self.assertEqual(
            [method for method, _body in sent],
            ["sendRichMessage", "sendMessage", "sendMessage"],
        )

    def test_codex_partial_rich_failure_sends_one_compact_notice_without_resend(self):
        prefix = "✅ **Codex · infrado**\nTurn finished\n\n"
        detail = "x" * (
            notify.TELEGRAM_RICH_TEXT_LIMIT - len(prefix)
            + 2 * notify.TELEGRAM_RICH_TEXT_LIMIT
        )

        sent = self._codex_requests(detail, responses=[
            b'{"ok": true}', b'{"ok": true}', b'{"ok": false}', b'{"ok": true}',
        ])

        self.assertEqual(
            [method for method, _body in sent],
            ["sendRichMessage", "sendRichMessage", "sendRichMessage", "sendMessage"],
        )
        self.assertEqual(sent[-1][1]["text"], (
            "✅ <b>Codex · infrado</b>\nTurn finished\n"
            "Some of the rich report was delivered; the remainder could not be sent."
        ))

    def test_codex_oversized_inline_code_line_falls_back_without_exposing_literal_html(self):
        prefix = "✅ **Codex · infrado**\nTurn finished\n\n"
        trusted_literal = '<tg-button type="copy_text" text="x">Copy</tg-button>'
        detail = "x" * (notify.TELEGRAM_RICH_TEXT_LIMIT - len(prefix) - 1)
        detail += f"`{trusted_literal}`"

        sent = self._codex_requests(detail)

        self.assertEqual(len(sent), 1)
        self.assertTrue(all(method == "sendMessage" for method, _body in sent))
        self.assertEqual(sent[0][1]["text"], self._codex_oversized_notice())
        self.assertNotIn(trusted_literal, sent[0][1]["text"])

    def test_codex_rich_failure_retries_complete_report_as_safe_html(self):
        detail = "## Report\n\n- **unsafe <b>tag</b> & useful**"

        sent = self._codex_requests(detail, responses=[
            b'{"ok": false, "error_code": 400, "description": "unsupported markup"}',
            b'{"ok": true}',
        ])

        self.assertEqual([method for method, _body in sent], ["sendRichMessage", "sendMessage"])
        self.assertEqual(sent[1][1], {
            "chat_id": "chat",
            "text": (
                "✅ <b>Codex · infrado</b>\nTurn finished\n"
                "## Report\n\n- **unsafe &lt;b&gt;tag&lt;/b&gt; &amp; useful**"
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    def test_codex_rich_messages_split_at_documented_limit(self):
        prefix = "✅ **Codex · infrado**\nTurn finished\n\n"
        detail = "a" * (notify.TELEGRAM_RICH_TEXT_LIMIT - len(prefix) + 1)

        sent = self._codex_requests(detail)

        self.assertEqual([method for method, _body in sent], ["sendRichMessage", "sendRichMessage"])
        chunks = [body["rich_message"]["markdown"] for _method, body in sent]
        self.assertEqual(len(chunks[0]), notify.TELEGRAM_RICH_TEXT_LIMIT)
        self.assertEqual(chunks[0], prefix + detail[:-1])
        self.assertEqual(chunks[1], "a")

    def test_codex_exact_rich_limit_stays_in_one_unicode_safe_request(self):
        prefix = "✅ **Codex · infrado**\nTurn finished\n\n"
        detail = "🙂" * (notify.TELEGRAM_RICH_TEXT_LIMIT - len(prefix))

        sent = self._codex_requests(detail)

        self.assertEqual(len(sent), 1)
        markdown = sent[0][1]["rich_message"]["markdown"]
        self.assertEqual(len(markdown), notify.TELEGRAM_RICH_TEXT_LIMIT)
        self.assertEqual(markdown, prefix + detail)

    def test_codex_split_closes_and_reopens_fenced_code(self):
        prefix = "✅ **Codex · infrado**\nTurn finished\n\n"
        detail = "```text\n" + "x" * notify.TELEGRAM_RICH_TEXT_LIMIT + "\n```"

        sent = self._codex_requests(detail)
        chunks = [body["rich_message"]["markdown"] for _method, body in sent]

        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[0].startswith(prefix + "```text\n"))
        self.assertTrue(chunks[0].endswith("\n```"))
        self.assertTrue(chunks[1].startswith("```text\n"))
        self.assertTrue(all(len(chunk) <= notify.TELEGRAM_RICH_TEXT_LIMIT for chunk in chunks))
        self.assertTrue(chunks[-1].endswith("\n```"))

    def test_codex_plain_fallback_splits_at_regular_message_limit(self):
        visible_header = "✅ Codex · infrado\nTurn finished\n"
        detail = "🙂" * (notify.TELEGRAM_TEXT_LIMIT - len(visible_header) + 1)

        sent = self._codex_requests(detail, responses=[b'{"ok": false}'])

        fallback = [body for method, body in sent if method == "sendMessage"]
        self.assertEqual(len(fallback), 2)
        self.assertEqual(
            len(fallback[0]["text"].replace("<b>", "").replace("</b>", "")),
            notify.TELEGRAM_TEXT_LIMIT,
        )
        self.assertEqual(fallback[1]["text"], "🙂")

    def test_telegram_errors_do_not_leak_token(self):
        output = io.StringIO()
        error = urllib.error.URLError("request failed for secret-token")

        with mock.patch("sys.stdout", output), mock.patch("sys.stderr", output):
            sent = self._codex_requests("report", responses=[error, error])

        self.assertEqual([method for method, _body in sent], ["sendRichMessage", "sendMessage"])
        self.assertNotIn("secret-token", output.getvalue())

    def test_background_work_stays_quiet_until_final_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notices = self._claude_notices({
                "hook_event_name": "Stop", "cwd": "/work/project", "session_id": "session-1",
                "background_tasks": [{"task_id": "background-1"}],
            }, state_root=root)
            notices += self._claude_notices({
                "hook_event_name": "Notification", "notification_type": "idle_prompt",
                "cwd": "/work/project", "session_id": "session-1",
            }, state_root=root)
            notices += self._claude_notices({
                "hook_event_name": "Stop", "cwd": "/work/project", "session_id": "session-1",
                "background_tasks": [],
            }, state_root=root)
        self.assertEqual(notices, [
            "✅ <b>Claude · project</b>\nTurn finished",
        ])


if __name__ == "__main__":
    unittest.main()
