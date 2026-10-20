import asyncio
import logging
import unittest

from moderation_delete import delete_message


class Forbidden(Exception):
    status = 403


class NotFound(Exception):
    status = 404


class HTTPException(Exception):
    status = 429


class _Message:
    id = 123
    content = "secret banned content must not be logged"

    class _Object:
        id = 456

    guild = _Object()
    channel = _Object()
    author = _Object()

    def __init__(self, error=None):
        self.error = error

    async def delete(self):
        if self.error is not None:
            raise self.error


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class ModerationDeleteTests(unittest.TestCase):
    def _run(self, message, reason="profanity"):
        logger = logging.getLogger(f"moderation-delete-test-{id(message)}")
        logger.setLevel(logging.INFO)
        handler = _ListHandler()
        logger.addHandler(handler)
        try:
            result = asyncio.run(delete_message(message, reason=reason, logger=logger))
        finally:
            logger.removeHandler(handler)
        return result, handler.records

    def test_success_is_logged_without_message_content(self):
        result, records = self._run(_Message())
        self.assertTrue(result)
        self.assertEqual(len(records), 1)
        rendered = records[0].getMessage()
        self.assertIn("status=deleted", rendered)
        self.assertIn("reason=profanity", rendered)
        self.assertIn("message_id=123", rendered)
        self.assertNotIn("secret banned content", rendered)

    def test_permission_failure_is_logged_with_status_and_metadata(self):
        result, records = self._run(_Message(Forbidden()))
        self.assertFalse(result)
        self.assertEqual(len(records), 1)
        rendered = records[0].getMessage()
        self.assertIn("MODERATION_DELETE_FAILED", rendered)
        self.assertIn("error=Forbidden", rendered)
        self.assertIn("status=403", rendered)
        self.assertIn("channel_id=456", rendered)
        self.assertNotIn("secret banned content", rendered)

    def test_not_found_is_visible_as_already_gone(self):
        result, records = self._run(_Message(NotFound()))
        self.assertFalse(result)
        self.assertIn("status=already_gone", records[0].getMessage())

    def test_http_failure_is_logged(self):
        result, records = self._run(_Message(HTTPException()))
        self.assertFalse(result)
        rendered = records[0].getMessage()
        self.assertIn("error=HTTPException", rendered)
        self.assertIn("status=429", rendered)


if __name__ == "__main__":
    unittest.main()
