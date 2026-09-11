"""The cmux control client, and the parsing that turns text into ids.

Every method was exercised against cmux 0.64.22 before being written; what
these tests hold is the part that silently rots - parsing its output, and
failing in the right way when it is not there.

The rule that matters: unavailable is not the same as failed. cmux not
installed, not running, and refusing us are three different situations
with one meaning for a caller - use a different surface - and none of them
should look like a command that went wrong.

Run with:  python3 -m unittest tests.test_cmux_client -v
"""

from __future__ import annotations

import unittest
from unittest import mock

from conductor.cmux_client import (CmuxClient, CmuxUnavailable, _surfaces,
                                   _workspaces)

WORKSPACES = (
    "cmux: 'list-workspaces' is now an alias for 'cmux workspace list'.\n"
    "* workspace:1 40DCEB03-FE64-4924-8B43-893A7275BE80  ~  [selected]\n"
    "  workspace:2 9DA832B9-9317-4779-BA34-7A57CDAE6D1D  Posely · Fix login\n"
)
SURFACES = ("* surface:2 E1260737-897B-44CE-BF7D-E8D191AE6C34  "
            "Posely · Fix login  [selected]\n")


class ParsingRealOutput(unittest.TestCase):
    def test_workspaces_carry_stable_uuids(self):
        found = _workspaces(WORKSPACES)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[1].id, "9DA832B9-9317-4779-BA34-7A57CDAE6D1D")
        self.assertEqual(found[1].title, "Posely · Fix login")

    def test_the_deprecation_notice_is_not_a_workspace(self):
        """cmux prints an alias notice on stdout; parsing it as a row would
        invent a workspace that does not exist."""
        self.assertTrue(all("alias" not in w.title
                            for w in _workspaces(WORKSPACES)))

    def test_selection_is_read_but_is_not_identity(self):
        found = _workspaces(WORKSPACES)
        self.assertTrue(found[0].selected)
        self.assertFalse(found[1].selected)
        self.assertNotIn("[selected]", found[0].title)

    def test_a_title_with_spaces_survives(self):
        """Titles carry task identity - "Posely · Fix login" - so splitting
        on whitespace and keeping one field would mangle every one."""
        self.assertEqual(_workspaces(WORKSPACES)[1].title,
                         "Posely · Fix login")

    def test_surfaces_parse_the_same_way(self):
        found = _surfaces(SURFACES)
        self.assertEqual(found[0].id, "E1260737-897B-44CE-BF7D-E8D191AE6C34")
        self.assertTrue(found[0].selected)

    def test_empty_output_is_no_workspaces_not_an_error(self):
        self.assertEqual(_workspaces(""), [])
        self.assertEqual(_surfaces("\n\n"), [])


class UnavailableIsNotFailure(unittest.IsolatedAsyncioTestCase):
    """Three ways cmux is not there, one meaning for the caller."""

    def client(self) -> CmuxClient:
        return CmuxClient(binary="cmux", password="x")

    async def run_with(self, stdout: str, code: int):
        proc = mock.AsyncMock()
        proc.returncode = code
        proc.communicate = mock.AsyncMock(return_value=(stdout.encode(), b""))
        with mock.patch("asyncio.create_subprocess_exec",
                        new=mock.AsyncMock(return_value=proc)):
            return await self.client()._run("ping")

    async def test_not_running_is_unavailable(self):
        with self.assertRaises(CmuxUnavailable) as caught:
            await self.run_with("Error: Socket not found at /x/cmux.sock", 1)
        self.assertIn("not running", str(caught.exception))

    async def test_denied_access_says_how_to_fix_it(self):
        """The default mode is cmuxOnly and we are outside cmux, so this is
        the first thing anyone hits. The error carries the setting."""
        with self.assertRaises(CmuxUnavailable) as caught:
            await self.run_with(
                "ERROR: Access denied - only processes started inside cmux "
                "can connect", 1)
        self.assertIn("socketControlMode", str(caught.exception))

    async def test_not_installed_is_unavailable(self):
        with mock.patch("asyncio.create_subprocess_exec",
                        side_effect=OSError("No such file")):
            with self.assertRaises(CmuxUnavailable):
                await self.client()._run("ping")

    async def test_ping_answers_false_rather_than_raising(self):
        with mock.patch("asyncio.create_subprocess_exec",
                        side_effect=OSError("nope")):
            self.assertFalse(await self.client().ping())

    async def test_an_ordinary_command_failure_is_not_unavailable(self):
        """A bad argument is a bug in us, not a missing cmux - conflating
        them would send the caller off to another surface over a typo."""
        with self.assertRaises(RuntimeError) as caught:
            await self.run_with("Error: unknown workspace", 1)
        self.assertNotIsInstance(caught.exception, CmuxUnavailable)


class TargetingIsByIdNeverByFocus(unittest.IsolatedAsyncioTestCase):
    """Section 4 of the spec: never identify a worker by title, terminal
    title, or whichever surface happens to be focused."""

    async def test_send_addresses_the_surface_explicitly(self):
        client = CmuxClient(binary="cmux", password="x")
        with mock.patch.object(client, "_run",
                               new=mock.AsyncMock(return_value="OK")) as run:
            await client.send("SURFACE-UUID", "hello")
        calls = [c.args for c in run.await_args_list]
        self.assertIn(("send", "--surface", "SURFACE-UUID", "hello"), calls)
        self.assertIn(("send-key", "--surface", "SURFACE-UUID", "Enter"),
                      calls)

    async def test_focus_addresses_the_workspace_explicitly(self):
        client = CmuxClient(binary="cmux", password="x")
        with mock.patch.object(client, "_run",
                               new=mock.AsyncMock(return_value="OK")) as run:
            await client.focus_workspace("WS-UUID")
        self.assertEqual(run.await_args.args,
                         ("select-workspace", "--workspace", "WS-UUID"))

    async def test_a_missing_surface_reads_as_absent_not_as_an_error(self):
        client = CmuxClient(binary="cmux", password="x")
        with mock.patch.object(client, "_run",
                               new=mock.AsyncMock(
                                   side_effect=CmuxUnavailable("gone"))):
            self.assertFalse(await client.surface_exists("w", "s"))


if __name__ == "__main__":
    unittest.main()
