"""Release verification must distinguish enforced denial from ordinary outages."""

import errno
import os
import sys
import unittest
from unittest.mock import patch

from scripts import check_offline_network as network


class OfflineNetworkTests(unittest.TestCase):
    def test_macos_runs_verifier_only_after_permission_denial(self) -> None:
        command = [sys.executable, "-V"]
        args = ["offline", "run-macos", "1.1.1.1", *command]
        with (
            patch.object(sys, "argv", args),
            patch.object(sys, "platform", "darwin"),
            patch.object(os, "geteuid", create=True, return_value=501),
            patch.object(network, "_connect") as connect,
            patch.object(os, "execv") as execute,
        ):
            with self.assertRaisesRegex(ValueError, "remains reachable"):
                network.main()
            connect.side_effect = TimeoutError(errno.ETIMEDOUT, "timed out")
            with self.assertRaises(TimeoutError):
                network.main()
            execute.assert_not_called()
            connect.side_effect = PermissionError(errno.EPERM, "denied")
            network.main()
            execute.assert_called_once_with(command[0], command)
