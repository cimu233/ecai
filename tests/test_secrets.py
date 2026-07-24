import subprocess
import unittest

from xiaoe_core.secrets import MacKeychainSecretStore


class SecretStoreTest(unittest.TestCase):
    def test_reads_dashscope_key_without_logging_it(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))

            class Result:
                returncode = 0
                stdout = "sk-test-secret\n"

            return Result()

        value = MacKeychainSecretStore(runner=runner).get_dashscope_api_key()
        self.assertEqual("sk-test-secret", value)
        self.assertEqual("-w", calls[0][0][-1])
        self.assertNotIn("sk-test-secret", " ".join(calls[0][0]))

    def test_missing_key_returns_none(self):
        def runner(command, **kwargs):
            class Result:
                returncode = 44
                stdout = ""

            return Result()

        self.assertIsNone(MacKeychainSecretStore(runner=runner).get_dashscope_api_key())

    def test_saves_named_secret_without_printing_it(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))

            class Result:
                returncode = 0
                stdout = ""

            return Result()

        MacKeychainSecretStore(runner=runner).set("openai.api_key", "sk-private", "OpenAI")
        command, options = calls[0]
        self.assertEqual("openai.api_key", command[command.index("-a") + 1])
        self.assertEqual(subprocess.DEVNULL, options["stdout"])


if __name__ == "__main__":
    unittest.main()
