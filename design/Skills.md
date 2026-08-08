# Skills Design Document

- Status: implemented
- Parent document: `DESIGN.md` (§5.7, §8)
- This document is the detailed design of **Agent Skills** in expecto:
  instructions the user writes for a particular kind of task, listed in
  the system prompt and pulled in on demand through a tool.

---

## 1. Purpose

AGENTS.md carries instructions that apply to *every* query, so it has to
stay small. A skill is the opposite shape: a long, specific procedure —
how this site upgrades a router, how to work through a BGP session that
will not establish — that is worth many paragraphs but only matters for
the occasional query. Listing every such procedure up front would spend
the context window on instructions that are almost always irrelevant.

So expecto advertises only each skill's name and description, and lets
the model pull in the body when the goal matches.

## 2. Layout

```
~/.expecto/skills/                 # or the skills_dir config key
├── bgp-troubleshooting/
│   ├── SKILL.md
│   └── checklist.md               # bundled file, served by load_skill
└── junos-upgrade/
    └── SKILL.md
```

```markdown
---
name: bgp-troubleshooting
description: Diagnose BGP sessions stuck in Idle/Active on Junos and IOS. Use when a peer will not establish or routes are missing.
---

# BGP troubleshooting

1. Check the session state with `show bgp summary`.
...
```

- **The directory name is the skill's identity.** A frontmatter `name`
  that disagrees is a warning, not a second name.
- **`description` is the load-bearing field.** It is the only part that
  reaches the system prompt, so it is what the model decides on: say what
  the skill covers *and when to use it*. A skill without one is warned
  about and left out — an unselectable skill is worse than no skill.
- Frontmatter is parsed as flat `key: value` lines, which is all a
  SKILL.md header uses; keys expecto does not know (`license`,
  `allowed-tools`, …) are ignored. No YAML dependency is taken.
- Everything is read at startup, bodies included: local files are small,
  and a lookup at call time cannot then fail. Editing a SKILL.md mid
  session has no effect until expecto restarts.
- Skills are sorted by name so the system prompt they feed is
  byte-identical across runs (Conversation.md §2).
- Unusable entries — no SKILL.md, no frontmatter, no description — are
  reported on stderr at startup and skipped; a missing skills directory is
  normal and simply yields no skills.

## 3. In the system prompt

When at least one skill loaded, a catalog is appended to the base prompt
(before the MCP note and AGENTS.md, all of them fixed at startup):

```
# Skills

Skills are instructions the user wrote for a particular kind of task.
When the goal matches one of them, call load_skill with its name and
follow what it returns before acting. Load only the ones you need.

Available skills:
- bgp-troubleshooting: Diagnose BGP sessions stuck in Idle/Active ...
- junos-upgrade: ...
```

## 4. The load_skill tool

| tool | arguments | behavior |
|------|-----------|----------|
| `load_skill` | `name: string`, `file: string` | Returns that skill's SKILL.md body, or — when `file` is non-empty — a file bundled with the skill |

- Registered with `strict: true` and validated with `LoadSkillParams`,
  like expecto's other own tools. **It is only offered when skills
  exist** — an empty tool is worse than none. `file` is required by
  strict mode; the empty string means the skill itself.
- The Executor handles it synchronously: the skills were read at startup,
  so there is nothing to send to the terminal and nothing to wait for. The
  load is shown on screen (`[expecto: skill <name>]`, or
  `[expecto: skill <name>/<file>]`) so it is as visible as a command.
- **Files are read by expecto, never through `run_command`.** The skills
  live on the machine expecto runs on, while the terminal may be an ssh
  session or a device CLI where those paths do not exist — the tool
  description and the rendered skill both say so, and the skill's local
  path is deliberately not shown, so the model is not tempted to `cat`
  it.
- A skill's rendered body ends with the list of files bundled beside it
  and how to fetch one, so a procedure can refer to its own checklist or
  script.
- `file` is resolved against the skill directory and must stay inside it
  (`..` and absolute paths are refused): `load_skill` is not a
  general-purpose file reader.
- An unknown skill name, an escaping path, or an unreadable file all come
  back as tool-result text — listing the available skills or the skill's
  bundled files — rather than an abort, so the model can correct itself.

## 5. Design-Decision Record

1. **Description in the prompt, body behind a tool.** The whole point is
   to keep long procedures out of the prompt until they are relevant.
2. **`load_skill` serves bundled files too, rather than pointing the
   model at `run_command`.** Reading them through the terminal only works
   when the terminal happens to be a local shell — and the situations a
   skill is written for (a router upgrade, a device stuck mid-migration)
   are exactly the ones where it is not.
3. **Read at startup, not on demand.** Simpler, and it keeps `load_skill`
   free of I/O failures mid-session. The cost — edits need a restart — is
   the same one AGENTS.md already has.
4. **Flat `key: value` frontmatter parsing.** A YAML dependency for three
   scalar fields is not worth it, and the repo already reads its config
   with stdlib-only parsing.

## 6. Future work

- Capping or truncating a very large SKILL.md body (today the user's own
  file is trusted as-is).
- Project-local skills (a `./.expecto/skills` alongside the user-level
  directory).
