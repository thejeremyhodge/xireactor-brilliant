"""Skill-side version constant — kept in lockstep with ``api/_version.py``
and ``mcp/_version.py`` plus the ``skill_version`` field in
``SKILL.md`` frontmatter.

Shipped inside ``brilliant-kb-assistant.zip`` so consumers of the skill
bundle can introspect the version without parsing markdown frontmatter.

Drift between this file, the SKILL.md frontmatter, and the api/mcp
version files is the symptom of a missed step in the release-cut dance
(see ``CONTRIBUTING.md`` "Cutting a release").
"""

SKILL_VERSION = "0.10.0"
