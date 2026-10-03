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

Example: `0.3.0` -> `0.3.1`.

### MINOR

Increment MINOR when compatible new functionality is introduced.

Examples include:

- New features
- New commands or options
- New UI functionality
- Meaningful enhancements to existing features
- New configuration capabilities

Example: `0.3.1` -> `0.4.0`.

Reset PATCH to `0` whenever MINOR is incremented.

### MAJOR

Once the project has reached `1.0.0`, increment MAJOR for intentional breaking changes.

Examples include:

- Breaking API changes
- Removing established functionality
- Incompatible configuration changes
- Changes requiring users or dependent software to adapt

Example: `1.4.2` -> `2.0.0`.

Reset MINOR and PATCH to `0` whenever MAJOR is incremented.

### Pre-1.0 Development

This project is currently in `0.x` development. While the version remains below `1.0.0`:

- Bug fixes increment PATCH.
- Compatible new features increment MINOR.
- Significant or breaking development changes should normally increment MINOR.
- Do not automatically change the project to `1.0.0`.

Version `1.0.0` must only be used when the developers intentionally decide that the project has reached its first stable release.

### One-Time Version History Correction

The historical `v1.2.0` release was created before this version policy and is not
considered part of the authoritative SemVer sequence.

For the `Polished4Windows` promotion only, the project may reset from `1.2.0` to
`0.7.0`. This exception does not permit any future version decrease or reuse.
After this correction, normal version progression resumes from `0.7.0`.

### Required Check Before Every Push

Before every push, the agent must:

1. Read the current authoritative version from `VERSION`.
2. Review all changes included in the push, including changes already present in the branch or working tree.
3. Determine the highest SemVer increment justified by those changes.
4. Update `VERSION` before pushing.
5. Update and verify all required references to the project version so they remain consistent with `VERSION`.
6. Never reuse or decrease a version number, except for the documented one-time
   version history correction above.
7. Never arbitrarily bump the version beyond what the changes justify.

If multiple types of changes are included, use the highest applicable increment.

Examples:

- Several bug fixes require a PATCH increment.
- Bug fixes plus a new feature require a MINOR increment.
- After `1.0.0`, new features plus a breaking change require a MAJOR increment.
