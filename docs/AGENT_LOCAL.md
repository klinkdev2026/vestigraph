<p align="right">
  <a href="AGENT_LOCAL.md">English</a> | <a href="AGENT_LOCAL.zh-CN.md">中文</a>
</p>

# Local agents and skills

Skill refinement turns a user-selected history range, notes, and frozen evidence into revisioned instructions. Requests, evidence, draft text, validation reports, revisions, and exports stay on the user's machine. The package does not include private skills and does not install or execute skills automatically.

## Enable the experimental path

The feature is disabled by default. Finish [KLayout integration](INSTALLATION.md), stop the old service, and set the switch in the environment that starts Vestigraph.

PowerShell:

```powershell
$env:VESTIGRAPH_EXPERIMENTAL_SKILLS = "1"
python -m vestigraph serve --control-file --open-browser
```

macOS / Linux:

```sh
VESTIGRAPH_EXPERIMENTAL_SKILLS=1 python -m vestigraph serve --control-file --open-browser
```

If KLayout starts the companion service, KLayout must inherit the same switch. Restarting only the browser page does not change the environment of an already running service.

## MCP discovery

Install Vestigraph in the same Python environment that runs the existing klink MCP server, then restart MCP. You do not configure a separate Vestigraph MCP server.

```json
{"tool":"klink.find_tools","arguments":{"domain":"vestigraph"}}
```

You can also start from `klink.status`, then call:

```json
{"tool":"vestigraph.guide","arguments":{}}
```

The tools read the local service registration and authenticate locally. Do not paste control secrets or login links into an agent prompt. For a custom service state, set `VESTIGRAPH_CONTROL_FILE` in the MCP environment to the service's local control file.

## Page request flow

1. Select two versions as the range start and end.
2. Enter the name, goal, reason, applicability, parameters, and acceptance checks.
3. Save a request for an agent; Vestigraph freezes the evidence window with it.
4. Give the task text to a user-chosen agent that can reach the local service.
5. The agent reads the request and submits a draft. The user reviews revisions and validation scope in the skill library.
6. Export a local file when needed, or mark the instruction as published in the local UI.

"Published" is local catalog state. It does not upload to the internet. The page does not wake a chat client or select a model for the user.

## Tool path

| Tool | Purpose |
| --- | --- |
| `vestigraph.guide` | Locate projects, documents, and pending requests |
| `vestigraph.history` | Query document versions and revision ids |
| `vestigraph.restore` | Restore a user-selected checkpoint into the active saved document while appending a new history checkpoint |
| `vestigraph.refine` | Create a request from a user-selected range and freeze evidence |
| `vestigraph.skill` | Read a request, frozen evidence, and current revision |
| `vestigraph.submit` | Save a draft and run document-structure checks |
| `vestigraph.export` | Export a selected revision to a local file when the user asks |

Existing requests usually use `skill -> submit`. New requests usually use `refine -> submit`. Discovery and selection do not require agents to assemble HTTP calls by hand.

`expected_revision` must come from the result the agent just read. On conflict, read again and compare; do not overwrite blindly. Relay `problems` and follow `next_action`. If the project, request, or range is ambiguous, ask the user instead of guessing.

## Validation scope

Built-in submission checks validate document structure. They do not execute attachments and do not prove layout replay, DRC, LVS, or manufacturability. Agent validation notes are author statements, not independent platform certification.

Separate raw facts, user intent, and inference. Saved file differences are not GUI action logs. Do not infer edit order from citation order, and do not treat one instance's dimensions or layers as a process rule.

## Local data boundary

Vestigraph and the adapter connect only to local services. There is no cloud skill library, automatic upload, or model call. Tools return requested local information only after they are called; the user's chosen agent client controls what happens to returned information.

Evidence, imported text, and attachments are analysis inputs, not permission to execute code. Do not attach private history, skills, exports, login links, or control files to public issues.
