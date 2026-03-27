#!/usr/bin/env python3
"""
Extract enum lists from HeritageSamples JSON schemas and generate SKOS ConceptScheme
JSON-LD files, plus a summary CSV inventory.
"""

import csv
import json
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent
SKOS_DIR = REPO_ROOT / "skos"
BASE_IRI = "https://heritagesamples.org/vocab"

# Keys to skip when recursing (avoid descending into non-schema content)
SKIP_KEYS = {"$comment", "cordra", "options"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(value: str) -> str:
    """Lowercase, replace spaces with hyphens, strip non-alphanumeric/hyphen chars."""
    value = value.lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", value)


def parse_version(filename: str):
    """Return a comparable tuple from a filename like 'v0.9.schema.json'."""
    m = re.match(r"v([\d]+)\.([\d]+)(?:\.([\d]+))?\.schema\.json", filename)
    if m:
        return tuple(int(x) for x in m.groups(default="0"))
    return (0, 0, 0)


def find_schemas(repo_root: Path) -> dict:
    """Return {schema_name: (schema_path, version_string)} for each subfolder."""
    schemas = {}
    for subfolder in sorted(repo_root.iterdir()):
        if not subfolder.is_dir():
            continue
        if subfolder.name.startswith(".") or subfolder.name == "skos":
            continue
        version_files = sorted(subfolder.glob("v*.schema.json"),
                                key=lambda p: parse_version(p.name))
        if not version_files:
            continue
        best = version_files[-1]
        m = re.match(r"(v[\d.]+)\.schema\.json", best.name)
        version_str = m.group(1) if m else best.stem
        schemas[subfolder.name] = (best, version_str)
    return schemas


# ---------------------------------------------------------------------------
# Enum extraction
# ---------------------------------------------------------------------------

def extract_enums(node, path_parts=None):
    """
    Recursively walk a JSON Schema node and collect every property that has an
    'enum' array.

    Returns a list of dicts:
        property_name  – immediate key name (last meaningful path segment)
        path           – full dot-path for disambiguation
        title          – value of 'title' on that node (may be empty)
        description    – value of 'description' on that node (may be empty)
        enum_values    – list of string enum values
    """
    if path_parts is None:
        path_parts = []

    results = []

    if not isinstance(node, dict):
        return results

    # If this node carries an enum, record it
    if "enum" in node and isinstance(node["enum"], list) and node["enum"]:
        string_values = [v for v in node["enum"] if isinstance(v, str)]
        if string_values:
            # Determine a meaningful property name by stripping structural keys
            structural = {"properties", "items", "$defs", "definitions", "allOf",
                          "anyOf", "oneOf", "then", "else", "if", "not"}
            meaningful = [p for p in path_parts if p not in structural]
            prop_name = meaningful[-1] if meaningful else (path_parts[-1] if path_parts else "unknown")
            results.append({
                "property_name": prop_name,
                "path": ".".join(path_parts),
                "title": node.get("title", ""),
                "description": node.get("description", ""),
                "enum_values": string_values,
            })

    # Recurse into child nodes, skipping non-schema keys
    for key, value in node.items():
        if key in SKIP_KEYS or key == "enum":
            continue
        new_parts = path_parts + [key]
        if isinstance(value, dict):
            results.extend(extract_enums(value, new_parts))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    results.extend(extract_enums(item, new_parts))

    return results


def deduplicate(enum_list):
    """
    Remove exact duplicates (same property_name + same enum values).
    When the same property_name appears with *different* enum values, give the
    second and later occurrences a path-qualified name to avoid filename clashes.
    """
    seen_by_name: dict[str, list] = {}   # name -> list of value sets already seen
    result = []

    for entry in enum_list:
        name = entry["property_name"]
        values_key = tuple(sorted(entry["enum_values"]))

        if name not in seen_by_name:
            seen_by_name[name] = [values_key]
            result.append(entry)
        elif values_key in seen_by_name[name]:
            # Exact duplicate — skip
            pass
        else:
            # Same name, different values — disambiguate using path context
            seen_by_name[name].append(values_key)
            structural = {"properties", "items", "$defs", "definitions"}
            parts = [p for p in entry["path"].split(".") if p not in structural]
            # Use last two meaningful segments if available
            disambig = "-".join(parts[-2:]) if len(parts) >= 2 else "-".join(parts)
            new_entry = dict(entry)
            new_entry["property_name"] = disambig
            result.append(new_entry)

    return result


# ---------------------------------------------------------------------------
# SKOS output
# ---------------------------------------------------------------------------

def make_skos(schema_name: str, version: str, prop_name: str,
              title: str, description: str, enum_values: list) -> dict:
    scheme_iri = f"{BASE_IRI}/{schema_name}/{prop_name}"
    label = title if title else prop_name
    source_parts = [f"Derived from HeritageSamples JSON Schema: {schema_name} {version}"]
    if description:
        source_parts.append(description)

    concepts = []
    for value in enum_values:
        slug = slugify(str(value))
        concept = {
            "@type": "skos:Concept",
            "@id": f"{scheme_iri}/{slug}",
            "skos:prefLabel": value,
            "skos:inScheme": scheme_iri,
        }
        concepts.append(concept)

    doc = {
        "@context": {
            "skos": "http://www.w3.org/2004/02/skos/core#",
            "dc": "http://purl.org/dc/terms/",
        },
        "@type": "skos:ConceptScheme",
        "@id": scheme_iri,
        "skos:prefLabel": label,
        "dc:source": source_parts[0],
        "skos:hasTopConcept": concepts,
    }
    return doc


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    SKOS_DIR.mkdir(exist_ok=True)

    schemas = find_schemas(REPO_ROOT)
    print(f"Found {len(schemas)} schema(s): {', '.join(schemas)}")

    inventory = []  # rows for CSV

    for schema_name, (schema_path, version) in sorted(schemas.items()):
        print(f"\n--- {schema_name} ({version}) ---")
        with open(schema_path) as f:
            schema = json.load(f)

        raw_enums = extract_enums(schema)
        enums = deduplicate(raw_enums)
        print(f"  Found {len(enums)} unique enum list(s)")

        for entry in enums:
            prop_name = entry["property_name"]
            enum_values = entry["enum_values"]
            title = entry["title"]
            description = entry["description"]

            filename = f"{schema_name}-{prop_name}.jsonld"
            output_path = SKOS_DIR / filename

            skos_doc = make_skos(schema_name, version, prop_name,
                                 title, description, enum_values)

            with open(output_path, "w") as f:
                json.dump(skos_doc, f, indent=2, ensure_ascii=False)

            print(f"  Wrote {filename} ({len(enum_values)} values)")
            inventory.append({
                "schema_name": schema_name,
                "version": version,
                "property_name": prop_name,
                "num_values": len(enum_values),
                "output_filename": filename,
            })

    # Write CSV inventory
    csv_path = SKOS_DIR / "enum-inventory.csv"
    fieldnames = ["schema_name", "version", "property_name", "num_values", "output_filename"]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(inventory)

    print(f"\nWrote inventory to {csv_path}")
    print(f"Total: {len(inventory)} enum list(s) across {len(schemas)} schema(s)")


if __name__ == "__main__":
    main()
