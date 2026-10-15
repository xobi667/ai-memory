# ai-memory

[简体中文](README.md) · English

A project memory and documentation Skill for AI agents. Create project docs, organize existing material, save progress, and resume work in a new session.

## Ask your agent to install and start

Send this to Codex, Claude Code, Cursor, or another agent that supports Skills:

```text
Install this Skill for me: https://github.com/xobi667/ai-memory
Then use ai-memory to inspect this project and its existing docs,
add the documentation it needs, organize docs by purpose,
and record the current progress and next steps.
```

The agent reads the repository's Skill instructions, installs it using the current client's supported method, and works on your project documentation.

## Say what you need

| Task | Message to your agent |
|---|---|
| Create docs | Use ai-memory to create the documentation this project needs. |
| Organize material | Use ai-memory to read the existing docs, organize them by purpose, and repair links. |
| Save progress | Use ai-memory to record these changes, test results, and next steps. |
| Resume in a new session | Use ai-memory to read the project records, check the current state, and continue the previous task. |
| Check documentation | Use ai-memory to compare the docs with the implementation and correct outdated content. |

## Keep documents in your project

Reuse existing folders and naming conventions. A short task can use one handoff document; structured records support multiple tasks and traceable history.

Markdown documents with an unclear purpose can go into a pending-classification folder, with their sources and open questions recorded. Records remain plain Markdown for subsequent agents to read.

[Skill entry point](SKILL.md) · [Document organization](references/documents.md) · [Record and resume](references/lifecycle.md) · [Report an issue](https://github.com/xobi667/ai-memory/issues)

[LICENSE](LICENSE) · [Attribution and provenance](NOTICE.md)
