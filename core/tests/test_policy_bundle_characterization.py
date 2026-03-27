import uuid
import unittest

from core.app_jobs import _build_policy_bundle


class PolicyBundleCharacterizationTests(unittest.TestCase):
    def test_simple_policy_bundle_ordering_and_structure_snapshot(self):
        app_spec = {
            "app_slug": "simple-tasks",
            "title": "Simple Tasks",
            "entity_contracts": [
                {
                    "key": "tasks",
                    "singular_label": "task",
                    "plural_label": "tasks",
                    "fields": [
                        {"name": "id", "type": "uuid"},
                        {"name": "workspace_id", "type": "uuid"},
                        {"name": "status", "type": "string", "options": ["draft", "open", "done"]},
                    ],
                    "relationships": [],
                }
            ],
        }
        prompt = (
            "Core entities: 1. Task - status.\n"
            "Behavior: 1. Task status should move from draft to open to done.\n"
            "Validation: 1. Task status transitions must follow allowed order.\n"
        )

        bundle = _build_policy_bundle(workspace_id=uuid.uuid4(), app_spec=app_spec, raw_prompt=prompt)

        counts = {key: len(value) for key, value in (bundle.get("policies") or {}).items()}
        self.assertEqual(
            counts,
            {
                "validation_policies": 0,
                "relation_constraints": 0,
                "transition_policies": 3,
                "invariant_policies": 0,
                "derived_policies": 0,
                "trigger_policies": 0,
            },
        )

        transition_rows = bundle["policies"]["transition_policies"]
        self.assertEqual([row["id"] for row in transition_rows], ["simple-tasks-001", "simple-tasks-002", "simple-tasks-003"])
        self.assertEqual([row["enforcement_stage"] for row in transition_rows], ["not_compiled", "not_compiled", "runtime_enforced"])
        self.assertEqual((transition_rows[2].get("parameters") or {}).get("runtime_rule"), "field_transition_guard")
        self.assertEqual((transition_rows[2].get("targets") or {}).get("entity_keys"), ["tasks"])
        self.assertEqual((transition_rows[2].get("targets") or {}).get("field_names"), ["status"])

    def test_relationships_constraints_triggers_snapshot(self):
        app_spec = {
            "app_slug": "team-lunch-poll",
            "title": "Team Lunch Poll",
            "entity_contracts": [
                {
                    "key": "polls",
                    "singular_label": "poll",
                    "plural_label": "polls",
                    "fields": [
                        {"name": "id", "type": "uuid"},
                        {"name": "workspace_id", "type": "uuid"},
                        {"name": "status", "type": "string", "options": ["draft", "open", "selected", "closed"]},
                        {"name": "title", "type": "string"},
                        {"name": "option_count", "type": "number"},
                    ],
                    "relationships": [],
                },
                {
                    "key": "options",
                    "singular_label": "option",
                    "plural_label": "options",
                    "fields": [
                        {"name": "id", "type": "uuid"},
                        {"name": "workspace_id", "type": "uuid"},
                        {"name": "poll_id", "type": "uuid"},
                        {"name": "selected", "type": "string", "options": ["yes", "no"]},
                    ],
                    "relationships": [{"field": "poll_id", "target_entity": "polls", "target_field": "id"}],
                },
                {
                    "key": "votes",
                    "singular_label": "vote",
                    "plural_label": "votes",
                    "fields": [
                        {"name": "id", "type": "uuid"},
                        {"name": "workspace_id", "type": "uuid"},
                        {"name": "option_id", "type": "uuid"},
                        {"name": "poll_id", "type": "uuid"},
                    ],
                    "relationships": [
                        {"field": "option_id", "target_entity": "options", "target_field": "id"},
                        {"field": "poll_id", "target_entity": "polls", "target_field": "id"},
                    ],
                },
            ],
        }
        prompt = (
            "Core entities: 1. Poll. 2. Option. 3. Vote.\n"
            "Behavior:\n"
            "1. When a poll is selected, automatically mark one option as selected.\n"
            "2. Vote counts per option should roll up on polls.option_count.\n"
            "3. Exactly one option may be selected per poll.\n"
            "4. Polls in selected status must have one selected option.\n"
            "Validation:\n"
            "1. Votes must belong to the same poll referenced by option_id.\n"
            "2. Vote writes are only allowed when poll status is open.\n"
        )

        bundle = _build_policy_bundle(workspace_id=uuid.uuid4(), app_spec=app_spec, raw_prompt=prompt)
        policies = bundle.get("policies") or {}
        counts = {key: len(value) for key, value in policies.items()}
        self.assertEqual(
            counts,
            {
                "validation_policies": 1,
                "relation_constraints": 2,
                "transition_policies": 1,
                "invariant_policies": 2,
                "derived_policies": 1,
                "trigger_policies": 1,
            },
        )

        flattened = [row for family in policies.values() for row in family]
        flattened_view = [
            (
                row["id"],
                row["family"],
                row["enforcement_stage"],
                (row.get("parameters") or {}).get("runtime_rule"),
            )
            for row in flattened
        ]
        self.assertEqual(
            flattened_view,
            [
                ("team-lunch-poll-004", "validation_policies", "runtime_enforced", "parent_status_gate"),
                ("team-lunch-poll-002", "relation_constraints", "not_compiled", None),
                ("team-lunch-poll-003", "relation_constraints", "runtime_enforced", "match_related_field"),
                ("team-lunch-poll-005", "transition_policies", "runtime_enforced", "field_transition_guard"),
                ("team-lunch-poll-007", "invariant_policies", "runtime_enforced", "at_most_one_matching_child_per_parent"),
                ("team-lunch-poll-008", "invariant_policies", "runtime_enforced", "at_least_one_matching_child_per_parent"),
                ("team-lunch-poll-001", "derived_policies", "not_compiled", None),
                ("team-lunch-poll-006", "trigger_policies", "runtime_enforced", "post_write_related_update"),
            ],
        )

        relation_runtime = policies["relation_constraints"][1]
        self.assertEqual((relation_runtime.get("targets") or {}).get("entity_keys"), ["votes", "options", "polls"])
        self.assertEqual((relation_runtime.get("parameters") or {}).get("runtime_rule"), "match_related_field")


if __name__ == "__main__":
    unittest.main()
