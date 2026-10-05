# Agent Instructions

## Semantic Versioning

This project strictly follows Semantic Versioning using `MAJOR.MINOR.PATCH`.

The authoritative project version is the value in the root `VERSION` file. Any required version references in project metadata or source code must remain consistent with it.

### PATCH

Increment PATCH for fixes and changes that do not add significant new functionality or intentionally break compatibility.

Examples include:

- Bug fixes
- Small UI fixes
- Refactoring without changed external behavior
- Minor configuration corrections
- Performance improvements
- Documentation corrections

Example: `1.3.0` -> `1.3.1`.

### MINOR

Increment MINOR when compatible new functionality is introduced.

Examples include:

- New features
- New commands or options
- New UI functionality
- Meaningful enhancements to existing features
- New configuration capabilities

Example: `1.3.1` -> `1.4.0`.

Reset PATCH to `0` whenever MINOR is incremented.

### MAJOR

Increment MAJOR for intentional breaking changes.

Examples include:

- Breaking API changes
- Removing established functionality
- Incompatible configuration changes
- Changes requiring users or dependent software to adapt

Example: `1.4.2` -> `2.0.0`.

Reset MINOR and PATCH to `0` whenever MAJOR is incremented.

### Post-1.0 Status

Version `1.2.0` was published as a GitHub release, and installed copies compare
release tags against their own version to decide whether to offer an update. The
project is therefore past `1.0.0`: never return to a `0.x` version, or existing
installs will stop seeing updates.

- Bug fixes increment PATCH.
- Compatible new features increment MINOR.
- Intentional breaking changes increment MAJOR.

### Required Check Before Every Push

Before every push, the agent must:

1. Read the current authoritative version from `VERSION`.
2. Review all changes included in the push, including changes already present in the branch or working tree.
3. Determine the highest SemVer increment justified by those changes.
4. Update `VERSION` before pushing.
5. Update and verify all required references to the project version so they remain consistent with `VERSION`.
6. Never reuse or decrease a version number.
7. Never arbitrarily bump the version beyond what the changes justify.

If multiple types of changes are included, use the highest applicable increment.

Examples:

- Several bug fixes require a PATCH increment.
- Bug fixes plus a new feature require a MINOR increment.
- New features plus a breaking change require a MAJOR increment.
