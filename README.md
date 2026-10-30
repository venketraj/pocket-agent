# Pocket Agent

Pocket Agent is a Windows demo of an agent built around a small language model (SLM) running locally through Ollama. The local model can use tools to read an agenda, check the clock, calculate time, organize sample files, summarize meeting notes, and complete a local feedback form in Microsoft Edge.

A Rich terminal dashboard makes the agent's work visible: it shows tool calls, local model usage, token counts, generation speed, GPU usage, network status, and an estimated cloud cost comparison. When a request needs broader knowledge, the local SLM can hand it to a large language model (LLM), Claude, if internet access and an API key are available.

## Agent Flow

1. The user asks a question or gives the agent a task.
2. The local Ollama model decides whether to answer or call a tool.
3. Python runs the selected tool and sends its result back to the model.
4. For broad-knowledge tasks, the local model can call Claude and return its response.
5. The dashboard reports model calls, token usage, timing, and estimated cloud costs.

File operations are limited to `demo_files/workspace`. In interactive mode, the agent asks before moving or writing files. Its browser tool uses the bundled local feedback form by default; external websites are disabled unless `ALLOW_WEB=true` is configured.

This is a demo project, not a production agent or security boundary.

## Presentation

[Download the Pocket-Sized Brains presentation](Pocket-Sized-Brains.pptx).

## Requirements

- Windows 10 or 11
- Python 3.12 or newer
- [Ollama for Windows](https://ollama.com/download)
- A locally available Ollama model that supports tool calling
- Microsoft Edge for the local browser task

An Anthropic API key is optional and is only needed for a live Claude handoff. The local agent and its tools do not require a cloud API key.

## Setup

Open PowerShell in the project folder and create the Python environment:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip uv
uv sync --group dev
```

If PowerShell blocks activation, allow scripts for this terminal session and activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

Start Ollama if it is not already running, then download the default local model:

```powershell
ollama pull gemma4:latest
```

## Run the Agent

Start an interactive session:

```powershell
python pocket_agent.py
```

Type a request at the `You` prompt. Enter `/reset` to clear the conversation or `/quit` to exit. Add `--no-layout` to skip automatic window placement:

```powershell
python pocket_agent.py --no-layout
```

To play the scripted four-act agent demo:

```powershell
python pocket_agent.py --script demo_script.yaml --reset
```

The demo exercises local reasoning and tools, then reconnects to the cloud handoff in its final act. It temporarily disconnects Wi-Fi in the first act. The scripted flow automatically approves its file and form changes. `--reset` replaces `demo_files/workspace` with the original sample files, so back up anything in that folder that you want to keep.

## Configuration

`pocket_agent.py` loads an optional `.env` file from the project root. The defaults are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOCAL_MODEL` | `gemma4:latest` | Ollama model used for local reasoning |
| `LOCAL_CTX` | `8192` | Local model context size |
| `CLOUD_MODEL` | `claude-haiku-5-5` | Claude model used for the optional handoff |
| `DEMO_NOW` | empty | Freeze the clock at `HH:MM`, useful for repeatable demos |
| `ALLOW_WEB` | `false` | Allow the browser tool to open external websites |

To enable the Claude handoff, set `ANTHROPIC_API_KEY` in your local `.env`. Keep API keys private. You can select a different local model with `--model MODEL_NAME` or by setting `LOCAL_MODEL`.

## Test the Agent

Run the unit and integration tests:

```powershell
pytest
```

Run each scripted agent check once:

```powershell
python rehearse.py -n 1
```

Rehearsal resets `demo_files/workspace` before each check and runs the browser check headlessly. The hybrid check uses a local fallback unless `--cloud` is specified. A live cloud rehearsal can incur API charges:

```powershell
python rehearse.py -n 1 --acts hybrid --cloud
```

## Project Files

| Path | Description |
| --- | --- |
| `pocket_agent.py` | Agent loop, tools, dashboard, and interactive/scripted entry point |
| `demo_script.yaml` | Four-act scripted agent demo |
| `demo_files/agenda.txt` | Sample agenda read by the agent |
| `demo_files/workspace_template/` | Original sample files restored by workspace reset and rehearsal |
| `demo_files/workspace/` | Working copy the agent can organize and edit |
| `demo_files/site/feedback_form.html` | Local browser task target |
| `rehearse.py` | Repeatable checks for the agent demo acts |
| `tests/` | Tests for agent tools, browser flow, and cloud handoff |