# SRE Agent conversation clients

Two Python command-line clients for asking an existing Azure SRE Agent questions
and continuing conversations:

| Client | Transport | Dependencies |
| --- | --- | --- |
| [sre_chat_api.py](sre_chat_api.py) | Direct REST API (simplest setup) | Python, Azure CLI, Azure Identity, HTTPX |
| [sre_chat.py](sre_chat.py) | Local Azure MCP server over stdio | Python, Azure CLI, Node.js/npm, MCP Python SDK |

Both use [sre_chat_common.py](sre_chat_common.py); keep that file beside the
scripts. Neither client creates an agent or provisions Azure resources.

## Prerequisites

- Python **3.11 or later**, available as `python`.
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli),
  available as `az`.
- An existing Azure SRE Agent and an account authorized to read its Azure
  resource and use its conversations. Ask your Azure administrator if needed.
- Your **subscription ID**, **resource group**, and **agent resource name**.
  Find these in the Azure portal on the agent's Overview / JSON View pages.
- For the MCP client only: a supported
  [Node.js LTS release](https://nodejs.org/) with npm and `npx`.

The direct API client supports the Azure public cloud. The SRE Agent APIs are
in preview and their contract may change.

## Quick start: direct API (PowerShell)

### 1. Download and open the project

Clone this repository using its GitHub **Code** URL, or download and extract
its ZIP. Open PowerShell in the folder containing this README.

Check your tools:

```powershell
python --version
az version
```

### 2. Create a virtual environment and install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements-api.txt
```

No environment activation or PowerShell execution-policy change is needed:
the examples use the virtual environment's Python executable directly.

### 3. Sign in and choose your target

```powershell
az login
az account list --query "[].{Name:name,Subscription:id,Tenant:tenantId}" --output table
```

Replace all three placeholders below with your own values. Keep these values
in your local shell; do not paste real values into files you plan to publish.
The scripts do not read `.env` files automatically.

```powershell
$target = @(
    "--subscription", "<your-subscription-id>",
    "--resource-group", "<your-resource-group>",
    "--agent", "<your-agent-resource-name>"
)
```

All three target arguments are **required**; there are no built-in personal or
demo-environment defaults. The subscription ID is the UUID, not its display name.

For a different tenant, sign in to that tenant and add it to the arguments:

```powershell
az login --tenant "<your-tenant-id>"
$target += @("--tenant", "<your-tenant-id>")
```

The clients use Azure CLI credentials. The direct API client requests credentials
for the specified subscription/tenant without changing your active CLI account.

### 4. Ask a question

Run in the same PowerShell session where you defined `$target`:

```powershell
.\.venv\Scripts\python.exe .\sre_chat_api.py "What do you know about my environment? Use read-only checks." @target
```

The client prints a thread ID and waits for a completed answer. Keep that ID to
continue the conversation. Questions run under the agent's configured capabilities
and approval policy; these clients do not enforce read-only behavior.

### 5. Continue a conversation

Replace the placeholder with the thread UUID printed by the previous command:

```powershell
.\.venv\Scripts\python.exe .\sre_chat_api.py "Which findings are still current? Use read-only checks." @target `
    --thread-id "<thread-id-from-previous-output>" `
    --timeout 600 --poll-interval 10
```

Reuse the same target. A follow-up always sends a new message; run only one
request at a time per thread.

## Alternative: MCP client

Complete the sign-in and target setup above. Install the MCP dependency in the
same virtual environment (the direct API dependencies are not required):

```powershell
node --version
npx --version
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
.\.venv\Scripts\python.exe .\sre_chat.py "What do you know about my environment? Use read-only checks." @target
```

This starts a **local** Azure MCP server; it does not use the MCP server
registered in VS Code or call the SRE REST API directly. The child process uses
Azure CLI credentials.

Azure MCP is pinned to **3.0.0-beta.49**, which contains the required SRE Agent
tools. `npx` downloads it on first use and then uses its cache, so the first run
needs access to npm and may take longer. The MCP Python dependency is
`mcp>=1.28,<2`.

Both clients accept the same arguments and can continue a thread created by
either client. Replace `sre_chat_api.py` with `sre_chat.py` in usage examples.

## Arguments and output

| Argument | Required / default | Purpose |
| --- | --- | --- |
| `question` | Required | Quoted question or follow-up message |
| `--subscription` | Required | Azure subscription ID |
| `--resource-group` | Required | Resource group containing the agent |
| `--agent` | Required | Agent resource name |
| `--tenant` | Optional | Azure tenant ID |
| `--thread-id` | Optional | Existing conversation UUID; omit for a new conversation |
| `--timeout` | `300` seconds | Request timeout; see details below |
| `--poll-interval` | `5` seconds | Time between thread reads |
| `--json` | Off | Emit one JSON result on stdout |

Use `--help` with either script for command-line help. Time values must be finite
and greater than zero.

Plain output contains the thread ID and answer. JSON output has this shape:

```json
{"thread_id": "...", "answer": "..."}
```

Progress, diagnostics, and the thread ID as soon as it is known go to **stderr**.
Only a successful final result goes to **stdout**.

For PowerShell automation:

```powershell
$result = .\.venv\Scripts\python.exe .\sre_chat_api.py "Summarize current known issues. Use read-only checks." @target --json
if ($LASTEXITCODE -ne 0) { throw "SRE request failed; see stderr." }
$reply = $result | ConvertFrom-Json
$reply.thread_id
$reply.answer

.\.venv\Scripts\python.exe .\sre_chat_api.py "Explain the highest-priority finding. Use read-only checks." @target `
    --thread-id $reply.thread_id
```

## Completion, timeouts, and errors

- Individual thread messages can be marked complete even when they are only
  progress updates. Each request therefore appends a unique completion-marker
  instruction and returns only a completed agent answer ending in that marker.
  The marker is removed from output; this is not a server-side completed-turn flag.
- If the agent omits the marker, requires approval, or takes too long, the client
  times out rather than treating a progress update as a final answer.
- The API client's timeout covers authentication, ARM lookup, sending, and
  polling. HTTP operations and Azure CLI token requests also have individual
  30-second timeouts.
- The MCP timeout starts after the stdio connection opens and covers
  initialization, discovery, sending, and polling. Initial npm download and
  process cleanup can add time.
- No send is automatically retried. A timeout or local cancellation **does not
  cancel work already started remotely**. Use any printed thread ID to inspect
  the conversation. If creation times out before returning an ID, inspect the
  agent's threads before sending again.
- These clients do not auto-approve actions or use investigation/yolo tools.
- Exit codes: `0` success, `1` request error/timeout, `2` invalid arguments,
  `130` Ctrl+C.

### Troubleshooting

| Symptom | What to check |
| --- | --- |
| `python`, `az`, or `npx` not found | Install the prerequisite, reopen your terminal, and check the version command. `npx` is only needed for MCP. |
| `ModuleNotFoundError` | Install the relevant requirements with `.\.venv\Scripts\python.exe -m pip`; run that same Python executable. |
| Missing required target arguments | Define `$target` and pass `@target` in the same PowerShell session. |
| Authentication error / HTTP 401 | Run `az login`, check the tenant and subscription, and confirm the account can access the agent. |
| HTTP 403 | Ask your administrator to verify permissions for both the agent resource and conversations. |
| HTTP 404 | Check the subscription ID, resource group, and agent resource name. |
| Timeout or approval needed | Inspect the thread in the agent UI before retrying; use a longer `--timeout` if appropriate. |
| Missing MCP tools / npm failure | Check npm connectivity and the pinned Azure MCP version. |

## Privacy and authentication

The direct client obtains tokens at runtime using `AzureCliCredential`. A
`Bearer` header is constructed from that token; no real token belongs in source
code. Tokens are cached in memory and refreshed before expiry. The clients do
not write credentials to disk themselves; Azure CLI manages its own login cache.

The direct client sends your **local operating-system username** as `userId`
and `displayName` in messages, matching the MCP implementation's convention.
Authorization comes from the Azure token, not those display fields. Questions,
answers, resource details, and thread IDs may be sensitive: review terminal
output, logs, and screenshots before sharing them.

The API client reads `properties.agentEndpoint` from ARM using
`2025-05-01-preview`, requests the `https://azuresre.dev/.default` token scope,
and creates/sends/reads threads under `/api/v1/threads`. It requires an HTTPS
endpoint under `*.azuresre.ai`, refuses redirects, and restricts pagination to
the same thread endpoint.

## Tests (no Azure access required)

Install both dependency sets to run both mock-based suites:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt -r .\requirements-api.txt
.\.venv\Scripts\python.exe -m unittest -v test_sre_chat test_sre_chat_api
```

For only the API client, run `-m unittest -v test_sre_chat_api` with its
dependencies installed. For only MCP, run `-m unittest -v test_sre_chat`.
Fixtures use synthetic targets, tokens, and thread UUIDs.

## References

- [SRE Agent MCP setup](https://learn.microsoft.com/en-us/azure/sre-agent/setup-mcp-server?tabs=vscode)
- [SRE Agent API reference](https://learn.microsoft.com/en-us/azure/sre-agent/api-reference)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
- [Microsoft's thread request models](https://github.com/microsoft/mcp/blob/main/tools/Azure.Mcp.Tools.SreAgent/src/Models/SreAgentThreadModels.cs)
- [Microsoft's request construction](https://github.com/microsoft/mcp/blob/main/tools/Azure.Mcp.Tools.SreAgent/src/Commands/SreAgentCommandHelpers.cs)
