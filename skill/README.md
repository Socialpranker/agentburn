# agentburn as a skill

Teach your agent to answer **"where do you burn my money?"** about itself.
The skill wraps the local `agentburn` CLI — read-only, zero deps, local-only.

## Install

**Hermes Agent** (skills are rescanned from `~/.hermes/skills/`):

```bash
mkdir -p ~/.hermes/skills/agentburn
curl -fsSL https://raw.githubusercontent.com/Socialpranker/agentburn/main/skill/agentburn/SKILL.md \
  -o ~/.hermes/skills/agentburn/SKILL.md
# then in Hermes: reload skills
```

**OpenClaw** (workspace skills directory):

```bash
mkdir -p ~/.openclaw/skills/agentburn   # or <workspace>/skills/agentburn
curl -fsSL https://raw.githubusercontent.com/Socialpranker/agentburn/main/skill/agentburn/SKILL.md \
  -o ~/.openclaw/skills/agentburn/SKILL.md
```

**Claude Code**:

```bash
mkdir -p ~/.claude/skills/agentburn
curl -fsSL https://raw.githubusercontent.com/Socialpranker/agentburn/main/skill/agentburn/SKILL.md \
  -o ~/.claude/skills/agentburn/SKILL.md
```

Then just ask the agent: *"сколько ты сжёг за ночь?"* / *"where do my tokens go?"*

Prefer structured tools over skills? Use the MCP server instead: `agentburn mcp`
(`claude mcp add agentburn -- agentburn mcp`).
