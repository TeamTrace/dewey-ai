# Choosing the answer-writing model (`open` stack)

On `DEWEY_STACK="open"`, one chat model does two jobs for every question:

1. **Search planning.** It turns the question into a search: the topic, a date range and
   author names, returned as JSON that has to match a schema.
2. **The cited answer.** It reads up to 10 article excerpts (about 380 words each) and writes
   an answer with `[1]`, `[2]` … links to the articles.

Embeddings and reranking are separate and are not affected by anything on this page. You can
change the chat model at any time without re-indexing.

The chat model is set with three settings in `.env`, whichever way you run it:

| Setting | What it is |
|---|---|
| `OPEN_LLM_BASE_URL` | The server's OpenAI-compatible address, ending in `/v1`. |
| `OPEN_LLM_MODEL` | The model name exactly as that server knows it. |
| `OPEN_LLM_API_KEY` | Only if the server checks keys. Required for OpenRouter. |

There are three ways to run the model:

| | A. Ollama | B. vLLM | C. OpenRouter |
|---|---|---|---|
| Where the model runs | Your computer or server | Your GPU server | A provider that OpenRouter picks for each request |
| Who receives questions and article excerpts | Nobody outside your network | Nobody outside your network | OpenRouter and that provider ([what they keep](#what-the-privacy-settings-do-and-dont-do)) |
| Cost | Your hardware | Your hardware (GPU) | Pay per use (prices are listed per token, request or unit); no hardware |
| Effort | Lowest | Highest | Low: an account and an API key |

Facts about Ollama, vLLM and OpenRouter on this page were checked against their own
documentation (linked). Anything marked *unverified* we could not check: confirm it in the
provider's docs.

## What Dewey needs from the model

- **JSON that matches a schema** for search planning. Dewey asks for `json_schema` structured
  output first. If the server rejects that, it retries in plain JSON mode with the schema in the
  prompt. Either way it validates the reply, and if the reply still doesn't fit, it searches the
  question as typed, with no date or author filters (the log says when that happens).
- **Following the citation rules**: citing sources by number, and saying when the archive
  doesn't answer the question. Small models are worst at this (see
  [Test before you rely on it](#test-before-you-rely-on-it)).
- **Enough context window** for the prompt plus about 3,800 words of excerpts. If answers seem to
  ignore the sources, check that the server isn't cutting the prompt short (see the Ollama note
  below).
- Models that write their reasoning inside `<think>…</think>` have that text removed before it
  reaches the journalist.

## A. Self-hosted with Ollama

Ollama is Dewey's default: `OPEN_LLM_BASE_URL` defaults to `http://localhost:11434/v1`.

- Ollama serves an OpenAI-compatible API at `http://localhost:11434/v1/`. OpenAI clients have to
  send an API key, but Ollama ignores it; Dewey sends a placeholder when `OPEN_LLM_API_KEY` is
  empty. ([Ollama: OpenAI compatibility](https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx))
- Ollama's own API accepts a JSON schema in its `format` field, and its docs say structured
  outputs also work through the OpenAI-compatible API via `response_format`, which is what Dewey
  uses. ([Ollama: structured outputs](https://github.com/ollama/ollama/blob/main/docs/capabilities/structured-outputs.mdx))
- Context size: the OpenAI-compatible API can't set it per request. To raise it, create a model
  from a `Modelfile` with `PARAMETER num_ctx <size>` and use that model's name in
  `OPEN_LLM_MODEL`. (Same OpenAI-compatibility page. Ollama's default size is *unverified* here.)

**Hermes models in Ollama.** Search [ollama.com/library](https://ollama.com/library) for
"hermes" and copy the exact name and tag from the model's page. Dewey doesn't hard-code one,
because names and tags change. Before you use one, check on its page which upstream model it was
built from, and its license (see [Hermes licenses](#hermes-model-licenses)).

```bash
ollama pull <name:tag from ollama.com/library>
```

```bash
# .env
DEWEY_STACK="open"
OPEN_LLM_MODEL="<name:tag from ollama.com/library>"
# OPEN_LLM_BASE_URL is not needed for a local Ollama.
```

## B. Self-hosted with vLLM

vLLM is for a server with a suitable GPU. It runs Hugging Face models directly and serves one
model at a time.

- Start it with the model's Hugging Face id, e.g. `vllm serve <org>/<model>`. By default it
  listens on `http://localhost:8000`. `curl http://localhost:8000/v1/models` shows the model id
  to put in `OPEN_LLM_MODEL`. ([vLLM quickstart](https://github.com/vllm-project/vllm/blob/main/docs/getting_started/quickstart.md))
- To require a key, start vLLM with `--api-key` (or set `VLLM_API_KEY`) and put the same key in
  `OPEN_LLM_API_KEY`. (Same quickstart.)
- vLLM's OpenAI-compatible server supports `response_format` with `type: json_schema`, and
  structured outputs are on by default. ([vLLM: structured outputs](https://raw.githubusercontent.com/vllm-project/vllm/main/docs/features/structured_outputs.md))
- GPU memory needed for a given model, and whether your vLLM version supports a given model
  architecture: *unverified*, check vLLM's docs. The Hugging Face page lists each model's
  architecture (for example `qwen3` for Hermes 4 14B and `seed_oss` for Hermes 4.3 36B).

```bash
# .env
DEWEY_STACK="open"
OPEN_LLM_BASE_URL="http://localhost:8000/v1"   # or http://<gpu-server>:8000/v1
OPEN_LLM_MODEL="<org>/<model>"                 # the id from /v1/models
# OPEN_LLM_API_KEY="..."                       # if you started vLLM with --api-key
```

## C. Hosted through OpenRouter

OpenRouter is one API in front of many model providers. It's the quickest way to try larger
open-weight models without a GPU. The trade-off: your journalists' questions and the article
excerpts leave your network. Questions can reveal what a reporter is working on.

### Setup

1. Create an account and an API key on the [API keys page](https://openrouter.ai/keys). Keys
   start with `sk-or-v1-`. ([OpenRouter: authentication](https://github.com/OpenRouterTeam/docs/blob/main/api_reference/authentication.mdx))
2. Pick a model that supports structured outputs. Start from the
   [models page filtered for structured outputs](https://openrouter.ai/models?supported_parameters=structured_outputs),
   search for "hermes" (or any other model), and copy the model id from the model's page.
   Support is decided per endpoint, not per model: the same model can be served by several
   providers and only some may support structured outputs. The Providers section of a model's
   page shows which do. ([OpenRouter: structured outputs](https://github.com/OpenRouterTeam/docs/blob/main/guides/features/structured-outputs.mdx))
3. Set `.env`. The base URL for OpenAI-compatible clients is `https://openrouter.ai/api/v1`;
   Dewey sends the key as `Authorization: Bearer <key>`.
   ([OpenRouter quickstart](https://github.com/OpenRouterTeam/docs/blob/main/quickstart.mdx))

```bash
# .env
DEWEY_STACK="open"
OPEN_LLM_BASE_URL="https://openrouter.ai/api/v1"
OPEN_LLM_API_KEY="your-openrouter-api-key"
OPEN_LLM_MODEL="<model id copied from openrouter.ai/models>"
# OPENROUTER_PRIVACY="strict"    # the default; see below
```

Dewey detects OpenRouter from the base URL (`openrouter.ai` or a subdomain of it). It refuses to
start without `OPEN_LLM_API_KEY`, or with an `http://` OpenRouter URL (the key and every question
would travel unencrypted). If you reach OpenRouter through your own proxy or gateway, Dewey can't
tell, so it sends **no** privacy preferences; it prints a warning at startup if `OPENROUTER_PRIVACY`
is set. In that case, set privacy in your OpenRouter account settings instead.

OpenRouter lists prices in USD per token, request or unit; check the model's page.
([OpenRouter: models](https://github.com/OpenRouterTeam/docs/blob/main/guides/overview/models.mdx))
Each question makes two requests (search planning, then the answer), and the answer request
carries the article excerpts.

### What Dewey sends

On OpenRouter, every request includes OpenRouter's `provider` preferences in the request body.
`OPENROUTER_PRIVACY` picks the privacy part
([OpenRouter: provider selection](https://github.com/OpenRouterTeam/docs/blob/main/guides/routing/provider-selection.mdx)):

| `OPENROUTER_PRIVACY` | Sent on every request | Meaning |
|---|---|---|
| `strict` (default) | `"data_collection": "deny", "zdr": true` | Only providers that don't collect your data, and only Zero Data Retention endpoints (with the caveat below). |
| `standard` | `"data_collection": "deny"` | Only providers that don't collect your data. Endpoints that keep prompts to monitor abuse (Safety Retention) can still be used ([ZDR docs](https://github.com/OpenRouterTeam/docs/blob/main/guides/features/zdr.mdx): "When ZDR isn't enforced, Safety Retention endpoints are available like any other endpoint that doesn't train on your data"). |
| `off` | nothing | OpenRouter's defaults and your account settings decide. OpenRouter's request default is `data_collection: "allow"`, which allows providers that store data and may train on it. |

The search-planning request also sends `"require_parameters": true` at every level, so
OpenRouter only routes it to endpoints that support all its parameters, including the
`json_schema` response format. Without it, OpenRouter can send the request to an endpoint that
ignores parameters it doesn't support. The answer request doesn't need it. Each request is
routed on its own, so the two may be served by different providers (*unverified*: our reading
of OpenRouter's routing docs).

Your [account privacy settings](https://openrouter.ai/settings/privacy) apply as well.
Per-request `zdr` can switch ZDR on but can't switch off ZDR that your account or a guardrail
enforces.

### What the privacy settings do and don't do

From OpenRouter's documentation:

- **OpenRouter itself** does not store your prompts or responses unless you opt in (prompt
  logging, or letting OpenRouter use your data for a discount), or content is flagged as
  potential child sexual abuse material. It screens requests for that in flight, including ZDR
  traffic.
  ([OpenRouter: data collection](https://github.com/OpenRouterTeam/docs/blob/main/guides/privacy/data-collection.mdx))
- **It does store metadata** for every request: token counts, latency and similar. This shows in
  your OpenRouter logs. The settings on this page don't change that. (Same page.)
- **`data_collection: "deny"`** limits routing to providers that, by OpenRouter's records, don't
  collect your data. OpenRouter says its provider data-policy information "is not a definitive
  source of third party data policies, but represents our best knowledge."
  ([provider selection](https://github.com/OpenRouterTeam/docs/blob/main/guides/routing/provider-selection.mdx))
- **`zdr: true` is not quite "zero retention" by default.** OpenRouter has a tier called Safety
  Retention: OpenRouter or the provider may keep prompts and responses from any request, only to
  detect and prevent abuse, and not for training. With `zdr: true`, Safety Retention endpoints are
  still allowed unless your account's privacy settings are set to **ZDR only**. Self-serve
  accounts allow Safety Retention unless you change it. For strict zero retention, turn on
  **ZDR only** in your [privacy settings](https://openrouter.ai/settings/privacy). That may make
  some models unavailable.
  ([OpenRouter: Zero Data Retention](https://github.com/OpenRouterTeam/docs/blob/main/guides/features/zdr.mdx))
- **These are policies, not technical controls.** OpenRouter and the provider receive the full
  text of each request in order to run it. The settings govern what they keep afterwards, not
  what they see.

If your newsroom has rules about sending reporters' questions to outside services, check them
before using this option. `standard` and `off` should be a deliberate choice, not a fix for an
error.

### When OpenRouter can't route a request

If no endpoint for the model meets the privacy preferences, or (for search planning) supports
structured outputs, OpenRouter rejects the request. Dewey then stops with an error that names the
model, quotes OpenRouter's message and lists what to change. It doesn't fall back to plain JSON
mode in that case, because the fallback would hide the problem. OpenRouter's docs show both
HTTP 404 and 503 for routing failures, and don't fully document which case gets which, so Dewey
treats both (and a 400 whose message says no endpoint or provider was available) as routing
failures. ([404 example](https://github.com/OpenRouterTeam/docs/blob/main/guides/routing/routers/router-metadata.mdx),
[503 in the error codes](https://github.com/OpenRouterTeam/docs/blob/main/api_reference/errors-and-debugging.mdx).
The 400 case is our guess, *unverified*.)

| What you see | What to do |
|---|---|
| `OpenRouter could not route Dewey's search-planning request …` | Pick a model whose Providers section lists endpoints with structured outputs (and ZDR, on `strict`). Check that `OPEN_LLM_MODEL` is spelled exactly as on openrouter.ai; a wrong id gives a similar error. Relax `OPENROUTER_PRIVACY` only if your policy allows it. |
| `… could not route Dewey's answer request …` | Same, minus the structured-outputs part. |
| HTTP 503 | OpenRouter's docs list 503 as "no available model provider that meets your routing requirements." If the model worked before, try again later. ([errors](https://github.com/OpenRouterTeam/docs/blob/main/api_reference/errors-and-debugging.mdx)) |
| HTTP 401 | The key is missing, wrong or revoked. |
| HTTP 402 | The account or key is out of credits. |

The demo page doesn't show these details to the journalist. The full message is in the log of the
process running `app/mcp_server.py` (for `ask_archive`) or `app/main.py`.

## Hermes model licenses

Nous Research's Hermes models come in several sizes. Their licenses differ, which matters if you
want the stack to stay open source:

| Model | Built on | License | OSI open source? |
|---|---|---|---|
| [Hermes 4.3 36B](https://hf.co/NousResearch/Hermes-4.3-36B) | ByteDance Seed-OSS-36B | Apache-2.0 | Yes |
| [Hermes 4 14B](https://hf.co/NousResearch/Hermes-4-14B) | Qwen3-14B | Apache-2.0 | Yes |
| [Hermes 4 70B](https://hf.co/NousResearch/Hermes-4-70B), [405B](https://hf.co/NousResearch/Hermes-4-405B) | Llama 3.1 | Meta's Llama 3 license (`llama3` on the model page) | No. It is free to download but carries Meta's use restrictions. Read it before you deploy. |

The same licenses apply however you run the model: Ollama, vLLM or OpenRouter. Builds in other
catalogues (Ollama's library, OpenRouter) should say which of these they are; check.

## Test before you rely on it

Small local models cite less reliably than large hosted ones. They are more likely to miss a
citation, cite the wrong article, or answer when they should say the archive doesn't cover the
question. Before you give a model to the newsroom:

1. Write 10–20 questions whose answers you know from your own archive. Include a few the archive
   **can't** answer, and a few with a date range or byline ("What did Jane Doe write about the
   transit strike in 2019?").
2. Ask each one in Dewey with the model you're considering. Open the thought process and check
   that the search plan got the dates and authors right.
3. Click every citation. Does the linked article support the sentence?
4. Check that the unanswerable questions get a clear "the archive doesn't answer this" (the answer
   prompt tells the model to say so instead of guessing; the exact wording varies), not a guess.
5. Repeat when you change models, providers, or `OPENROUTER_PRIVACY` (which can change the
   provider that serves the model).
