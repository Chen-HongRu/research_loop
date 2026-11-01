import unittest

from benchmarks.cactus.bridge import parse_request


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "job_id": "tiny-001",
            "epochs": 1,
            "lr": 0.001,
            "batch_size": 128,
            "seed": 0,
        }

    def test_valid_configuration_is_copied(self):
        parsed = parse_request(self.request)
        self.assertEqual(parsed, self.request)
        self.assertIsNot(parsed, self.request)

    def test_rejects_injection_unknown_fields_and_invalid_ranges(self):
        for field, value in [
            ("job_id", "../outside"),
            ("job_id", "x;touch pwned"),
            ("job_id", "$(whoami)"),
            ("epochs", 0),
            ("epochs", 6),
            ("epochs", True),
            ("batch_size", 31),
            ("batch_size", 513),
            ("batch_size", 128.0),
            ("seed", -1),
            ("seed", True),
            ("lr", float("nan")),
            ("lr", float("inf")),
            ("lr", -0.001),
            ("lr", 0),
            ("lr", 0.11),
            ("lr", True),
            ("command", "whoami"),
        ]:
            request = {**self.request, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                parse_request(request)
        del self.request["seed"]
        with self.assertRaises(ValueError):
            parse_request(self.request)
