"""BossSession: one per voice conversation, with an ordered timeline that
survives everything.

Run with:  python3 -m unittest tests.test_boss_timeline -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conductor.boss_session import (BossSession, BossSessionStore,
                                    input_summary, output_summary,
                                    render_timeline, tool_label)


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = BossSessionStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class OneBossPerConversation(Store):
    def test_the_product_starts_inside_a_conversation(self):
        first = self.store.current_conversation()
        self.assertTrue(first.startswith("conv_"))
        self.assertEqual(self.store.current_conversation(), first)
        self.assertIsNone(self.store.current_boss())

    def test_binding_is_persisted_and_canonical(self):
        conv = self.store.current_conversation()
        boss = BossSession(id="boss_1", conversation_id=conv)
        self.store.save(boss)
        self.store.bind(conv, "boss_1")
        again = BossSessionStore(self.tmp.name)       # a restart
        self.assertEqual(again.current_boss().id, "boss_1")
        self.assertEqual(again.boss_for(conv).id, "boss_1")

    def test_new_voice_chat_means_a_new_conversation_not_a_new_window(self):
        conv = self.store.current_conversation()
        self.store.save(BossSession(id="boss_1", conversation_id=conv))
        self.store.bind(conv, "boss_1")
        fresh = self.store.new_conversation()
        self.assertNotEqual(fresh, conv)
        self.assertIsNone(self.store.current_boss())
        self.assertEqual(self.store.boss_for(conv).id, "boss_1")   # kept

    def test_an_old_conversation_can_be_made_current_again(self):
        conv = self.store.current_conversation()
        self.store.save(BossSession(id="boss_1", conversation_id=conv))
        self.store.bind(conv, "boss_1")
        self.store.new_conversation()
        self.assertIsNone(self.store.current_boss())
        self.store.resume_conversation(conv)
        self.assertEqual(self.store.current_conversation(), conv)
        self.assertEqual(self.store.current_boss().id, "boss_1")

    def test_an_unknown_conversation_refuses_to_resume(self):
        self.store.current_conversation()
        with self.assertRaises(KeyError):
            self.store.resume_conversation("conv_nope")

    def test_the_provider_session_id_is_part_of_the_record(self):
        boss = BossSession(id="boss_1", conversation_id="c",
                           provider_session_id="209bb486")
        self.store.save(boss)
        self.assertEqual(self.store.get("boss_1").provider_session_id, "209bb486")

    def test_children_are_persisted_on_the_boss(self):
        boss = BossSession(id="boss_1", conversation_id="c",
                           child_subagent_ids=["sub_a", "sub_b"])
        self.store.save(boss)
        self.assertEqual(self.store.get("boss_1").child_subagent_ids,
                         ["sub_a", "sub_b"])


class TheTimelineIsOrderedAndDurable(Store):
    def test_sequence_is_monotonic_across_restarts(self):
        self.store.append("boss_1", "user_message", {"text": "hi"})
        self.store.append("boss_1", "boss_message", {"text": "hello"})
        again = BossSessionStore(self.tmp.name)
        again.append("boss_1", "user_message", {"text": "more"})
        events = again.events("boss_1")
        self.assertEqual([e.sequence for e in events], [1, 2, 3])
        self.assertEqual([e.type for e in events],
                         ["user_message", "boss_message", "user_message"])

    def test_every_event_type_is_accepted_and_nothing_else(self):
        from conductor.boss_session import EVENT_TYPES
        for type_ in EVENT_TYPES:
            self.store.append("boss_1", type_, {})
        with self.assertRaises(ValueError):
            self.store.append("boss_1", "chain_of_thought", {})

    def test_trace_ids_travel(self):
        event = self.store.append("boss_1", "tool_started", {}, trace_id="trc_9")
        self.assertEqual(self.store.events("boss_1")[0].trace_id, "trc_9")
        self.assertEqual(event.trace_id, "trc_9")


class RenderingReadsLikeCodex(unittest.TestCase):
    def events(self, store, *specs):
        for type_, payload in specs:
            store.append("boss_1", type_, payload)
        return store.events("boss_1")

    def test_a_tool_is_one_item_from_start_to_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BossSessionStore(tmp)
            events = self.events(
                store,
                ("user_message", {"text": "See if my changes were pushed", "source": "voice"}),
                ("tool_started", {"execution_id": "x1", "tool": "find_project",
                                  "args": {"query": "voice agent"}}),
                ("tool_completed", {"execution_id": "x1", "tool": "find_project",
                                    "output_summary": "~/Developer/voice-agent"}),
                ("boss_message", {"text": "Two commits are unpushed."}))
        text = render_timeline(events, title="New voice chat")
        self.assertIn("YOU\n  See if my changes were pushed", text)
        self.assertIn('✓ Agent Control · Find project\n  "voice agent"\n  ~/Developer/voice-agent', text)
        self.assertNotIn("▶ Agent Control · Find project", text, "start and finish rendered twice")
        self.assertIn("BOSS\n  Two commits are unpushed.", text)

    def test_a_failed_tool_shows_the_error_on_the_same_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BossSessionStore(tmp)
            events = self.events(
                store,
                ("tool_started", {"execution_id": "x1", "tool": "inspect_project",
                                  "args": {"project_id": "p"}}),
                ("tool_failed", {"execution_id": "x1", "tool": "inspect_project",
                                 "error": "no such project"}))
        text = render_timeline(events)
        self.assertIn("! Agent Control · Inspect project\n  p\n  no such project", text)

    def test_worker_actions_carry_the_childs_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BossSessionStore(tmp)
            events = self.events(
                store,
                ("subagent_created", {"task_id": "task_a", "subagent_id": "sub_task_a",
                                      "title": "Posely · Fix login"}),
                ("subagent_messaged", {"task_id": "task_a", "title": "Posely · Fix login",
                                       "message": "rerun the auth tests"}),
                ("subagent_event_received", {"task_id": "task_a", "title": "Posely · Fix login",
                                             "type": "completed", "summary": "12 tests passed"}))
        text = render_timeline(events)
        self.assertIn("◉ Worker started\n  Posely · Fix login    [Open sub_task_a]", text)
        self.assertIn('◉ Worker messaged\n  Posely · Fix login\n  "rerun the auth tests"', text)
        self.assertIn("◉ Worker update\n  Posely · Fix login\n  12 tests passed", text)

    def test_typed_turns_are_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BossSessionStore(tmp)
            events = self.events(store, ("user_message", {"text": "also billing",
                                                          "source": "typed"}))
        self.assertIn("YOU (typed)\n  also billing", render_timeline(events))

    def test_no_raw_json_on_the_page(self):
        self.assertEqual(input_summary("create_task",
                                       {"project_id": "p", "title": "Fix login",
                                        "goal": "..."}), "Fix login")
        self.assertEqual(input_summary("send_to_task",
                                       {"task_id": "task_a", "message": "go"}),
                         'task_a: "go"')
        self.assertEqual(output_summary("create_task",
                                        '{"task_id": "task_a", "title": "Fix login", "status": "running"}'),
                         "Fix login (task_a)")
        self.assertEqual(output_summary("list_tasks", "[]"), "nothing")
        self.assertEqual(tool_label("find_project"), "Agent Control · Find project")
        self.assertEqual(tool_label("create_task"), "Agent Control · Start worker")


if __name__ == "__main__":
    unittest.main()
