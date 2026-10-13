# Drafting with subscription accounts

Portkeeper can run the official Codex CLI, Claude Code, and Gemini CLI using their native account logins. Choose the corresponding subscription provider in Settings and sign in there. This is separate from the existing API-key providers and Ollama. A CLI installed in the container is not automatically signed in, and an account must have access to the selected model.

Subscription mode never falls back to an API key, a different provider, or a cloud project credential when authentication or quota checks fail. Portkeeper gives each CLI a private home and a small environment allowlist instead of inheriting API keys or provider settings from the service. Native authentication is checked before drafting. OAuth credentials stay with the vendor CLI; Portkeeper does not copy them into its HTTP model provider or use subscription tokens as an API proxy.

## Sign-in and Unraid

- **Codex:** native `codex login --device-auth` supplies a browser link and a one-time code. Device-code login must be permitted by your ChatGPT account or workspace. Open the official link on your laptop or phone and approve it there. Portkeeper checks native login status afterwards. See [OpenAI's authentication guide](https://developers.openai.com/codex/auth).
- **Claude Code:** native `claude auth login --claudeai` explicitly selects a Claude subscription. Complete the vendor's browser authorization; if requested, return the one-time authorization code to the waiting login. `--console` selects separately billed API usage and is not used here. Portkeeper checks `claude auth status --json`. See [Claude Code authentication](https://code.claude.com/docs/en/authentication).
- **Gemini:** the native CLI uses **Sign in with Google**, with personal Google OAuth enforced by the adapter. Use the account associated with your Google AI subscription if applicable. Its remote/manual flow opens an official authorization link and asks for the returned code. Cached login is verified by the native ACP authentication handshake, without sending a model prompt. See [Gemini CLI authentication](https://geminicli.com/docs/get-started/authentication/).

If a browser authorization flow cannot finish through Portkeeper, use the **terminal command displayed by Settings** in the Unraid container console or over SSH. The command starts the same installed CLI as the container's configured non-root user, with the correct private home and clean environment. Do not substitute a bare command using `/tmp` as its home: that can lose the login when the container is recreated. Do not paste OAuth tokens or credential files into the API-key field, a model prompt, or an issue. After terminal login, refresh/check the provider's connection in Settings. Completing login does not run a draft.

With the usual `/data` mapping, native state lives under:

| Provider | Private home | Native configuration |
| --- | --- | --- |
| Codex | `/data/secrets/agents/codex` | `.codex` via `CODEX_HOME` |
| Claude Code | `/data/secrets/agents/claude_code` | `.claude` via `CLAUDE_CONFIG_DIR` |
| Gemini | `/data/secrets/agents/gemini` | `.gemini` via `GEMINI_CLI_HOME` |

These folders belong to the configured service UID and use private permissions. They persist in appdata across image updates. They are credentials, so keep them out of public exports and Git repositories. Portkeeper does not read token values into its public connection status.

## Usage and validation

Drafts consume the account's normal allowance; subscriptions are not unlimited inference. Codex's local and cloud work share plan usage, and purchased credits may apply. Claude Code shares the connected Claude account's allowance and can draw on usage credits already enabled on that account. Google sign-in uses the account's Gemini CLI/Code Assist quota. Portkeeper does not enable extra usage, purchase credits, or switch to API billing. Check the vendor's account controls if you require a strict spend limit. See [OpenAI usage](https://developers.openai.com/codex/pricing), [Claude subscription limits](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan), [Claude usage credits](https://support.claude.com/en/articles/12429409-manage-usage-credits-for-paid-claude-plans), and [Gemini quotas](https://geminicli.com/docs/resources/quota-and-pricing/).

Only the prepared public project material and sanitized drafting context are supplied to the CLI. Owned ROMs, extracted game assets, appdata, credentials, and private input paths are not drafting material. The CLI's filesystem, shell, MCP, extension, and other native tools are disabled or denied by provider-specific controls; Portkeeper runs supported packaging operations through its existing builder. Returned instructions still require the usual schema/release checks, local test-build validation where configured, and user review before approval. A passed packaging test does not establish native Windows startup or gameplay.

## Pinned installation

| Component | Version |
| --- | --- |
| Official Node.js LTS runtime | `24.21.0` |
| `@openai/codex` | `0.162.1` |
| `@anthropic-ai/claude-code` (stable channel) | `2.1.287` |
| `@google/gemini-cli` | `0.63.0` |

The Dockerfile pins the official Node multi-platform image by digest. `tools/subscription-agents/package-lock.json` pins npm versions, registry archive URLs, and SHA-512 integrity for all package archives, including AMD64 and ARM64 native optional packages. `npm ci` verifies the lock. The build skips optional shell/keychain compilation and runs only Claude's unmodified official postinstall to link its locked native executable. The vendor programs and bundled JavaScript are not patched. All three are on the container PATH, and no npm installation or root access is needed at login time.

The current publication workflow ships AMD64. Native ARM64 package entries and architecture-aware library copying are retained for a future multi-platform workflow; ARM64 has not been runtime-tested here. Local AMD64 image verification runs version/help probes as an arbitrary non-root UID, with a read-only container, network disabled, and fresh private homes. Updating these versions requires an explicit lockfile/image change; Claude's updater is disabled in the shipped image, and the adapter disables Gemini updates for its managed sessions.

Official installation references: [Codex CLI](https://developers.openai.com/codex/cli), [Claude Code setup](https://code.claude.com/docs/en/setup), [Gemini CLI installation](https://geminicli.com/docs/get-started/installation/).
