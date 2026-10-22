"""docs/config.schema.json and wakeci/config_check.py describe the same file; these tests
keep them in step. The validator below covers the JSON Schema keywords the schema uses."""

import copy
import json
import re
import unittest
from pathlib import Path

from wakeci import config_check

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "docs" / "config.schema.json").read_text(encoding="utf-8"))
EXAMPLE = json.loads((ROOT / "controller" / "config.example.json").read_text(encoding="utf-8"))
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


SUPPORTED = {
    "$schema",
    "$id",
    "$ref",
    "$defs",
    "title",
    "description",
    "default",
    "type",
    "enum",
    "pattern",
    "minLength",
    "minimum",
    "exclusiveMinimum",
    "minItems",
    "items",
    "properties",
    "required",
    "additionalProperties",
}


def keywords(schema):
    """Every keyword the schema uses, so an unsupported or misspelt one fails a test
    instead of being ignored by this validator."""
    found = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            found.add(key)
            if key in ("properties", "$defs"):
                for sub in value.values():
                    found |= keywords(sub)
            elif isinstance(value, dict):
                found |= keywords(value)
    return found


def errors(value, schema, where="$"):
    if "$ref" in schema:
        schema = SCHEMA["$defs"][schema["$ref"].split("/")[-1]]
    out = []
    t = schema.get("type")
    if t == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif t == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif t:
        ok = isinstance(value, TYPES[t])
    else:
        ok = True
    if not ok:
        return [f"{where}: not {t}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where}: not one of {schema['enum']}")
    # JSON Schema patterns are ECMA regexes, where "$" does not match before a trailing
    # newline as Python's does.
    if "pattern" in schema and not re.search(schema["pattern"].replace("$", r"\Z"), value):
        out.append(f"{where}: does not match {schema['pattern']}")
    if "minLength" in schema and len(value) < schema["minLength"]:
        out.append(f"{where}: too short")
    if "minimum" in schema and value < schema["minimum"]:
        out.append(f"{where}: below {schema['minimum']}")
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        out.append(f"{where}: not above {schema['exclusiveMinimum']}")
    if t == "array":
        if len(value) < schema.get("minItems", 0):
            out.append(f"{where}: too few items")
        for i, item in enumerate(value):
            out += errors(item, schema.get("items", {}), f"{where}[{i}]")
    if t == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{where}: missing {key}")
        for key, item in value.items():
            if key in props:
                out += errors(item, props[key], f"{where}.{key}")
            elif schema.get("additionalProperties") is False:
                out.append(f"{where}: unknown key {key}")
    return out


def props(*path):
    s = SCHEMA
    for p in path:
        s = s["properties"][p]
        if s.get("type") == "array":
            s = s["items"]
    return set(s["properties"])


class SchemaTest(unittest.TestCase):
    def test_a_mac_without_separators_is_valid_in_both(self):
        cfg = copy.deepcopy(EXAMPLE)
        cfg["machines"][0]["mac"] = "00005e005301"
        self.assertEqual(errors(cfg, SCHEMA), [])

    def test_the_schema_uses_only_keywords_this_validator_checks(self):
        self.assertEqual(keywords(SCHEMA) - SUPPORTED, set())

    def test_the_example_matches_the_schema(self):
        self.assertEqual(errors(EXAMPLE, SCHEMA), [])

    def test_the_schema_knows_exactly_the_keys_the_checker_knows(self):
        self.assertEqual(props(), config_check.TOP_KEYS)
        self.assertEqual(props("sources"), config_check.SOURCE_KEYS)
        self.assertEqual(props("machines"), config_check.MACHINE_KEYS)
        self.assertEqual(props("notify"), config_check.NOTIFY_KEYS)

    def test_the_schema_and_the_checker_agree_on_the_config_version_and_the_etc_directory(self):
        self.assertEqual(SCHEMA["properties"]["config_version"]["enum"], [config_check.CONFIG_VERSION])
        self.assertTrue(SCHEMA["$defs"]["etcFile"]["pattern"].startswith("^" + config_check.ETC + "/"))

    def test_the_schema_catches_typical_mistakes(self):
        for change in (
            lambda c: c.update(idle_minute=30),
            lambda c: c.update(dry_run="yes"),
            lambda c: c["machines"][0].update(mac="00:00:5e"),
            lambda c: c["machines"][0].update(colour="red"),
            lambda c: c["sources"][0].update(token_file="/tmp/t"),
            lambda c: c["notify"].update(format="teams"),
            lambda c: c.update(config_version=2),
            lambda c: c.update(config_version=True),
            lambda c: c["machines"][0].update(rank=1.5),
            lambda c: c.update(ssh_key="/etc/wakeci/id_ed25519\n"),
            lambda c: c.update(state_file="/var/lib/wakeci/s.json\n"),
            lambda c: c["machines"][0].update(name='host"1'),
            lambda c: c["machines"][0].update(runner_prefixes=["a\n"]),
        ):
            cfg = copy.deepcopy(EXAMPLE)
            change(cfg)
            self.assertTrue(errors(cfg, SCHEMA), cfg)


if __name__ == "__main__":
    unittest.main()
