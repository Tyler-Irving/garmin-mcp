# Launch playbook — getting garmin-mcp in front of people

This is the non-code half of "make it more useful and get eyeballs." None of it is
automated because each step needs *your* accounts/credentials. Work top to bottom; the
first two items are the highest leverage.

---

## 1. The demo GIF (do this first — it's the README's single biggest conversion lever)

A repo with a 15–20s GIF at the top gets starred far more than one without. The new
`get_daily_briefing` tool *is* the demo: one prompt shows the whole value prop.

**Record this exact sequence in Claude Desktop** (screen recorder → trim → convert to GIF,
e.g. with `gifski` or an online MP4→GIF tool; keep it under ~3 MB so GitHub inlines it):

1. Type: **"Give me my Garmin morning briefing — should I train hard today?"**
2. Let Claude call `get_daily_briefing` and answer in prose (sleep + HRV + Body Battery +
   readiness + RHR vs. baseline, then its judgement).
3. Follow up: **"How does that compare to last Monday?"** to show it reasoning across days.

Then drop it at the very top of the README:

```markdown
<p align="center">
  <img src="docs/demo.gif" alt="Asking Claude for a Garmin morning briefing" width="720">
</p>
```

> Privacy note before you publish: your real health numbers will be in the GIF. Either use
> a throwaway/older day you're comfortable sharing, or blur the specific values.

---

## 2. Submit to the registries (where people actually browse for MCP servers)

### Official MCP registry
The `server.json` at the repo root is a starting manifest. Validate/publish with the
official tooling rather than trusting the hand-written file — the schema moves:

```bash
# Install the publisher CLI (see modelcontextprotocol/registry releases)
mcp-publisher init        # regenerates/validates server.json against the live schema
mcp-publisher login github # proves ownership of the io.github.Tyler-Irving/* namespace
mcp-publisher publish
```

The `io.github.Tyler-Irving/*` namespace is authenticated via your GitHub account, so no DNS
setup is needed.

### Community lists (open a PR to each)
- **`punkpeye/awesome-mcp-servers`** — the big one. Entry text below.
- **`wong2/awesome-mcp-servers`**
- **Smithery** (smithery.ai), **Glama** (glama.ai/mcp), **mcp.so** — mostly auto-index from
  the registry once you're listed, but you can submit directly too.

**awesome-mcp-servers entry** (drop under the Health/Fitness or Lifestyle section):

```markdown
- [Tyler-Irving/garmin-mcp](https://github.com/Tyler-Irving/garmin-mcp) 🐍 🏠 - Your Garmin
  Connect data (sleep, HRV, Body Battery, training load/readiness, a one-call daily
  briefing) as MCP tools. Read-only by default with one opt-in strength-workout write path.
```

(🐍 = Python, 🏠 = local/self-hosted — match the legend each list uses.)

---

## 3. The launch post (your distribution channel)

One short post with the GIF, cross-posted. Lead with the user benefit, not the tech.

**Title:** "I built an MCP server so I can just *ask* Claude about my Garmin data"

**Body skeleton:**
> The Garmin app shows every metric in its own silo. I wanted to ask one question —
> "given last night's sleep, my HRV, and my recent load, should I go hard today?" — and
> get one answer. So `garmin-mcp` exposes your Garmin Connect data to Claude as tools.
> `uvx garmin-mcp login`, add it to Claude Desktop, done. Read-only by default. [GIF]
> Repo: <link>

**Where:** r/Garmin, r/running (or the relevant sport sub), r/ClaudeAI, Hacker News
("Show HN"), and the MCP Discord #show-and-tell. Space them out over a few days; reply to
comments — engagement is what surfaces the post.

---

## 4. Lower-priority polish that helps adoption

- Make the `uvx garmin-mcp login` one-liner the *first* concrete thing in the README (above
  the JSON config block) — friction is the #1 adoption killer.
- Add repo topics if not already: `mcp`, `model-context-protocol`, `garmin`,
  `garmin-connect`, `claude`, `fitness`.
- Pin a "single-user by design — here's why" note so people don't file multi-tenant auth as
  a bug.
