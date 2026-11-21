# Choosing the answer-writing model (`open` stack)

**In short:** Set up the [chat model](GLOSSARY.md#chat-model) for Dewey's `open` [stack](GLOSSARY.md#stack) ([open-weight](GLOSSARY.md#open-weight-model) models, not OpenAI) with [Ollama](GLOSSARY.md#ollama), [vLLM](GLOSSARY.md#vllm) or [OpenRouter](GLOSSARY.md#openrouter). Only [`ask_archive`](GLOSSARY.md#ask_archive) and the [demo page](GLOSSARY.md#demo-page) use this model; [`search_archive`](GLOSSARY.md#search_archive) never calls it.

**Who this is for:** You chose [Fully open on your own computer](STACKS.md#fully-open-on-your-own-computer) or [Open-weight models through OpenRouter](STACKS.md#open-weight-models-through-openrouter), and you can run commands in a [terminal](GLOSSARY.md#terminal).

**You'll need:** Dewey installed, with its [virtual environment](GLOSSARY.md#virtual-environment) on ([how](INSTALL.md#turn-it-on-in-every-new-terminal-window)), and:

- **Ollama:** a computer with enough memory ([an example](STACKS.md#fully-open-on-your-own-computer)) and 5 to 14 GB of free disk for a model. On Windows, Ollama itself needs at least 4 GB more, by Ollama's docs.
- **vLLM:** a server with a [GPU](GLOSSARY.md#gpu) that can hold the model.
- **OpenRouter:** an account with credits.

> [!NOTE]
> Facts about Ollama, vLLM and OpenRouter come from their own docs and code, checked November 2026; *unverified* marks what we couldn't check. Since this fork began, nobody has run Dewey with a real Ollama, vLLM or OpenRouter server, only automated tests with stand-in servers.

**On this page:**

- [What Dewey needs from the model](#what-dewey-needs-from-the-model)
- [A. Self-hosted with Ollama](#a-self-hosted-with-ollama)
- [B. Self-hosted with vLLM](#b-self-hosted-with-vllm)
- [C. Hosted through OpenRouter](#c-hosted-through-openrouter)
- [Hermes model licenses](#hermes-model-licenses)
- [Test before you rely on it](#test-before-you-rely-on-it)

## What Dewey needs from the model

On `open`, one chat model does two jobs for every question:

- **[Search planning](GLOSSARY.md#search-planning):** it turns the question into a topic, dates and author names, as [JSON](GLOSSARY.md#json) in a fixed shape. Dewey asks for [structured outputs](GLOSSARY.md#structured-outputs) (`json_schema`), then plain JSON. If the reply still doesn't fit, Dewey searches the question as typed, without date or author filters, and says so in its log.
- **The cited answer:** it reads up to 10 article [excerpts](GLOSSARY.md#excerpt) of about 380 words each and writes an answer that cites them. Dewey turns each [citation](GLOSSARY.md#citation) into a numbered link. The model must also say when the archive doesn't answer; small models are worst at this ([test first](#test-before-you-rely-on-it)).

It needs a [context window](GLOSSARY.md#context-window) of at least 16,384 [tokens](GLOSSARY.md#token-billing); 32,768 is comfortable (our estimate: we counted the prompt, not the answers). The answer request alone is about 7,100 tokens. The model's reasoning takes room too, even inside `<think>…</think>`, where Dewey hides it from the journalist.

If an answer hits the model's length limit, Dewey adds "This answer hit the length limit and is incomplete." It can't tell when a server cut the prompt itself, a real risk on Ollama ([section A](#a-self-hosted-with-ollama)). Changing the chat model never needs [re-indexing](GLOSSARY.md#indexing).

**The three settings**, in your [`.env` file](GLOSSARY.md#env-file):

| Setting | What it is |
|---|---|
| `OPEN_LLM_BASE_URL` | The server's [OpenAI-compatible](GLOSSARY.md#openai-compatible-api) address, ending in `/v1`. Leave it out (or blank) for Ollama on the same computer. |
| `OPEN_LLM_MODEL` | The model's name on that server. Dewey won't start without it, even if you only use `search_archive`. |
| `OPEN_LLM_API_KEY` | Only if the server checks keys, as OpenRouter does. |

Dewey reaches these servers with OpenAI's client library; nothing is sent to OpenAI.

**Three ways to run the model:**

| | A. Ollama | B. vLLM | C. OpenRouter |
|---|---|---|---|
| Who receives questions and excerpts | Nobody outside your network | Nobody outside your network | OpenRouter and the provider it picks for each request ([what they keep](#what-the-privacy-settings-do-and-dont-do)) |
| Cost | Your hardware | Your hardware | Roughly $0.0002 to $0.001 a question with gpt-oss-20b (estimate) |

The [search models](GLOSSARY.md#search-models) and your AI tool receive data too ([what each provider receives](STACKS.md#what-the-hosted-providers-receive)).

## A. Self-hosted with Ollama

Ollama is a free app (MIT license) that runs open-weight models on your computer and, by default, answers only there ([localhost](GLOSSARY.md#localhost), [port](GLOSSARY.md#port) 11434). For Ollama in [Docker](GLOSSARY.md#docker) with Dewey's [Docker Compose](GLOSSARY.md#docker-compose) file, follow [Steps for one server](DEPLOYMENT.md#steps-for-one-server) instead.

> [!WARNING]
> **Ollama silently cuts Dewey's prompt unless you raise its context length.** Each answer request is about 7,100 tokens, and on most computers Ollama reads only 4,096 by default.
>
> It keeps the first few words and the end, roughly the three lowest-ranked excerpts. The model never sees Dewey's instructions, the question or the best excerpts, and writes a fluent answer that isn't about your question. Step 2 fixes this.

The `ollama` commands below work the same in [PowerShell](GLOSSARY.md#powershell) on Windows.

1. **Install Ollama.**

   **On a Mac** (macOS 14 Sonoma or newer): download Ollama from [ollama.com/download](https://ollama.com/download) (230 MB), drag it into Applications and open it. Allow it to create a link in `/usr/local/bin`: that's how the `ollama` command reaches your terminal ([Ollama: macOS](https://github.com/ollama/ollama/blob/main/docs/macos.mdx)).

   **On Windows** (10 22H2 or newer; no administrator rights needed): run the installer from the same page (about 1.9 GB), or in PowerShell:

   ```powershell
   winget install --id Ollama.Ollama -e --source winget
   ```

   An NVIDIA card needs driver 551.61 or newer ([Ollama: Windows](https://github.com/ollama/ollama/blob/main/docs/windows.mdx)). Afterwards, open a new PowerShell window, so it finds the `ollama` command.

   **On Linux:** Ollama's installer needs `curl` and `zstd`. On Ubuntu or Debian:

   ```bash
   sudo apt install curl zstd
   ```

   On Fedora (type `y` when dnf asks `Is this ok [y/N]:`; Enter alone cancels):

   ```bash
   sudo dnf install curl zstd
   ```

   Then run Ollama's install script (about 1.6 GB). It asks for your password itself, so don't type `sudo` in front:

   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```

   You should see something like:

   ```text
   >>> The Ollama API is now available at 127.0.0.1:11434.
   >>> Install complete. Run "ollama" from the command line.
   ```

   With an NVIDIA card but no driver, it may install one and ask you to restart ([Ollama: Linux](https://github.com/ollama/ollama/blob/main/docs/linux.mdx)).

   **Then, on any system, check that Ollama runs:**

   ```bash
   ollama -v
   ```

   You should see something like:

   ```text
   ollama version is 0.40.16
   ```

   If it says `Warning: could not connect to a running Ollama instance`, open the app, or on Linux run `sudo systemctl restart ollama`.

2. **Set the context length to 32,768 tokens.** It applies to every model, and a bigger context uses more memory. With 16 GB of memory, use 16,384, the smallest size that leaves room for an answer (our estimate), and no lower.

   **On a Mac or Windows:** open Ollama's **Settings** and move **Context length** to **32k** (or **16k**). Ollama saves it and restarts. Settings is in the menu of Ollama's icon by the clock on Windows, and in its menu-bar icon or window on a Mac. *Unverified:* we couldn't check these menus in the running app; the slider is in [Ollama's app code](https://github.com/ollama/ollama/blob/v0.40.16/app/ui/app/src/components/Settings.tsx#L743-L775).

   **On Linux:** the setting goes in an extra file for Ollama's service. With 16 GB of memory, change `32768` to `16384` in the second command. Run these one at a time:

   ```bash
   sudo mkdir -p /etc/systemd/system/ollama.service.d
   ```

   ```bash
   printf '[Service]\nEnvironment="OLLAMA_CONTEXT_LENGTH=32768"\n' | sudo tee /etc/systemd/system/ollama.service.d/dewey.conf
   ```

   You should see something like:

   ```text
   [Service]
   Environment="OLLAMA_CONTEXT_LENGTH=32768"
   ```

   Then load the setting and restart Ollama:

   ```bash
   sudo systemctl daemon-reload
   ```

   ```bash
   sudo systemctl restart ollama
   ```

   This file should survive Ollama updates (our reading of its install script). Other ways to set the context are under **Other ways to set the context length**, below.

3. **Download a model.** The [setup wizard](GLOSSARY.md#setup-wizard) suggests two, both under the Apache 2.0 license:

   | Model | Download | The wizard's words |
   |---|---|---|
   | `qwen3:8b` | 5.2 GB | "Smaller; a reasonable start on a laptop." |
   | `gpt-oss:20b` | 13.8 GB | "Stronger, needs much more memory." |

   By our estimate, "a laptop" means 24 GB of memory or more ([memory budget](STACKS.md#running-a-chat-model-yourself)). Sizes are from Ollama's library pages for [qwen3](https://ollama.com/library/qwen3/tags) and [gpt-oss](https://ollama.com/library/gpt-oss/tags), checked November 2026. Download one:

   ```bash
   ollama pull qwen3:8b
   ```

   Or:

   ```bash
   ollama pull gpt-oss:20b
   ```

   **Other models** in [Ollama's library](https://ollama.com/library) may work, but nobody has tried any model with Dewey yet. Copy the exact name and tag from its page, check its license ([Hermes model licenses](#hermes-model-licenses)), then run this with NAME:TAG replaced:

   ```bash
   ollama pull NAME:TAG
   ```

4. **Point Dewey at it.** With the virtual environment on, run the setup wizard:

   ```bash
   python install.py
   ```

   Choose `open`, press Enter to keep the server address `http://localhost:11434/v1`, and type the model name. If it offers to download a model you already have, press Enter to skip. Near the end, press Enter at `Check your credentials and services now? (Y/n):` ([the other questions](INSTALL.md#what-the-wizard-asks)).

   You should see something like:

   ```text
     Chat model server: ok, 'qwen3:8b' is available
   ```

   You can also change `.env` by hand ([how to open it](INSTALL.md#change-your-settings)). Put these lines in `.env` (a file in the Dewey folder) with a text editor, not in Terminal, changing any that are already there ([how](STACKS.md#configuring-by-hand)):

   ```dotenv
   DEWEY_STACK='open'
   OPEN_LLM_MODEL='qwen3:8b'
   # OPEN_LLM_BASE_URL isn't needed for Ollama on this computer.
   ```

5. **Check that Ollama reads the whole prompt.** Ask Dewey a question on the [demo page](INSTALL.md#try-the-demo-page) or through `ask_archive`. Within five minutes, while the model is still loaded, run:

   ```bash
   ollama ps
   ```

   The CONTEXT column should say `32768` (or `16384`). With a GPU, PROCESSOR should say `100% GPU`. A split such as `48%/52% CPU/GPU` means the model and context don't fit in the GPU's memory: use a smaller context or model ([Ollama FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.mdx)).

   Then check Ollama's log for cut prompts. No output means none since Ollama last started; if lines appear, go back to step 2. On a Mac:

   ```bash
   grep 'truncating input prompt' ~/.ollama/logs/server.log
   ```

   On Windows, in PowerShell:

   ```powershell
   Select-String -Path "$env:LOCALAPPDATA\Ollama\server.log" -Pattern 'truncating input prompt'
   ```

   On Linux, the log also keeps lines from before Ollama last started. Each line begins with its date and time: only lines from after your restart in step 2 count.

   ```bash
   sudo journalctl -u ollama --no-pager | grep 'truncating input prompt'
   ```

**If something goes wrong:**

| You see | What to do |
|---|---|
| `FAILED to reach http://localhost:11434/v1: … Is Ollama running?` in the wizard's check | Start Ollama (the app, or on Linux `sudo systemctl restart ollama`), then run the wizard again. |
| `reachable, but 'qwen3:8b' isn't listed` | Run `ollama pull` for it, or fix the spelling of `OPEN_LLM_MODEL` in `.env`: it must match a name after `found:` exactly. |
| Answers ignore the question or the excerpts | Ollama is probably still cutting the prompt: redo step 2, then check with step 5. |
| Dewey runs in [WSL](GLOSSARY.md#wsl) and can't reach Ollama for Windows | Ollama for Windows answers only on Windows. Turn on WSL's mirrored networking (`networkingMode=mirrored`, Windows 11 22H2 or later), or install Ollama inside WSL. *Not tested by us.* |

<details><summary>Other ways to set the context length</summary>

Write the number in digits: Ollama ignores `32k` in these settings.

**If you start Ollama yourself** with `ollama serve`, instead of the app or the Linux service:

```bash
OLLAMA_CONTEXT_LENGTH=32768 ollama serve
```

**On a Mac, without the app's slider:** run this, then quit and reopen Ollama. Once you have moved the slider, its value wins.

```bash
launchctl setenv OLLAMA_CONTEXT_LENGTH 32768
```

**On Windows, as an [environment variable](GLOSSARY.md#environment-variable):** run this in PowerShell, choose **Quit Ollama** from its icon by the clock, and start Ollama again from the Start menu. A slider you have moved wins here too.

```powershell
[Environment]::SetEnvironmentVariable('OLLAMA_CONTEXT_LENGTH', '32768', 'User')
```

**For Dewey's model only, with a Modelfile:** this gives one model its own context length, which also wins over the slider. Download the model first (step 3). On a Mac or Linux:

```bash
printf 'FROM qwen3:8b\nPARAMETER num_ctx 32768\n' > ~/dewey-qwen3.Modelfile
```

```bash
ollama create dewey-qwen3 -f ~/dewey-qwen3.Modelfile
```

On Windows, write the file with `Set-Content`, not `>`, which would save it as UTF-16, a different text encoding:

```powershell
Set-Content -Path "$HOME\dewey-qwen3.Modelfile" -Value 'FROM qwen3:8b', 'PARAMETER num_ctx 32768'
```

```powershell
ollama create dewey-qwen3 -f "$HOME\dewey-qwen3.Modelfile"
```

Then use `dewey-qwen3:latest` as the model name, as Ollama lists it. With plain `dewey-qwen3`, the wizard's check says the model isn't listed and suggests an `ollama pull` that can't work (from Ollama's code; not run). Type the name at the wizard's model question and answer N to the download offer, or write `OPEN_LLM_MODEL='dewey-qwen3:latest'` in `.env`.

Sources (Ollama's code at v0.40.16, checked November 2026): [which setting wins](https://github.com/ollama/ollama/blob/v0.40.16/server/routes.go#L139-L161), [how the app passes its slider value on](https://github.com/ollama/ollama/blob/v0.40.16/app/server/server.go#L263-L265), [the digits-only rule](https://github.com/ollama/ollama/blob/v0.40.16/envconfig/config.go#L261-L273), [`ollama create`](https://github.com/ollama/ollama/blob/v0.40.16/cmd/cmd.go#L2578-L2584) and [how Ollama lists model names](https://github.com/ollama/ollama/blob/v0.40.16/types/model/name.go#L231-L249).

</details>

<details><summary>Sources</summary>

Checked November 2026, with Ollama's code at v0.40.16.

- **Ollama's OpenAI-compatible API** is at `http://localhost:11434/v1/`. It ignores the API key that OpenAI clients must send, so Dewey sends a placeholder when `OPEN_LLM_API_KEY` is empty. "The OpenAI API does not have a way of setting the context size for a model," and Dewey's requests carry no such field ([Ollama: OpenAI compatibility](https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx); [openai.go](https://github.com/ollama/ollama/blob/v0.40.16/openai/openai.go#L110-L131)).
- **Structured outputs** work through that API via `response_format`, which Dewey uses ([Ollama: structured outputs](https://github.com/ollama/ollama/blob/main/docs/capabilities/structured-outputs.mdx)).
- **The default context** depends on GPU memory: 4,096 tokens below about 24 GiB, 32,768 up to about 48 GiB, 262,144 above, and 4,096 without a GPU (1 GiB is about 1.07 GB). The code's thresholds are 23 and 47 GiB ([Ollama: context length](https://github.com/ollama/ollama/blob/main/docs/context-length.mdx); [routes.go](https://github.com/ollama/ollama/blob/v0.40.16/server/routes.go#L2570-L2586)). The [FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.mdx) says 4,096 and the [Modelfile page](https://github.com/ollama/ollama/blob/main/docs/modelfile.mdx) 2,048; only the context-length page matches the code.
- **Macs:** Ollama counts only the memory macOS lets the GPU use: 28.08 GiB on one 36 GB Mac (an Apple M3 Max, measured once in November 2026). Apple doesn't document that share, so on a 32 GB Mac the default could be 4,096 or 32,768; assume 4,096.
- **Neither suggested model sets its own context length.** Ollama never goes above a model's maximum: 40,960 tokens for qwen3:8b and 128K for gpt-oss:20b ([sched.go](https://github.com/ollama/ollama/blob/v0.40.16/server/sched.go#L192-L210)).
- **How Ollama cuts a long prompt:** it drops earlier messages, then keeps the first 4 tokens and the end. It logs `truncating input prompt` as a warning, and Dewey gets a normal answer ([llama_server.go](https://github.com/ollama/ollama/blob/v0.40.16/llm/llama_server.go#L309-L358)). When the window fills during the answer, it drops older text, instructions included, so 8,192 is too tight for a model that reasons first, such as qwen3:8b (from Ollama's code; not run).
- **The 7,100 tokens**, counted with Qwen3's tokenizer on ten full-length excerpts from real articles: instructions 674, question 15, question plus excerpts 6,442. With gpt-oss's tokenizer, about 6,700; a typical question is about 6,500.
- **Memory**, for STACKS.md's budget: the models' settings on Hugging Face ([Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B/blob/main/config.json), [gpt-oss-20b](https://huggingface.co/openai/gpt-oss-20b/blob/main/config.json)). OpenAI says gpt-oss-20b can "run within 16GB of memory" ([gpt-oss README](https://github.com/openai/gpt-oss/blob/main/README.md)), but on a 16 GB Mac the GPU's share is less, so part of the model would run on the processor (our inference).

</details>

## B. Self-hosted with vLLM

vLLM is a model server for a GPU machine. It runs one [Hugging Face](GLOSSARY.md#hugging-face) model at a time. Install it with [vLLM's quickstart](https://github.com/vllm-project/vllm/blob/main/docs/getting_started/quickstart.md).

1. **Start vLLM with the model's Hugging Face id,** shown on the model's page as ORG/MODEL:

   ```bash
   vllm serve ORG/MODEL
   ```

   It listens on port 8000. To require a key, add `--api-key` or set `VLLM_API_KEY`, then put the same key in `OPEN_LLM_API_KEY` ([vLLM quickstart](https://github.com/vllm-project/vllm/blob/main/docs/getting_started/quickstart.md)).

2. **Find the model id that Dewey must use.** On the same machine, run:

   ```bash
   curl http://localhost:8000/v1/models
   ```

   The `id` in its reply goes in `OPEN_LLM_MODEL`.

3. **Point Dewey at it.** Run the setup wizard, choose `open`, and type the server's address, the model id and any key. Or put these lines in `.env` (a file in the Dewey folder) with a text editor, not in Terminal, changing any that are already there ([how](STACKS.md#configuring-by-hand)):

   ```dotenv
   DEWEY_STACK='open'
   OPEN_LLM_BASE_URL='http://localhost:8000/v1'
   OPEN_LLM_MODEL='ORG/MODEL'
   # Only if you started vLLM with a key:
   # OPEN_LLM_API_KEY='YOUR_VLLM_KEY'
   ```

   On another machine, use its address, such as `http://GPU_SERVER:8000/v1`. Plain `http://` sends the key and every question unencrypted, so use `https://` ([TLS](GLOSSARY.md#tls-and-https)) or a private network. The wizard warns about this, and its check won't send a key that way.

vLLM supports structured outputs, on by default ([vLLM: structured outputs](https://raw.githubusercontent.com/vllm-project/vllm/main/docs/features/structured_outputs.md)). How much GPU memory a model needs, and whether your vLLM version supports its architecture, is *unverified*: check vLLM's docs. The model's Hugging Face page names the architecture, such as `seed_oss` for Hermes 4.3 36B.

## C. Hosted through OpenRouter

OpenRouter is one API in front of many providers that run open-weight models, including large ones, so you need no GPU.

> [!WARNING]
> With OpenRouter, your journalists' questions and the article excerpts leave your network. Questions can reveal what a reporter is working on. OpenRouter and the provider that serves each request receive the full text ([what the privacy settings do and don't do](#what-the-privacy-settings-do-and-dont-do)).

### Setup

1. **Create an account and an API key.** Make the key on OpenRouter's [API keys page](https://openrouter.ai/keys); keys start with `sk-or-v1-` ([OpenRouter: authentication](https://github.com/OpenRouterTeam/docs/blob/main/api_reference/authentication.mdx)). Then buy credits. OpenRouter passes on each provider's price and adds a 5.5% fee ($0.80 minimum) when you buy credits. Unused credits may expire after a year ([OpenRouter FAQ](https://github.com/OpenRouterTeam/docs/blob/main/faq.mdx), checked November 2026).

2. **Pick a model that supports structured outputs.** Start from the [models page filtered for structured outputs](https://openrouter.ai/models?supported_parameters=structured_outputs) and copy the id from the model's page. Check its **Providers** section first, because support is per provider ([OpenRouter: structured outputs](https://github.com/OpenRouterTeam/docs/blob/main/guides/features/structured-outputs.mdx)). Some models have none at all, such as `qwen/qwen3-8b` in November 2026.

   For example, `openai/gpt-oss-20b` is the open-weight model Ollama calls `gpt-oss:20b`: `openai/` names its maker. Dewey's requests go to OpenRouter, which picks a provider that runs the model.

3. **Put the settings in `.env`.** Run the setup wizard ([what it asks](INSTALL.md#what-the-wizard-asks)) and choose `open`. Type `https://openrouter.ai/api/v1` as the server address ([OpenRouter quickstart](https://github.com/OpenRouterTeam/docs/blob/main/quickstart.mdx)), then the model id. Paste the key at "API key for that server, if it needs one". Or put these lines in `.env` (a file in the Dewey folder) with a text editor, not in Terminal, changing any that are already there ([how](STACKS.md#configuring-by-hand)):

   ```dotenv
   DEWEY_STACK='open'
   OPEN_LLM_BASE_URL='https://openrouter.ai/api/v1'
   OPEN_LLM_API_KEY='YOUR_OPENROUTER_KEY'
   OPEN_LLM_MODEL='openai/gpt-oss-20b'
   # Optional. strict is the default: see "What Dewey sends" below.
   # OPENROUTER_PRIVACY='strict'
   ```

   The wizard's check only looks for the id in OpenRouter's model list. It doesn't check which providers serve it, or your privacy setting.

**What Dewey checks.** Dewey recognizes OpenRouter only by its address (`openrouter.ai` or a subdomain). It won't start without `OPEN_LLM_API_KEY`, or with an `http://` OpenRouter address, which would send the key and every question unencrypted. Through your own proxy or gateway, it sends **no** privacy preferences, and warns at startup if `OPENROUTER_PRIVACY` is set. Set privacy in your OpenRouter account instead.

**What it costs.** Each question sends about 7,000 tokens and gets back 500 to 2,000, counting the model's reasoning. That's roughly $0.0002 to $0.001 with `openai/gpt-oss-20b`, and about $0.01 with `nousresearch/hermes-4-405b` (estimates, November 2026; [prices on each model's page](https://github.com/OpenRouterTeam/docs/blob/main/guides/overview/models.mdx)). Dewey has no budget or rate limit, and an AI tool can call `ask_archive` many times in one task ([cost and privacy](MCP.md#cost-and-privacy)). Watch your usage in your OpenRouter account.

<details><summary>Example models and prices (November 2026)</summary>

US dollars per million tokens, input / output, from OpenRouter's public lists of [models](https://openrouter.ai/api/v1/models) and of [endpoints](https://openrouter.ai/api/v1/endpoints/zdr) that offer [Zero Data Retention](GLOSSARY.md#zero-data-retention) (ZDR). They change often.

| Model id | Endpoints | On the ZDR list (what `strict` allows) | Cheapest of those with structured outputs | Longest context (tokens) |
|---|---|---|---|---|
| `openai/gpt-oss-20b` | 12 | 11 | $0.018 / $0.09 | 131,072 |
| `openai/gpt-oss-120b` | 23 | 23 | $0.03 / $0.15 | 131,072 |
| `qwen/qwen3-32b` | 5 | 5 | $0.08 / $0.28 | 40,960 |
| `qwen/qwen3-30b-a3b-instruct-2507` | 3 | 3 | $0.09 / $0.30 | 262,144 |
| `nousresearch/hermes-4-405b` | 1 | 1 | $1.00 / $3.00 | 131,072 |

- Each question makes two requests: search planning, then the answer with the excerpts. The per-question costs assume about 7,000 tokens in, from Dewey's prompts in `app/tools/retrieve.py` and `app/tools/answer.py`, and 500 to 2,000 out.
- `strict` ruled out one `openai/gpt-oss-20b` endpoint, the cheapest ($0.017 / $0.085), whose provider keeps prompts but doesn't train on them. `standard` ruled out none: by OpenRouter's records, none of these providers trains on your data. For search planning, `require_parameters` also ruled out that model's two Amazon Bedrock endpoints; the answer can still use them.
- `nousresearch/hermes-4-70b` and `nousresearch/hermes-3-llama-3.1-70b` had model pages but no endpoints, and `nousresearch/hermes-4.3-36b` wasn't listed.
- The ZDR list includes Safety Retention endpoints ([what that means](#what-the-privacy-settings-do-and-dont-do)), such as Google Vertex for `openai/gpt-oss-120b`.
- Some Qwen ids with `plus` or `max` in the name are Alibaba's hosted models, not open-weight ones (our reading of the names; not checked against Hugging Face).

</details>

### What Dewey sends

Every OpenRouter request carries `provider` preferences in its body, and `OPENROUTER_PRIVACY` picks the privacy part ([OpenRouter: provider selection](https://github.com/OpenRouterTeam/docs/blob/main/guides/routing/provider-selection.mdx)):

| `OPENROUTER_PRIVACY` | Sent with every request | What it means |
|---|---|---|
| `strict` (default) | `"data_collection": "deny", "zdr": true` | Providers that don't collect your data, on [Zero Data Retention](GLOSSARY.md#zero-data-retention) endpoints only (caveat below). |
| `standard` | `"data_collection": "deny"` | Only providers that don't collect your data, which for OpenRouter means they don't train on it. Endpoints that keep prompts can still be used, such as Safety Retention ones, which keep them to watch for abuse ([OpenRouter: ZDR](https://github.com/OpenRouterTeam/docs/blob/main/guides/features/zdr.mdx)), and the cheapest `openai/gpt-oss-20b` endpoint in November 2026. |
| `off` | nothing | OpenRouter's defaults and your account settings decide. Its request default, `data_collection: "allow"`, allows providers that store data and may train on it. |

Search planning also sends `"require_parameters": true`, at every level, even `off`, so it only reaches endpoints that support all its parameters, `json_schema` included. Each request is routed on its own, so planning and the answer may be served by different providers (*unverified*: our reading of OpenRouter's routing docs).

Your [account privacy settings](https://openrouter.ai/settings/privacy) apply too. A request's `zdr` can switch ZDR on, but can't switch off ZDR that your account or a guardrail enforces.

### What the privacy settings do and don't do

From OpenRouter's documentation:

- **OpenRouter itself** keeps prompts and responses only if you opt in (prompt logging, or a discount for your data). The exception is content flagged as potential child sexual abuse material, which it screens requests for in flight, ZDR traffic included.
- **It keeps metadata** for every request, such as token counts and latency, whatever these settings say.
- **`data_collection: "deny"`** rests on OpenRouter's records of provider policies, which it calls "not a definitive source of third party data policies, but represents our best knowledge."
- **`zdr: true` still allows Safety Retention:** OpenRouter or the provider may keep any request's prompts and responses, but only to detect abuse, not for training. Self-serve accounts allow it by default. For strict zero retention, turn on **ZDR only** in your [privacy settings](https://openrouter.ai/settings/privacy); some models may become unavailable.
- **These are policies, not technical controls.** OpenRouter and the provider see the full text of each request; the settings govern what they keep afterwards.

If your newsroom has rules about sending reporters' questions to outside services, check them before you use this option. Choose `standard` or `off` deliberately, not to fix an error.

<details><summary>Sources</summary>

OpenRouter's documentation repository, checked November 2026:

- [Data collection](https://github.com/OpenRouterTeam/docs/blob/main/guides/privacy/data-collection.mdx): what OpenRouter stores, the abuse screening and the metadata.
- [Provider selection](https://github.com/OpenRouterTeam/docs/blob/main/guides/routing/provider-selection.mdx): the three settings and the "best knowledge" quote.
- [Zero Data Retention](https://github.com/OpenRouterTeam/docs/blob/main/guides/features/zdr.mdx): Safety Retention and the ZDR-only setting. On the default: "If you haven't chosen, self-serve accounts allow Safety Retention and enterprise accounts use ZDR only."
- [Provider logging](https://github.com/OpenRouterTeam/docs/blob/main/guides/privacy/provider-logging.mdx): which providers train on or keep data.

</details>

### When OpenRouter can't route a request

If no endpoint for the model meets your privacy setting (or, for search planning, supports structured outputs), OpenRouter rejects the request. Dewey stops with an error that names the model, quotes OpenRouter and lists what to change. It doesn't fall back to plain JSON, which would hide the problem.

| What you see | What to do |
|---|---|
| `OpenRouter could not route Dewey's search-planning request …` | Pick a model with endpoints that offer structured outputs (and ZDR, on `strict`), or fix a misspelled `OPEN_LLM_MODEL`, which gives a similar error. Relax `OPENROUTER_PRIVACY` only if your policy allows it. |
| `… could not route Dewey's answer request …` | Same, minus structured outputs. |
| HTTP 503 | OpenRouter's docs: "no available model provider that meets your routing requirements." If the model worked before, try again later. |
| HTTP 401 | The key is missing, wrong or revoked. |
| HTTP 402 | The account or key is out of credits. |

Dewey treats HTTP 404 and 503 as routing failures, and a 400 that says no endpoint or provider was available (our guess, *unverified*). A 503 for an overloaded provider shows as a plain error, without Dewey's advice. An AI tool calling `ask_archive` gets the full message, as does the log of `app/mcp_server.py`. The demo page doesn't show it to the journalist: it's in the terminal running `app/main.py`.

<details><summary>Sources</summary>

OpenRouter's docs show both 404 and 503 for routing failures, without fully saying which case gets which: 404 in an [example in its router docs](https://github.com/OpenRouterTeam/docs/blob/main/guides/routing/routers/router-metadata.mdx), 503 in its [error codes](https://github.com/OpenRouterTeam/docs/blob/main/api_reference/errors-and-debugging.mdx), which also give 401 and 402. Dewey's own rules are in `app/providers/open_llm.py`.

</details>

## Hermes model licenses

Nous Research's Hermes models differ in license. That matters if the stack must stay [open source](GLOSSARY.md#open-source), meaning under a license approved by the Open Source Initiative (OSI).

| Model | Built on | License | Open source (OSI)? |
|---|---|---|---|
| [Hermes 4.3 36B](https://hf.co/NousResearch/Hermes-4.3-36B) | ByteDance Seed-OSS-36B | Apache-2.0 | Yes |
| [Hermes 4 14B](https://hf.co/NousResearch/Hermes-4-14B) | Qwen3-14B | Apache-2.0 | Yes |
| [Hermes 4 70B](https://hf.co/NousResearch/Hermes-4-70B), [405B](https://hf.co/NousResearch/Hermes-4-405B) | Llama 3.1 | Meta's Llama 3 license (`llama3` on the model page) | No: free to download, but with Meta's use restrictions. Read it before you deploy. |

The license is the same however you run the model. Builds in Ollama's library or on OpenRouter should say which of these they are; check. In November 2026, only Hermes 4 405B had a working OpenRouter endpoint ($1 / $3 per million input / output tokens). Hermes 4 70B had no providers, and Hermes 4.3 36B wasn't listed.

## Test before you rely on it

Small local models cite less reliably than large hosted ones. They miss citations, cite the wrong article, or answer when they should say the archive doesn't cover the question. As of November 2026, nobody has checked how well the suggested models answer with Dewey. Before you give a model to the newsroom:

1. **Write 10 to 20 test questions** whose answers you know. Include a few the archive **can't** answer, and a few with a date range or byline, such as "What has Jane Doe written about school funding since 2020?"
2. **Ask each one** on the [demo page](INSTALL.md#try-the-demo-page). In the **Generating metadata** step above the answer, check the search text, dates and authors Dewey used. Bylines must be whole names ([how byline filters match](MCP.md#the-two-tools)).
3. **Click every citation.** Does the linked article support the sentence?
4. **Check the ones the archive can't answer.** Each should get a clear "the archive doesn't answer this" (the wording varies), not a guess.
5. **On Ollama, check that no prompt was cut,** with the log check in step 5 of [section A](#a-self-hosted-with-ollama).
6. **Repeat when something changes:** the model, the provider, Ollama's context length, or `OPENROUTER_PRIVACY` (which can change the provider).
