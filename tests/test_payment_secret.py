import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.errors import StopOrder
from core.payment_secret import PaymentSecret, read_payment_secret
from core.worker import launch_worker


class PaymentSecretTests(unittest.TestCase):
    def test_validation_redacted_repr_and_clear(self):
        secret = PaymentSecret("654321")
        self.assertNotIn("654321", repr(secret))
        self.assertTrue(secret)
        secret.clear()
        self.assertFalse(secret)
        for value in ("123", "abcdef", "１２３４５６", 123456, None):
            with self.subTest(value=value), self.assertRaises(StopOrder):
                PaymentSecret(value)

    def test_stdin_payload_is_bounded_and_errors_do_not_echo(self):
        original = PaymentSecret("654321")
        received = read_payment_secret(io.StringIO(original.pipe_payload().decode()))
        self.assertEqual(received.value(), original.value())
        for text in ('{"payment_password":"654321invalid"}', '654321', '{"payment_password":"654321","extra":true}'):
            with self.subTest(), self.assertRaises(StopOrder) as caught:
                read_payment_secret(io.StringIO(text))
            self.assertNotIn("654321", str(caught.exception))

    def test_child_receives_secret_via_pipe_without_argv_or_environment(self):
        secret = PaymentSecret("654321")
        code = "from core.payment_secret import read_payment_secret;import sys;secret=read_payment_secret(sys.stdin);print(len(secret.value()));secret.clear()"
        command = [sys.executable, "-c", code]
        env = dict(os.environ)
        with tempfile.TemporaryFile() as output:
            worker = launch_worker(command, Path(__file__).resolve().parents[1], env, output, secret)
            self.assertEqual(worker.wait(timeout=10), 0)
            output.seek(0)
            self.assertEqual(output.read().strip(), b"6")
        self.assertNotIn(secret.value(), " ".join(worker.args))
        self.assertNotIn(secret.value(), str(env))
        self.assertNotIn("--payment-secret-stdin", command)

    def test_no_secret_uses_closed_input_and_no_secret_flag(self):
        with patch("core.worker.subprocess.Popen") as popen:
            launch_worker(["python", "main.py", "run"], Path.cwd(), {}, None, PaymentSecret())
        args, kwargs = popen.call_args
        self.assertNotIn("--payment-secret-stdin", args[0])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
